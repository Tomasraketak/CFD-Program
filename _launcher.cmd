@echo off
rem ===========================================================================
rem  AeroThermalStudio - shared launcher logic
rem
rem  Not run directly. Every .bat in this folder sets ATS_MODE and calls this,
rem  so Python discovery, the virtual environment and error reporting exist in
rem  one place instead of being copied into each launcher.
rem
rem  Discovery order:
rem    1. .venv in the project folder   (if you created one)
rem    2. the "py" launcher              (standard on Windows)
rem    3. python on PATH
rem ===========================================================================

setlocal EnableDelayedExpansion

set "ATS_ROOT=%~dp0"
if "%ATS_ROOT:~-1%"=="\" set "ATS_ROOT=%ATS_ROOT:~0,-1%"

rem --- locate an interpreter -------------------------------------------------
set "ATS_PYTHON="

if exist "%ATS_ROOT%\.venv\Scripts\python.exe" (
    set "ATS_PYTHON=%ATS_ROOT%\.venv\Scripts\python.exe"
    set "ATS_PYTHON_SOURCE=project virtual environment (.venv)"
    goto :found
)

py -3 --version >nul 2>&1
if %ERRORLEVEL%==0 (
    set "ATS_PYTHON=py -3"
    set "ATS_PYTHON_SOURCE=Python launcher (py -3)"
    goto :found
)

python --version >nul 2>&1
if %ERRORLEVEL%==0 (
    set "ATS_PYTHON=python"
    set "ATS_PYTHON_SOURCE=python on PATH"
    goto :found
)

echo.
echo  ============================================================
echo   Python was not found.
echo  ============================================================
echo.
echo   AeroThermalStudio needs Python 3.11 or newer.
echo.
echo   1. Install it from https://www.python.org/downloads/
echo      Tick "Add Python to PATH" in the installer.
echo   2. Run Setup.bat in this folder.
echo.
call :pause_unless_mcp
exit /b 1

:found
rem --- check the version is new enough ---------------------------------------
%ATS_PYTHON% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1
if not %ERRORLEVEL%==0 (
    echo.
    echo  ============================================================
    echo   Python is too old.
    echo  ============================================================
    echo.
    %ATS_PYTHON% --version
    echo.
    echo   AeroThermalStudio needs Python 3.11 or newer.
    echo   Install a newer version, then run Setup.bat.
    echo.
    call :pause_unless_mcp
    exit /b 1
)

rem --- run -------------------------------------------------------------------
pushd "%ATS_ROOT%"

if "%ATS_MODE%"=="setup" (
    echo  Using %ATS_PYTHON_SOURCE%
    echo.
    %ATS_PYTHON% setup_env.py %ATS_ARGS%
) else (
    %ATS_PYTHON% run_app.py %ATS_MODE% %ATS_ARGS%
)

set "ATS_EXIT=%ERRORLEVEL%"
popd

if not "%ATS_EXIT%"=="0" (
    echo.
    echo  ------------------------------------------------------------
    echo   Finished with exit code %ATS_EXIT%.
    echo   If this was unexpected, run Check-Environment.bat and read
    echo   docs\en\TUTORIAL.md ^(or docs\cs\TUTORIAL.md^).
    echo  ------------------------------------------------------------
    echo.
    call :pause_unless_mcp
)

exit /b %ATS_EXIT%

rem ---------------------------------------------------------------------------
rem  Wait for the operator, unless this is the MCP server.
rem
rem  The MCP server speaks JSON-RPC on stdin/stdout and is started by an AI
rem  client, so a "Press any key" prompt would hang that client with no way to
rem  answer it. Every other mode is launched by a person who needs to read the
rem  error before the console window closes.
rem ---------------------------------------------------------------------------
:pause_unless_mcp
if "%ATS_MODE%"=="--mcp" goto :eof
pause
goto :eof
