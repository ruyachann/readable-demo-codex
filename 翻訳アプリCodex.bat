@echo off
setlocal EnableExtensions
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set "MSG=%~dp0bat_messages"
rem Start the Codex edition with the shared local browser UI.
set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 goto nopython
type "%MSG%\webapp_start.txt"
%PY% -m readable.webapp --structure-provider codex
set RC=%errorlevel%
echo.
if %RC%==3 goto running
type "%MSG%\webapp_stopped.txt"
pause
exit /b %RC%
:running
type "%MSG%\webapp_running.txt"
pause
exit /b 3
:nopython
type "%MSG%\nopython.txt"
pause
exit /b 1
