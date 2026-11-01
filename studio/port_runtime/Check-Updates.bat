@echo off
rem RetroRemakes PS3 Studio
rem Copyright (c) 2026 RetroRemakes. All rights reserved.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0PortTools.ps1" -Action Check %*
if errorlevel 1 pause
