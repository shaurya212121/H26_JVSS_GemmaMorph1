@echo off
title GemmaMorph Mission Control UI
echo ===================================================
echo   Starting GemmaMorph UI on http://127.0.0.1:8765
echo ===================================================
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe ui.py --port 8765
) else (
    python ui.py --port 8765
)
pause
