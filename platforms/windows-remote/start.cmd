@echo off
setlocal
if not exist "%~dp0build\Qiban.Remote.exe" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1"
    if errorlevel 1 exit /b 1
)
if not "%~1"=="" goto supplied
set "labProfile=%~dp0..\..\.dev\linux-status\qiban-linux-round1\endpoint.json"
if not exist "%labProfile%" goto supplied
start "" "%~dp0build\Qiban.Remote.exe" --profile "%labProfile%"
exit /b
:supplied
start "" "%~dp0build\Qiban.Remote.exe" %*
