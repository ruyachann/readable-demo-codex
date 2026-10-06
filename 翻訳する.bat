@echo off
setlocal EnableExtensions
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
cd /d "%~dp0"
set "MSG=%~dp0bat_messages"
rem Drop English PDFs on this file. Output: <name>_ja.pdf and <name>_dual.pdf next to each PDF.
rem This file is ASCII only on purpose (cmd mis-parses UTF-8 after chcp); Japanese messages live in bat_messages\*.txt.

if "%~1"=="" goto usage

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
%PY% --version >nul 2>nul
if errorlevel 1 set "PY=py -3"
%PY% --version >nul 2>nul
if errorlevel 1 goto nopython
%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul
if errorlevel 1 goto nopython
%PY% -c "import fitz, google.genai, fontTools" >nul 2>nul
if errorlevel 1 goto nopackages

set /a OK=0
set /a NG=0

:loop
if "%~1"=="" goto done
echo.
echo ================================================================
echo  Translating: "%~nx1"
echo ================================================================
%PY% -m readable "%~f1" --out "%~dp1."
set RC=%errorlevel%
if %RC%==0 goto success
if %RC%==12 goto partial
set /a NG+=1
echo.
echo [FAILED] "%~nx1"  exit code %RC%
for %%C in (1 3 4 5 6 7 8 9 10 11) do if %RC%==%%C type "%MSG%\rc%%C.txt"
goto next

:partial
set /a OK+=1
echo.
echo [PARTIAL OK] "%~dpn1_ja.pdf"
echo [PARTIAL OK] "%~dpn1_dual.pdf"
type "%MSG%\rc12.txt"
goto next

:success
set /a OK+=1
echo.
echo [OK] "%~dpn1_ja.pdf"
echo [OK] "%~dpn1_dual.pdf"

:next
shift
goto loop

:done
echo.
echo ================================================================
echo  Result: success %OK% / failed %NG%
type "%MSG%\done.txt"
echo ================================================================
echo.
pause
exit /b %NG%

:usage
type "%MSG%\usage.txt"
pause
exit /b 1

:nopython
type "%MSG%\nopython.txt"
pause
exit /b 1

:nopackages
type "%MSG%\nopackages.txt"
pause
exit /b 1
