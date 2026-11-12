@echo off
REM Thin launcher only - all real logic lives in run_phase1.py.
REM (Batch cannot be trusted with paths containing parentheses; see the note
REM  at the top of run_phase1.py.)
cd /d "%~dp0"
set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 goto :nopython
%PY% "%~dp0run_phase1.py" --flash %*
set "RC=%ERRORLEVEL%"
echo.
pause
exit /b %RC%

:nopython
echo [ERROR] Python was not found.
echo   Install Python 3 from https://www.python.org/downloads/
echo   Tick "Add python.exe to PATH" on the first installer screen.
echo.
pause
exit /b 1
