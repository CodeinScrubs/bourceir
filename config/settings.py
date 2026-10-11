# -*- coding: utf-8 -*-
"""
تنظیمات مرکزی سامانه BRS Quant Engine
پشتیبانی از لود داینامیک پیکربندی از فایل‌های YAML (default.yaml و production.yaml)
و حفظ سازگاری کامل با تمامی متغیرهای پیشین.
"""

import os
from pathlib import Path
from typing import Dict, Any

try:
    import yaml
except ImportError:
    yaml = None

# مسیر ریشه پروژه (پوشه اصلی)
BASE_DIR = Path(__file__).resolve().parent.parent

# تعیین محیط اجرا (development یا production)
ENV = os.getenv("BRS_ENV", "development").lower()

CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / ("production.yaml" if ENV == "production" else "default.yaml")

# مقادیر پیش‌فرض هسته
PROJECT_NAME = "BRS Quant Engine"
VERSION = "0.4.1"
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
DB_PATH = DATA_DIR / "brs.db"

# اطمینان از وجود پوشه‌های محلی
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# مقادیر پیش‌فرض
TIMEZONE = "Asia/Tehran"
PRICE_UNIT = "Rial"
TEST_SYMBOLS = ["خودرو", "فولاد", "شپنا", "وبملت", "کگل"]
POLLING_INTERVAL_SECONDS = 30
MAX_RETRIES = 3
RETRY_DELAY_SECONDS = 5
RATE_LIMIT_DELAY = 1.0
STALE_DATA_THRESHOLD_SECONDS = 90

TRADING_HOURS = {
    "start": "09:00",
    "end": "12:30",
    "pre_market_start": "08:45"
}
TRADING_DAYS = [5, 6, 0, 1, 2] # شنبه تا چهارشنبه

BACKTEST_CONFIG = {
    "commission_buy": 0.003712,
    "commission_sell": 0.008800,
    "default_slippage_bps": 10.0,
    "max_volume_participation_pct": 0.05,
    "initial_capital": 100_000_000.0
}

# بارگذاری تنظیمات از فایل YAML در صورت وجود
if yaml and CONFIG_FILE.exists():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}

        if "project" in cfg:
            PROJECT_NAME = cfg["project"].get("name", PROJECT_NAME)
            VERSION = cfg["project"].get("version", VERSION)

        if "market" in cfg:
            m = cfg["market"]
            TIMEZONE = m.get("timezone", TIMEZONE)
            PRICE_UNIT = m.get("price_unit", PRICE_UNIT)
            if "trading_hours" in m:
                TRADING_HOURS.update(m["trading_hours"])
            if "trading_days" in m:
                TRADING_DAYS = m["trading_days"]

        if "data_collection" in cfg:
            dc = cfg["data_collection"]
            POLLING_INTERVAL_SECONDS = dc.get("polling_interval_seconds", POLLING_INTERVAL_SECONDS)
            MAX_RETRIES = dc.get("max_retries", MAX_RETRIES)
            RETRY_DELAY_SECONDS = dc.get("retry_delay_seconds", RETRY_DELAY_SECONDS)
            RATE_LIMIT_DELAY = dc.get("rate_limit_delay", RATE_LIMIT_DELAY)
            STALE_DATA_THRESHOLD_SECONDS = dc.get("stale_data_threshold_seconds", STALE_DATA_THRESHOLD_SECONDS)
            if "symbols" in dc:
                TEST_SYMBOLS = dc["symbols"]

        if "backtest" in cfg:
            BACKTEST_CONFIG.update(cfg["backtest"])

    except Exception as e:
        # در صورت بروز خطا در خواندن YAML، از مقادیر پیش‌فرض استفاده می‌شود
        pass
