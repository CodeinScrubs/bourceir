# -*- coding: utf-8 -*-
"""
نقطه ورود اصلی برای اجرای مستقیم داشبورد استریم‌لیت:
    streamlit run app.py
یا
    streamlit run dashboard/app.py
"""
import sys
from pathlib import Path

# افزودن مسیر ریشه پروژه به مسیر جستجوی پایتون
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# اجرای منطق داشبورد از پوشه dashboard
from dashboard.app import *
