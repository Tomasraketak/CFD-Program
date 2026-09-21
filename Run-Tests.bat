@echo off
rem  AeroThermalStudio - run the test suite.
rem  Useful after changing anything, or to verify an installation.
title AeroThermalStudio - Tests
setlocal
set "ATS_ROOT=%~dp0"
if "%ATS_ROOT:~-1%"=="\" set "ATS_ROOT=%ATS_ROOT:~0,-1%"
set "QT_QPA_PLATFORM=offscreen"
set "PYVISTA_OFF_SCREEN=true"
if exist "%ATS_ROOT%\.venv\Scripts\python.exe" (
    set "ATS_PYTHON=%ATS_ROOT%\.venv\Scripts\python.exe"
) else (
    set "ATS_PYTHON=py -3"
)
pushd "%ATS_ROOT%"
%ATS_PYTHON% -m pytest -q
popd
pause
