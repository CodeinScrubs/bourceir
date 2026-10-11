# -*- coding: utf-8 -*-
"""
تنظیمات مرکزی سامانه BRS Quant Engine

ترتیب بارگذاری:
1. config/default.yaml (همیشه)
2. config/production.yaml روی آن ادغام می‌شود (فقط وقتی BRS_ENV=production)
3. مقادیر محرمانه از فایل .env (مانند TYPESAFE_API_KEY) — این فایل هرگز در git ثبت نمی‌شود

نام متغیرهای قبلی (PROJECT_NAME، TEST_SYMBOLS، BACKTEST_CONFIG و ...) برای سازگاری حفظ شده‌اند.
"""

import logging
import os
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

logger = logging.getLogger(__name__)

# مسیر ریشه پروژه (پوشه اصلی)
BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"

# بارگذاری متغیرهای محرمانه از .env (بدون بازنویسی متغیرهای محیطی موجود)
if load_dotenv is not None:
    load_dotenv(BASE_DIR / ".env", override=False)

# تعیین محیط اجرا (development یا production)
ENV = os.getenv("BRS_ENV", "development").lower()


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """ادغام بازگشتی دو دیکشنری؛ مقادیر override اولویت دارند."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_yaml(path: Path) -> Dict[str, Any]:
    if yaml is None or not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("خطا در خواندن فایل تنظیمات %s: %s — از مقادیر پیش‌فرض استفاده می‌شود.", path, e)
        return {}


CONFIG: Dict[str, Any] = _load_yaml(CONFIG_DIR / "default.yaml")
if ENV == "production":
    CONFIG = _deep_merge(CONFIG, _load_yaml(CONFIG_DIR / "production.yaml"))

_project = CONFIG.get("project", {})
_market = CONFIG.get("market", {})
_universe = CONFIG.get("universe", {})
_dc = CONFIG.get("data_collection", {})
_fees = CONFIG.get("fees", {})
_bt = CONFIG.get("backtest", {})
_ai = CONFIG.get("ai", {})

# ─── پروژه و مسیرها ───
PROJECT_NAME = _project.get("name", "BRS Quant Engine")
VERSION = str(_project.get("version", "0.5.0"))
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
# امکان تغییر مسیر دیتابیس با متغیر محیطی (برای تست‌ها و محیط‌های جدا)
DB_PATH = Path(os.getenv("BRS_DB_PATH", str(DATA_DIR / "brs.db")))

DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ─── بازار ───
TIMEZONE = _market.get("timezone", "Asia/Tehran")
PRICE_UNIT = _market.get("price_unit", "Rial")
TRADING_HOURS = {"start": "09:00", "end": "12:30", "pre_market_start": "08:45"}
TRADING_HOURS.update(_market.get("trading_hours", {}))
TRADING_DAYS = list(_market.get("trading_days", [5, 6, 0, 1, 2]))  # شنبه تا چهارشنبه

# ─── دامنه نمادها (فقط سهام بورس و فرابورس) ───
UNIVERSE = {
    "instrument_types": list(_universe.get("instrument_types", [300, 303])),
    "include_base_market": bool(_universe.get("include_base_market", False)),
    "include_sme_market": bool(_universe.get("include_sme_market", False)),
    "exclude_block_boards": bool(_universe.get("exclude_block_boards", True)),
}

# ─── گردآوری داده ───
PRIMARY_SOURCE = _dc.get("primary_source", "tsetmc")
TEST_SYMBOLS = list(_dc.get("symbols", ["خودرو", "فولاد", "شپنا", "وبملت", "کگل"]))
POLLING_INTERVAL_SECONDS = _dc.get("polling_interval_seconds", 30)
MAX_RETRIES = int(_dc.get("max_retries", 3))
RETRY_DELAY_SECONDS = _dc.get("retry_delay_seconds", 5)
RATE_LIMIT_DELAY = float(_dc.get("rate_limit_delay", 0.5))
REQUEST_TIMEOUT_SECONDS = float(_dc.get("request_timeout_seconds", 30))
STALE_DATA_THRESHOLD_SECONDS = _dc.get("stale_data_threshold_seconds", 90)

# ─── کارمزدها (منبع یکتا برای کل سامانه) ───
FEES = {
    "buy": float(_fees.get("buy", 0.003712)),
    "sell": float(_fees.get("sell", 0.008800)),
}

# ─── بکتست ───
BACKTEST_CONFIG = {
    # کلیدهای commission_* برای سازگاری با نسخه‌های قبلی نگه داشته شده‌اند
    "commission_buy": FEES["buy"],
    "commission_sell": FEES["sell"],
    "default_slippage_bps": float(_bt.get("default_slippage_bps", 10.0)),
    "max_volume_participation_pct": float(_bt.get("max_volume_participation_pct", 0.05)),
    "initial_capital": float(_bt.get("initial_capital", 100_000_000.0)),
    "execution_price": _bt.get("execution_price", "next_day_vwap"),
    "lock_min_move_pct": float(_bt.get("lock_min_move_pct", 0.015)),
}

# ─── هوش مصنوعی (TypeSafe Jev) ───
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()
JEV_MODEL = _ai.get("jev_model", "jev-1.13.0")
JEV_TIMEOUT_SECONDS = float(_ai.get("jev_timeout_seconds", 30))
