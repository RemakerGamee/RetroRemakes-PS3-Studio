@echo off
rem RetroRemakes PS3 Studio
rem Copyright (c) 2026 RetroRemakes. All rights reserved.
setlocal
cd /d "%~dp0"
rem Load MSVC headers, libraries and optional Clang tools when VS is installed.
set "RR_VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%RR_VSWHERE%" goto launch
for /f "usebackq tokens=*" %%i in (`"%RR_VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "RR_VS=%%i"
if defined RR_VS if exist "%RR_VS%\VC\Auxiliary\Build\vcvars64.bat" call "%RR_VS%\VC\Auxiliary\Build\vcvars64.bat"
:launch
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto plainpython
py -3 -c "import sys; assert sys.version_info >= (3,11); import tkinter" >nul 2>nul
if errorlevel 1 goto missing
py -3 "%~dp0bootstrap.py"
if errorlevel 1 pause
exit /b
:plainpython
python -c "import sys; assert sys.version_info >= (3,11); import tkinter" >nul 2>nul
if errorlevel 1 goto missing
python "%~dp0bootstrap.py"
if errorlevel 1 pause
exit /b
:missing
echo Install Python 3.11+ with Tcl/Tk, then reopen this launcher.
echo https://www.python.org/downloads/windows/
pause
