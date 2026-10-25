@echo off
setlocal
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat" -arch=x64 -host_arch=x64 >nul
if errorlevel 1 exit /b 1
cd /d "%~dp0.."
if not exist ".dev\wechat\native-module-build" mkdir ".dev\wechat\native-module-build"
cl /nologo /std:c++17 /EHsc /W4 /WX /utf-8 /MD /O2 tests_wechat\native_session_harness.cpp /Fe:.dev\wechat\native-module-build\session_test.exe /Fo:.dev\wechat\native-module-build\session_test.obj
if errorlevel 1 exit /b 1
.dev\wechat\native-module-build\session_test.exe
exit /b %errorlevel%
