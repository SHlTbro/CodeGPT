@echo off
rem One-click deploy for Local Codebase MCP: starts server + tunnel, prints the public URL for ChatGPT.
rem NOTE: keep this file ASCII-only (Chinese comments here get shredded by cmd under wrong codepage, C-023).
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy.ps1" %*
pause
