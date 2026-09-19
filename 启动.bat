@echo off
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\admin\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

echo Starting service, please wait...
start "" /b "%PYTHON_EXE%" web_app.py

rem Wait until backend is ready (max ~90s), then open browser
set /a TRIES=0
:wait
set /a TRIES+=1
if %TRIES% GTR 90 (
  echo Service start timeout, check console output for errors.
  start "" "http://127.0.0.1:8765"
  exit /b 1
)
timeout /t 1 /nobreak >nul
curl -s -o nul -w "%%{http_code}" "http://127.0.0.1:8765/api/ping" | findstr "200" >nul
if errorlevel 1 goto wait

echo Service ready, opening browser...
start "" "http://127.0.0.1:8765"
