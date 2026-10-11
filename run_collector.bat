@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
echo ===================================================
echo   اجرای گردآوری تاریخچه داده‌های بورس BRS Data Engine
echo ===================================================
call .\venv\Scripts\activate.bat
python run_collector.py --mode history
pause
