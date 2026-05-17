@echo off
REM KGI City-Hall Tracker — daily run
setlocal
set PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe
set ROOT=%~dp0
cd /d "%ROOT%"
"%PY%" "%ROOT%backend\run_daily.py" %*
endlocal
