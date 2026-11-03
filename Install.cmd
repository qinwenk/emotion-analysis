@echo off
setlocal
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Workflow.ps1"
set "install_result=%errorlevel%"
echo.
if not "%install_result%"=="0" echo Installation did not finish. Keep the error above and ask Codex for help.
pause
exit /b %install_result%
