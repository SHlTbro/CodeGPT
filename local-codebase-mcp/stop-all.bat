@echo off
rem 一键停止：MCP Server（按 server.pid 精确停止，不误伤其他 python）+ ngrok 隧道
chcp 65001 >nul
cd /d "%~dp0"
powershell -NoProfile -Command ^
  "$spid = $null; if (Test-Path 'server.pid') { $spid = (Get-Content 'server.pid' -Raw).Trim() };" ^
  "if ($spid) { Stop-Process -Id $spid -Force -ErrorAction SilentlyContinue; Remove-Item 'server.pid' -ErrorAction SilentlyContinue; Write-Host ('stopped server pid=' + $spid) }" ^
  "Get-Process ngrok -ErrorAction SilentlyContinue | Stop-Process -Force; Write-Host 'stopped ngrok';" ^
  "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*server.py*config.runtime.yaml*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('stopped leftover server pid=' + $_.ProcessId) }"
pause
