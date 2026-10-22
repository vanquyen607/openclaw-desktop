@echo off
setlocal
set "PY=%USERPROFILE%\.venv\Scripts\pythonw.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\.venv\Scripts\python.exe"
start "" "%PY%" "%~dp0app.py"
endlocal
