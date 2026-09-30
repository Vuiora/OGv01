@echo off
chcp 65001 >nul
title CodexZH Setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-codexzh.ps1"
set "RC=%ERRORLEVEL%"
echo.
if not "%RC%"=="0" echo Setup failed. Error code: %RC%
pause
exit /b %RC%
