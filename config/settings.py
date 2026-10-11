# -*- coding: utf-8 -*-
"""
تنظیمات مرکزی سامانه BRS Data Engine
پروژه جمع‌آوری، ذخیره‌سازی و تحلیل داده‌های بورس اوراق بهادار تهران (TSE) و فرابورس (IFB)
"""

from pathlib import Path

# مسیر ریشه پروژه (پوشه اصلی)
BASE_DIR = Path(__file__).resolve().parent.parent

# نام و نسخه پروژه
PROJECT_NAME = 'BRS Data Engine'
VERSION = '0.1'

# مسیرهای داده و لاگ
DATA_DIR = BASE_DIR / 'data'
LOG_DIR = BASE_DIR / 'logs'
DB_PATH = DATA_DIR / 'brs.db'

# اطمینان از وجود پوشه‌های مورد نیاز در زمان لود تنظیمات
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# منطقه زمانی بازار سرمایه ایران
TIMEZONE = 'Asia/Tehran'

# واحد ارزی: تمام قیمت‌ها به ریال ذخیره می‌شوند
PRICE_UNIT = 'Rial'

# نمادهای آزمایشی اولیه برای بررسی سامانه
TEST_SYMBOLS = ['خودرو', 'فولاد', 'شپنا', 'وبملت', 'کگل']

# تنظیمات زمان‌بندی و پایش اطلاعات
POLLING_INTERVAL_SECONDS = 60
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
RATE_LIMIT_DELAY = 1.0  # فاصله بین درخواست‌ها به ثانیه جهت رعایت سقف مجاز سرورها

# ساعات رسمی معاملات بازار سهام تهران (شنبه تا چهارشنبه)
TRADING_HOURS = {
    'start': '09:00',
    'end': '12:30'
}

# روزهای کاری بورس تهران: شنبه=5، یکشنبه=6، دوشنبه=0، سه‌شنبه=1، چهارشنبه=2 (مطابق با استانداردهای datetime پایتون)
TRADING_DAYS = [5, 6, 0, 1, 2]
