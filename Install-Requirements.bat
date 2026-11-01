@echo off
rem RetroRemakes PS3 Studio
rem Copyright (c) 2026 RetroRemakes. All rights reserved.
setlocal
title RetroRemakes PS3 Studio - install requirements
cd /d "%~dp0"
echo.
echo  RetroRemakes PS3 Studio - install everything it needs
echo  =====================================================
echo  This installs, from each program's official source:
echo    - Python 3.12 (with Tcl/Tk)
echo    - Git
echo    - CMake
echo    - Ninja
echo    - Visual Studio 2022 Build Tools with C++, Windows SDK and Clang
echo    - Node.js LTS (optional, asked below)
echo.
echo  Programs you already have are skipped. Windows may ask for permission.
echo  The Build Tools download is several GB and can take a while.
echo.
echo  You still provide RPCS3 and the PS3 firmware yourself (see REQUIREMENTS.md).
echo.
pause

where winget >nul 2>nul
if errorlevel 1 goto nowinget

set "WG=winget install --exact --accept-package-agreements --accept-source-agreements"

echo.
echo [1/6] Python 3.12
%WG% --id Python.Python.3.12 --override "/passive PrependPath=1 Include_tcltk=1 Include_launcher=1"
echo.
echo [2/6] Git
%WG% --id Git.Git
echo.
echo [3/6] CMake
%WG% --id Kitware.CMake
echo.
echo [4/6] Ninja
%WG% --id Ninja-build.Ninja
echo.
echo [5/6] Visual Studio 2022 Build Tools (C++, Windows SDK, Clang)
%WG% --id Microsoft.VisualStudio.2022.BuildTools --override "--wait --passive --norestart --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.22621 --add Microsoft.VisualStudio.Component.VC.Llvm.Clang --add Microsoft.VisualStudio.Component.VC.Llvm.ClangToolset --includeRecommended"
echo.
choice /c YN /m "[6/6] Also install Node.js? Only needed for the optional Cartridge game identifier"
if errorlevel 2 goto done
%WG% --id OpenJS.NodeJS.LTS

:done
echo.
echo  Done. If anything above failed, run this file again or see REQUIREMENTS.md.
echo.
echo  Next:
echo    1. RESTART your PC so the new programs are found.
echo    2. Download RPCS3 and the PS3 firmware (pages open now).
echo    3. Run START-STUDIO.bat, then Settings and setup ^> Check environment.
echo.
start "" "https://rpcs3.net/download"
start "" "https://www.playstation.com/en-us/support/hardware/ps3/system-software/"
pause
exit /b 0

:nowinget
echo.
echo  This PC does not have winget (Windows App Installer).
echo  Get "App Installer" from the Microsoft Store, then run this file again,
echo  or install each program by hand using the links in REQUIREMENTS.md.
start "" "https://apps.microsoft.com/detail/9NBLGGH4NNS1"
pause
exit /b 1
