@echo off
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b 1
cd /d "%~dp0..\.."
if not exist ".dev\wechat\native-probe-build" mkdir ".dev\wechat\native-probe-build"
cl /nologo /std:c++17 /EHsc /W4 /WX /utf-8 /LD /MD /O2 /DNDEBUG scripts\wechat_native_probe\bridge.cpp /Fe:.dev\wechat\native-probe-build\xln_native_probe.dll /Fo:.dev\wechat\native-probe-build\bridge.obj /link /MACHINE:X64 bcrypt.lib user32.lib /IMPLIB:.dev\wechat\native-probe-build\bridge.lib
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m scripts.wechat_native_probe.build_manifest
exit /b %errorlevel%
