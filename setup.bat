@echo off
setlocal EnableExtensions
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set "MSG=%~dp0bat_messages"
rem Creates .venv inside this folder and installs requirements.txt into it. Nothing outside this folder is changed.
rem ASCII only on purpose; Japanese messages live in bat_messages\*.txt.

set "PY=python"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

type "%MSG%\setup_start.txt"
if exist ".venv\Scripts\python.exe" goto install
%PY% -m venv .venv
if errorlevel 1 goto fail

:install
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto fail
".venv\Scripts\python.exe" -c "import fitz, fontTools, google.genai" >nul 2>nul
if errorlevel 1 goto fail

type "%MSG%\setup_done.txt"
pause
exit /b 0

:nopython
type "%MSG%\nopython.txt"
pause
exit /b 1

:oldpython
type "%MSG%\setup_oldpython.txt"
pause
exit /b 1

:fail
type "%MSG%\setup_fail.txt"
pause
exit /b 1
