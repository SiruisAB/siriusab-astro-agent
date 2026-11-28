@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_web_advisor.ps1"
if errorlevel 1 pause
