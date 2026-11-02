@echo off
rem Open this project's daily dashboard from any working directory.
setlocal
set "DASHBOARD_PWSH=pwsh.exe"
where pwsh.exe >nul 2>nul
if not errorlevel 1 goto pwsh_ready
set "DASHBOARD_PWSH=%ProgramFiles%\PowerShell\7\pwsh.exe"
if exist "%DASHBOARD_PWSH%" goto pwsh_ready
rem Codex bundles the installed runtime even when Explorer's PATH does not include it.
set "DASHBOARD_PWSH=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\powershell\pwsh.exe"
if exist "%DASHBOARD_PWSH%" goto pwsh_ready
echo PowerShell 7 ^(pwsh.exe^) is missing. Install it before launching the dashboard.
pause
exit /b 1
:pwsh_ready
rem Explicit -Config overrides the daily full-run default.
set "DASHBOARD_CONFIG=-Config configs/a0-simulation.json"
for %%A in (%*) do if /i "%%~A"=="-Config" set "DASHBOARD_CONFIG="
"%DASHBOARD_PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-dashboard.ps1" %DASHBOARD_CONFIG% %*
set "DASHBOARD_EXIT=%ERRORLEVEL%"
if not "%DASHBOARD_EXIT%"=="0" (
    echo Dashboard launch failed. Review the error above.
    pause
)
exit /b %DASHBOARD_EXIT%
