# Self-check for deploy.ps1 tunnel argv construction (task23).
# Run: powershell -NoProfile -ExecutionPolicy Bypass -File tests\test_deploy_args.ps1
# Prints PASS/FAIL per assertion; exit 0 = all pass, 1 = any fail.
$ErrorActionPreference = 'Continue'
$testsDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$root = Split-Path -Parent $testsDir
$deploy = Join-Path $root 'deploy.ps1'
$script:failCount = 0

function Check($name, $ok) {
  if ($ok) { Write-Host "PASS: $name" }
  else { Write-Host "FAIL: $name"; $script:failCount++ }
}

# --- 1. PowerShell syntax: Parser::ParseFile must yield 0 errors ---
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($deploy, [ref]$null, [ref]$parseErrors)
Check 'deploy.ps1 parses with 0 errors' (@($parseErrors).Count -eq 0)

function Invoke-DryRun {
  param([string]$DeployArgs)
  $out = & powershell -NoProfile -ExecutionPolicy Bypass -File $deploy -DryRunTunnel -Port 8199 $DeployArgs.Split(' ') 2>&1
  return ($out | Out-String)
}
function Get-ArgvLine($text) {
  $m = [regex]::Match($text, 'DRYRUN tunnel-argv=(\S.*)')
  if ($m.Success) { return $m.Groups[1].Value.Trim() }
  return ''
}

# --- 2. -TunnelToken param -> named tunnel argv: tunnel run --protocol http2; token never in argv/output ---
$tokenParam = 'SECRETTESTTOKEN0123456789'
$outA = Invoke-DryRun "-TunnelToken $tokenParam"
Check 'token mode: tunnel-mode=named reported' ($outA -match 'DRYRUN tunnel-mode=named')
$argvA = Get-ArgvLine $outA
Check "token mode: argv is 'tunnel run --protocol http2 ...' (got: $argvA)" ($argvA -match '^tunnel run --protocol http2')
Check 'token mode: argv contains --protocol http2' ($argvA -match '--protocol http2')
Check 'token mode: token value NOT in argv or any output (env var only)' (-not ($outA -match [regex]::Escape($tokenParam)))
Check 'token mode: argv does not contain --token flag' (-not ($argvA -match '--token'))

# --- 3. no token, no token file -> quick tunnel argv: tunnel --url ... --protocol http2 ---
$tokenFile = Join-Path $root 'tunnel-token.txt'
$backupFile = $null
if (Test-Path $tokenFile) {
  $backupFile = "$tokenFile.testbak"
  Move-Item $tokenFile $backupFile -Force
  Write-Host 'NOTE: existing tunnel-token.txt moved aside for the no-token case (will be restored)'
}
try {
  $outB = Invoke-DryRun ''
  Check 'no-token mode: tunnel-mode=quick reported' ($outB -match 'DRYRUN tunnel-mode=quick')
  $argvB = Get-ArgvLine $outB
  Check "no-token mode: argv is 'tunnel --url http://127.0.0.1:8199 --protocol http2 ...' (got: $argvB)" ($argvB -match '^tunnel --url http://127\.0\.0\.1:8199 --protocol http2')
  Check 'no-token mode: argv contains --protocol http2' ($argvB -match '--protocol http2')
  Check 'no-token mode: argv has no "run" and no token flag' ((-not ($argvB -match '\brun\b')) -and (-not ($argvB -match 'token')))
} finally {
  if ($backupFile) { Move-Item $backupFile $tokenFile -Force }
}

# --- 4. token file branch (only when no real tunnel-token.txt present; never touches a real one) ---
if (-not (Test-Path $tokenFile)) {
  $multiline = "  `nFILETOKENTEST987654321`n  "
  Set-Content -Path $tokenFile -Value $multiline -Encoding ascii
  try {
    $outC = Invoke-DryRun ''
    Check 'token-file mode: tunnel-mode=named reported' ($outC -match 'DRYRUN tunnel-mode=named')
    $argvC = Get-ArgvLine $outC
    Check 'token-file mode: argv is tunnel run path' ($argvC -match '^tunnel run --protocol http2')
    Check 'token-file mode: multi-line/whitespace token trimmed and NOT in argv/output' (-not ($outC -match 'FILETOKENTEST987654321'))
  } finally {
    Remove-Item $tokenFile -Force
  }
} else {
  Write-Host 'SKIP: token-file branch test (real tunnel-token.txt present; not touching secrets)'
}

# --- 5. both branches must contain --protocol http2 (C-016) ---
Check 'both modes carry --protocol http2' ((Get-ArgvLine $outA -match '--protocol http2') -and ((Get-ArgvLine $outB) -match '--protocol http2'))

# --- 6. ngrok branch (ngrok-token.txt + ngrok-domain.txt -> fixed domain, token via NGROK_AUTHTOKEN env) ---
$ngrokTokenFile = Join-Path $root 'ngrok-token.txt'
$ngrokDomainFile = Join-Path $root 'ngrok-domain.txt'
$ngrokTokBak = $null; $ngrokDomBak = $null
if (Test-Path $ngrokTokenFile) { $ngrokTokBak = "$ngrokTokenFile.testbak"; Move-Item $ngrokTokenFile $ngrokTokBak -Force }
if (Test-Path $ngrokDomainFile) { $ngrokDomBak = "$ngrokDomainFile.testbak"; Move-Item $ngrokDomainFile $ngrokDomBak -Force }
Set-Content -Path $ngrokTokenFile -Value 'NGROKTESTTOKEN123456789' -Encoding ascii
Set-Content -Path $ngrokDomainFile -Value 'test-static.ngrok-free.app' -Encoding ascii
try {
  $outD = Invoke-DryRun ''
  Check 'ngrok mode: tunnel-mode=ngrok reported' ($outD -match 'DRYRUN tunnel-mode=ngrok')
  $argvD = Get-ArgvLine $outD
  Check "ngrok mode: argv is 'http --url=<fixed domain> ... <port>' (got: $argvD)" ($argvD -match '^http --url=test-static\.ngrok-free\.app .* 8199$')
  Check 'ngrok mode: token value NOT in argv or any output (env var only)' (-not ($outD -match 'NGROKTESTTOKEN123456789'))
} finally {
  Remove-Item $ngrokTokenFile -Force
  Remove-Item $ngrokDomainFile -Force
  if ($ngrokTokBak) { Move-Item $ngrokTokBak $ngrokTokenFile -Force }
  if ($ngrokDomBak) { Move-Item $ngrokDomBak $ngrokDomainFile -Force }
}

Write-Host ''
if ($script:failCount -eq 0) { Write-Host 'ALL PASS'; exit 0 } else { Write-Host "FAILED: $script:failCount assertion(s)"; exit 1 }
