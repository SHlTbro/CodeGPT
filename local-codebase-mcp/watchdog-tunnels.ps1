# watchdog-tunnels.ps1 (C-025): checks every 60s and auto-restarts
#   1) MCP Server (port not listening -> hidden restart, PID -> server.pid)
#   2) ngrok tunnel (process gone -> reconnect via ngrok-token.txt/ngrok-domain.txt, fixed domain URL unchanged)
# Usage: run watchdog.bat (keep its window open = watchdog on duty; closing it stops the watchdog only)
# NOTE: keep this file ASCII-only (PS5.1 reads no-BOM ps1 as ANSI; Chinese comments break parsing, see C-009/C-023)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Get-RuntimePort {
  $f = "$root\config.runtime.yaml"
  if (Test-Path $f) {
    $m = [regex]::Match((Get-Content $f -Raw), 'port:\s*(\d+)')
    if ($m.Success) { return [int]$m.Groups[1].Value }
  }
  return 8168
}

Write-Host "[watchdog] started: server(8168) + ngrok, check every 60s"
while ($true) {
  try {
    $Port = Get-RuntimePort

    # 1. server: port not listening -> hidden restart
    $listening = $false
    $t = New-Object Net.Sockets.TcpClient
    try { $t.Connect('127.0.0.1', $Port); $t.Close(); $listening = $true } catch { } finally { $t.Dispose() }
    if (-not $listening) {
      $venv = "$root\.venv\Scripts\python.exe"; if (-not (Test-Path $venv)) { $venv = 'python' }
      $p = Start-Process -FilePath $venv -ArgumentList 'server.py', '--config', "$root\config.runtime.yaml" -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$root\server-access.log" -RedirectStandardError "$root\server-access.err"
      Set-Content -Encoding ascii "$root\server.pid" $p.Id
      Write-Host ("[{0}] server DOWN -> restarted pid={1}" -f (Get-Date -Format 'HH:mm:ss'), $p.Id)
    }

    # 2. ngrok: process gone OR edge unreachable (ERR_NGROK_3200) -> reconnect (fixed domain, URL unchanged)
    #    blind spot fix: process-alive is NOT proof of reachability; probe the public URL (C-026)
    $ngrokAlive = [bool](Get-Process ngrok -ErrorAction SilentlyContinue)
    $edgeOk = $false
    if ($ngrokAlive) {
      try { $r = Invoke-WebRequest "https://$((Get-Content "$root
grok-domain.txt" -Raw).Trim())/mcp" -UseBasicParsing -TimeoutSec 8 -ErrorAction Stop; $edgeOk = $true } catch {
        if ($_.Exception.Response) { $edgeOk = $true }  # any HTTP response = edge alive
      }
    }
    if ($ngrokAlive -and -not $edgeOk) {
      Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force
      $ngrokAlive = $false
      Write-Host ("[{0}] ngrok edge unreachable (ERR_NGROK_3200) -> killed for restart" -f (Get-Date -Format 'HH:mm:ss'))
    }
    if (-not $ngrokAlive) {
      if ((Test-Path "$root\ngrok-token.txt") -and (Test-Path "$root\ngrok-domain.txt")) {
        $exe = "$root\ngrok.exe"
        if (-not (Test-Path $exe)) { $c = Get-Command ngrok -ErrorAction SilentlyContinue; if ($c) { $exe = $c.Source } }
        if (Test-Path $exe) {
          $env:NGROK_AUTHTOKEN = ((Get-Content "$root\ngrok-token.txt" -Raw) -replace '\s+', '')
          $dom = ((Get-Content "$root\ngrok-domain.txt" -Raw) -replace '\s+', '')
          Start-Process -FilePath $exe -ArgumentList 'http', "--url=$dom", '--log', 'stdout', "$Port" -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\tunnel-ngrok.log" -RedirectStandardError "$root\tunnel-ngrok.err"
          Remove-Item Env:NGROK_AUTHTOKEN -ErrorAction SilentlyContinue
          Write-Host ("[{0}] ngrok DOWN -> reconnected (url unchanged: https://{1}/mcp)" -f (Get-Date -Format 'HH:mm:ss'), $dom)
        }
      }
    }
  } catch { Write-Host ("[{0}] watchdog err: {1}" -f (Get-Date -Format 'HH:mm:ss'), $_.Exception.Message) }
  Start-Sleep -Seconds 15
}
