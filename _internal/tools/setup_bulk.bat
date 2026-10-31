@echo off
setlocal
cd /d "%~dp0..\.."
rem Manual console command. For silent background operation use the GUI batch modal.
rem Example: setup_bulk.bat --preset all-cuda
py -3.11 -X utf8 -u "_internal\bulk_setup.py" %*
if errorlevel 1 (
    echo.
    echo Some steps failed; inspect messages above.
    pause
    exit /b 1
)
echo.
echo Setup finished.
pause
