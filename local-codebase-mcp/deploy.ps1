param([int]$Port = 0, [int]$SetupPort = 8169,
      [ValidateSet('', 'off', 'cloudflare', 'ngrok')][string]$Tunnel = '',
      [switch]$DryRunTunnel, [string]$TunnelToken = '', [switch]$NoBrowser,
      [string]$Config = '')
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($TunnelToken) {
  Write-Host 'CodeGPT: Named tunnel rejected: remote ingress cannot be verified as MCP-only. Use -Tunnel cloudflare or ngrok.'
  exit 1
}
if ($DryRunTunnel) {
  $targetPort = $Port
  if ($targetPort -eq 0) { $targetPort = 8168 }
  $mode = $Tunnel
  if ($mode -eq '') { $mode = 'cloudflare' }
  Write-Host "[CodeGPT deploy] DRYRUN tunnel-mode=$mode"
  if ($mode -eq 'ngrok') {
    Write-Host "[CodeGPT deploy] DRYRUN tunnel-argv=http http://127.0.0.1:$targetPort --log stdout"
  } elseif ($mode -eq 'cloudflare') {
    Write-Host "[CodeGPT deploy] DRYRUN tunnel-argv=tunnel --url http://127.0.0.1:$targetPort --protocol http2 --loglevel info"
  } else { Write-Host '[CodeGPT deploy] DRYRUN tunnel-argv=off' }
  Write-Host '[CodeGPT deploy] dry run complete: nothing started, no files touched'
  exit 0
}
# Same startup/config/process owner as setup.bat; no runtime YAML or alternate watchdog.
& (Join-Path $root 'setup.ps1') -Port $Port -SetupPort $SetupPort -Tunnel $Tunnel -NoBrowser:$NoBrowser -Config $Config
exit $LASTEXITCODE
