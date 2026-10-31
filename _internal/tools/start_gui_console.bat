@echo off
setlocal
cd /d "%~dp0..\.."
rem Diagnostic launcher: intentionally opens a visible CMD with traceback/output.
py -3.11 -X utf8 "_internal\dnd_support.py"
py -3.11 -X utf8 "_internal\unified_gui.py"
echo.
echo GUI finished. Check the messages above if startup failed.
pause
