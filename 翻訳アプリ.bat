@echo off
setlocal EnableExtensions
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set "MSG=%~dp0bat_messages"
rem Starts the local web app (127.0.0.1 only) and opens the browser. Close this window to quit.
rem This file is ASCII only on purpose (cmd mis-parses UTF-8 after chcp); Japanese messages live in bat_messages\*.txt.

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 goto nopython

type "%MSG%\webapp_start.txt"
%PY% -m readable.webapp
set RC=%errorlevel%
echo.
if %RC%==3 goto running
type "%MSG%\webapp_stopped.txt"
pause
exit /b 0

:running
type "%MSG%\webapp_running.txt"
pause
exit /b 3

:nopython
type "%MSG%\nopython.txt"
pause
exit /b 1

