# -*- coding: utf-8 -*-
"""
نقطه ورود جایگزین داشبورد:  streamlit run app.py
(روش اصلی: START.bat یا  streamlit run dashboard/app.py)

نکته: «from dashboard.app import *» فقط بار اول اجرا می‌شود (ماژول در حافظه می‌ماند) و صفحه در
بارگذاری‌های بعدی خالی نمایش داده می‌شد؛ runpy اسکریپت داشبورد را در هر اجرا دوباره اجرا می‌کند.
"""

import runpy
from pathlib import Path

runpy.run_path(str(Path(__file__).resolve().parent / "dashboard" / "app.py"), run_name="__main__")
