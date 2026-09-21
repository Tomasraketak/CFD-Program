@echo off
rem  AeroThermalStudio - start the desktop application.
rem  Double-click this file to run the program.
title AeroThermalStudio
set "ATS_MODE=--gui"
set "ATS_ARGS=%*"
call "%~dp0_launcher.cmd"
