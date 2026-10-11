@echo off
chcp 65001 > nul
title BRS Live Market Runner v0.4.1

echo ==============================================================================
echo   🚀 BRS Quant Engine — Live Market Session Runner v0.4.1
echo   سامانه پایش زنده و برخط فید بازار بورس تهران
echo ==============================================================================
echo.

set PYTHONIOENCODING=utf-8

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

python run_live_session.py --interval 30 --force

pause
