@echo off
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b 1
cd /d "%~dp0.."
if not exist ".dev\wechat\native-module-build" mkdir ".dev\wechat\native-module-build"
cl /nologo /std:c++17 /EHsc /W4 /WX /utf-8 /LD /MD /O2 /DNDEBUG src\companion_wechat\native\bridge.cpp /Fe:.dev\wechat\native-module-build\xln_native_module.dll /Fo:.dev\wechat\native-module-build\bridge.obj /link /MACHINE:X64 bcrypt.lib user32.lib /IMPLIB:.dev\wechat\native-module-build\bridge.lib
if errorlevel 1 exit /b 1
.venv\Scripts\python.exe -m companion_wechat.module_cli manifest
exit /b %errorlevel%
