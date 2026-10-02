@echo off
rem Watchdog: keeps MCP server + ngrok tunnel alive (auto-restart every 60s check).
rem Keep this window open. Closing it stops the watchdog only (services keep running).
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0watchdog-tunnels.ps1"
