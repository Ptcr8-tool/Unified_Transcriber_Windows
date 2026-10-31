@echo off
setlocal
cd /d "%~dp0..\.."
for %%A in (openai-res faster-res faster-pya qwen-res qwen-pya) do (
    echo ===== %%A =====
    py -3.11 "_internal\setup_backend.py" --profile %%A --hardware cpu
    if errorlevel 1 goto :failed
)
echo All 5 profiles configured for CPU.
pause
exit /b 0
:failed
echo Installation failed. Check the error above.
pause
exit /b 1
