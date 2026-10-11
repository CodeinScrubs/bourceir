@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
echo ===================================================
echo   اجرای داشبورد BRS Data Engine v0.1
echo ===================================================
call .\venv\Scripts\activate.bat
streamlit run dashboard/app.py
pause
