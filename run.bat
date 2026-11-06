@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" "%~dp0flash.py" %*
) else (
  py -3 "%~dp0flash.py" %*
)
set "RESULT=%ERRORLEVEL%"
if "%~1"=="" pause
exit /b %RESULT%
