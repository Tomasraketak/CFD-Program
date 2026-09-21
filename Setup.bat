@echo off
rem  AeroThermalStudio - first-time setup.
rem  Checks the machine, installs the Python packages and fetches SU2.
rem  Run this once before using the program.
title AeroThermalStudio - Setup
echo.
echo  ============================================================
echo   AeroThermalStudio setup
echo  ============================================================
echo.
set "ATS_MODE=setup"
set "ATS_ARGS=%*"
call "%~dp0_launcher.cmd"
