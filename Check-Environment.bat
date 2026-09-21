@echo off
rem  AeroThermalStudio - report the detected toolchain.
rem  Run this when a solve reports that SU2 or MPI is missing.
title AeroThermalStudio - Environment check
set "ATS_MODE=--check"
set "ATS_ARGS=%*"
call "%~dp0_launcher.cmd"
pause
