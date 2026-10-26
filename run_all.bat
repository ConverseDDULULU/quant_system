Set-Content -Path "run_all.bat" -Encoding Ascii -Value @'
@echo off
title Quant System One-Click Launcher

cd /d "%~dp0"

echo ============================================================
echo [Quant System] Launcher Starting...
echo ============================================================

start "Quant Backend" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src && .venv\Scripts\python.exe -m uvicorn api_server:app --port 8000"

start "Quant Frontend" cmd /k "cd /d ""%~dp0frontend"" && npm run dev -- --open"

echo [Quant System] Done!
'@