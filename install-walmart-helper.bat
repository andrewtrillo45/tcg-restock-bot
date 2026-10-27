@echo off
REM Creates a Windows scheduled task "TCG Walmart helper" that runs walmart-helper.bat every 10 minutes
REM (only while you're logged in; it runs hidden). Run this once. To remove: schtasks /delete /tn "TCG Walmart helper" /f
cd /d "%~dp0"
schtasks /create /f /tn "TCG Walmart helper" /sc minute /mo 10 /tr "cmd /c \"\"%~dp0walmart-helper.bat\"\" >> \"%~dp0state-walmart\walmart-helper.log\" 2>&1" /rl limited
if %errorlevel%==0 (
  echo.
  echo Done. Walmart is now checked from this PC every 10 minutes while you're logged in.
  echo Log: state-walmart\walmart-helper.log
  echo Next: on GitHub go to Settings ^> Secrets and variables ^> Actions ^> Variables ^> New variable:
  echo       WALMART_ON_PC = true   (so the GitHub runs stop hitting Walmart from server IPs)
) else (
  echo Could not create the task. Right-click this file and "Run as administrator", then try again.
)
pause
