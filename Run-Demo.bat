@echo off
rem  AeroThermalStudio - self-contained demonstration.
rem  Builds the sample rocket, aligns it, extrudes the boundary layer and
rem  meshes the farfield. Needs no solver, so it is the quickest way to
rem  confirm the installation works. Takes about a minute.
title AeroThermalStudio - Demo
echo.
echo  Building and meshing the reference rocket. This takes about a minute.
echo.
set "ATS_MODE=--demo"
set "ATS_ARGS=%*"
call "%~dp0_launcher.cmd"
pause
