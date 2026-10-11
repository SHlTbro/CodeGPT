param(
  [int]$Port = 0,
  [int]$SetupPort = 8169,
  [ValidateSet('', 'off', 'cloudflare', 'ngrok')][string]$Tunnel = '',
  [switch]$NoBrowser,
  [string]$Config = ''
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
# The Run entry has no predictable cwd: relative configs belong to this project.
Set-Location -LiteralPath $root
if ($Config -eq '') { $Config = Join-Path $root 'config.toml' }
$Config = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Config)
$vpy = Join-Path $root '.venv\Scripts\python.exe'
function Assert-DependencyInstallPath {
  # Avoid a partial Node-wheel install on Windows' legacy path limit; do not
  # modify the user's system settings to make a deployment work.
  $nodeInstallPath = Join-Path $root '.venv\Lib\site-packages\nodejs_wheel\lib\node_modules\npm\node_modules\@sigstore\protobuf-specs\dist\__generated__\google\protobuf\descriptor.js'
  $longPaths = (Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' -Name LongPathsEnabled -ErrorAction SilentlyContinue).LongPathsEnabled
  if ($longPaths -ne 1 -and $nodeInstallPath.Length -ge 260) {
    Write-Host 'CodeGPT: Install path is too long for this Windows configuration. Move the project to a short folder such as D:\CodeGPT, then rerun setup.bat. No system setting was changed.'
    exit 2
  }
}
if (-not (Test-Path -LiteralPath $vpy)) {
  Assert-DependencyInstallPath
  $py = $null
  foreach ($candidate in @('python', 'py')) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
      $ErrorActionPreference = 'Continue'
      & $found.Source -c 'import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
      $probeCode = $LASTEXITCODE
      $ErrorActionPreference = 'Stop'
      if ($probeCode -eq 0) { $py = $found.Source; break }
    }
  }
  if (-not $py) { Write-Host 'CodeGPT: Install Python 3.12+ from https://www.python.org/downloads/ and double-click setup.bat again.'; exit 1 }
  $ErrorActionPreference = 'Continue'
  & $py -m venv (Join-Path $root '.venv')
  $venvCode = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($venvCode -ne 0) { Write-Host 'CodeGPT: Cannot create .venv. Check folder write permission.'; exit 1 }
}
$ErrorActionPreference = 'Continue'
& $vpy -c 'import sys;sys.exit(0 if sys.version_info >= (3,12) else 1)' 2>$null
$versionCode = $LASTEXITCODE
& $vpy -c 'from fastmcp import FastMCP,Client;from fastmcp.client.transports import StreamableHttpTransport;import yaml,tomllib,pyright,nodejs_wheel,jsonschema,rfc3339_validator,rfc3986_validator' 2>$null
$dependencyCode = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
if ($versionCode -ne 0) { Write-Host 'CodeGPT: Existing .venv needs Python 3.12+. Rename .venv, install supported Python and rerun setup.bat.'; exit 1 }
if ($dependencyCode -ne 0) {
  Assert-DependencyInstallPath
  Write-Host '[CodeGPT setup] Installing project dependencies...'
  $ErrorActionPreference = 'Continue'
  & $vpy -m pip install -r (Join-Path $root 'requirements.txt')
  $installCode = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($installCode -ne 0) { Write-Host 'CodeGPT: Dependency install failed. Check the error above. For path errors use a short folder such as D:\CodeGPT; for download errors check internet access and retry setup.bat.'; exit 1 }
}
$launchArgs = @((Join-Path $root 'setup.py'), '--config', $Config, '--port', "$SetupPort")
if (-not $NoBrowser) { $launchArgs += '--open' }
if ($Port -gt 0) { $launchArgs += @('--mcp-port', "$Port") }
if ($Tunnel -ne '') { $launchArgs += @('--tunnel', $Tunnel) }
# Foreground owner: closing this window stops only this instance's child processes.
# Unexpected owner failure recovers the same canonical config; explicit stop and
# actionable startup errors exit. Never reapply bootstrap overrides or open tabs.
while ($true) {
  & $vpy @launchArgs
  $ownerExit = $LASTEXITCODE
  if ($ownerExit -eq 0 -or $ownerExit -eq 2) { exit $ownerExit }
  Write-Host '[CodeGPT] Setup stopped unexpectedly; restoring saved configuration in 3 seconds...'
  Start-Sleep -Seconds 3
  $launchArgs = @((Join-Path $root 'setup.py'), '--config', $Config, '--port', "$SetupPort")
}
