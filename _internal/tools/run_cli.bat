@echo off
setlocal
cd /d "%~dp0..\.."
py -3.11 "_internal\cli.py" %*
if errorlevel 1 pause
