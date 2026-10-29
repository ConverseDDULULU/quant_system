@echo off
title Quant System One-Click Launcher

cd /d "%~dp0"

echo ============================================================
echo [Quant System] Cleaning old background processes...
echo ============================================================
taskkill /F /IM python.exe 2>nul

echo ============================================================
echo [Quant System] Launcher Starting...
echo ============================================================

:: 1. FastAPI Backend (Port 8000)
start "Quant Backend" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src;. && .venv\Scripts\python.exe -m uvicorn api_server:app --port 8000"

:: 2. React Dashboard Frontend
start "Quant Frontend" cmd /k "cd /d ""%~dp0frontend"" && npm run dev -- --open"

:: 3. Quant Sentinel Trading Engine (?? ?? ??? ?? ????)
start "Quant Trading Engine" cmd /k "cd /d ""%~dp0"" && set PYTHONPATH=src;. && .venv\Scripts\python.exe src\quant_scheduler.py"

echo [Quant System] All systems successfully launched!
