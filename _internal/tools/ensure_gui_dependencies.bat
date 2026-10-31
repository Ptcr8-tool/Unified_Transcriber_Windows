@echo off
setlocal
cd /d "%~dp0..\.."
rem Pinned SHA-256 wheel installer implemented with Python's standard library.
rem No pip: avoids --user / --target config conflicts on Windows.
py -3.11 -X utf8 "%~dp0..\dnd_support.py" --install-and-check
exit /b %errorlevel%
