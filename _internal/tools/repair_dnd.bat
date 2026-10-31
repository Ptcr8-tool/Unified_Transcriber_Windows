@echo off
setlocal
cd /d "%~dp0..\.."
echo ========================================================
echo     Explorer Drag and Drop - diagnostics and repair
echo ========================================================
echo.
py -3.11 -X utf8 "%~dp0..\dnd_support.py" --install-and-check --repair
if errorlevel 1 goto failed
echo.
echo SUCCESS: Close the GUI and run start_gui.vbs again.
echo Note: Windows Explorer and GUI need the same privilege level.
pause
exit /b 0
:failed
echo.
echo FAILED: See the error shown above. No pip settings were changed.
echo Check Python 3.11, internet access, write permission, and TkDND.
pause
exit /b 1
