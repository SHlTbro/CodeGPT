@echo off
rem CodeGPT compatibility entry point; delegates to the same Setup lifecycle.
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
set "deploy_exit_code=%errorlevel%"
if not "%deploy_exit_code%"=="0" pause
exit /b %deploy_exit_code%
