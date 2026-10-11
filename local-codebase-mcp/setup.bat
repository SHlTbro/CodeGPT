@echo off
rem CodeGPT local-only setup owner. Keep this window open while using the MCP.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
set "setup_exit_code=%errorlevel%"
if not "%setup_exit_code%"=="0" pause
exit /b %setup_exit_code%
