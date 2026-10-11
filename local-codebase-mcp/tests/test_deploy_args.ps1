# Unified launch/dry-run regression: no credentials touched and no processes started.
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$failures = 0
function Check($name, $value) {
  if ($value) { Write-Host "PASS: $name" } else { Write-Host "FAIL: $name"; $script:failures++ }
}
foreach ($file in @('setup.ps1','deploy.ps1','watchdog-tunnels.ps1')) {
  $errors = $null
  $null = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $root $file), [ref]$null, [ref]$errors)
  Check "$file parses" (@($errors).Count -eq 0)
}
$before = @{}
foreach ($name in @('config.toml','config.yaml','config.runtime.yaml','ngrok-token.txt','tunnel-token.txt')) {
  $path = Join-Path $root $name
  if (Test-Path -LiteralPath $path) { $before[$name] = (Get-FileHash -LiteralPath $path).Hash }
}
$quick = (& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'deploy.ps1') -DryRunTunnel -Port 8199 -Tunnel cloudflare | Out-String)
Check 'quick targets MCP loopback only' ($quick -match 'tunnel --url http://127.0.0.1:8199 --protocol http2')
$ngrok = (& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'deploy.ps1') -DryRunTunnel -Port 8199 -Tunnel ngrok | Out-String)
Check 'ngrok targets MCP loopback only' ($ngrok -match 'http http://127.0.0.1:8199 --log stdout')
$named = (& powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $root 'deploy.ps1') -DryRunTunnel -TunnelToken 'TESTTOKEN123456789' | Out-String)
Check 'unverifiable named ingress rejected' ($LASTEXITCODE -ne 0 -and $named -match 'Named tunnel rejected' -and $named -notmatch 'TESTTOKEN123456789')
foreach ($name in $before.Keys) {
  Check "dry-run preserves $name" ((Get-FileHash -LiteralPath (Join-Path $root $name)).Hash -eq $before[$name])
}
$prod = Get-Content -Raw -LiteralPath (Join-Path $root 'deploy.ps1')
Check 'deploy delegates to Setup' ($prod -match 'setup.ps1' -and $prod -notmatch 'config.runtime.yaml.*Write|Get-Process.*Stop-Process')
if ($failures) { exit 1 }
Write-Host 'ALL PASS'
