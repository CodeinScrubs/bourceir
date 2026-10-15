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
import math
import os
import re
from datetime import date
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
if ENV not in ("development", "production"):
    raise ValueError(f"BRS_ENV باید development یا production باشد، نه «{ENV}»")


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
    """
    خواندن فایل تنظیمات. خطا = توقف برنامه با پیام روشن: اجرای بی‌صدا با مقادیر پیش‌فرض (مثلاً کارمزد اشتباه)
    خطرناک‌تر از اجرا نشدن است.
    """
    if yaml is None:
        raise RuntimeError("کتابخانه PyYAML نصب نیست؛ فایل SETUP.bat را اجرا کنید.")
    if not path.exists():
        raise FileNotFoundError(f"فایل تنظیمات پیدا نشد: {path}")
    with open(path, "r", encoding="utf-8") as f:
        value = yaml.safe_load(f)
    if not isinstance(value, dict) or not value:
        raise ValueError(f"ساختار فایل تنظیمات {path.name} نامعتبر است")
    return value


def _check_range(config: Dict[str, Any], section: str, key: str, low: float, high: float) -> None:
    value = config.get(section, {}).get(key)
    if value is None:
        return  # مقدار پیش‌فرض کد استفاده می‌شود
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or not low <= value <= high:
        raise ValueError(f"مقدار نامعتبر در تنظیمات: {section}.{key} = {value!r} (مجاز: {low} تا {high})")


def validate_config(config: Dict[str, Any]) -> None:
    """قیدهای حیاتی که اشتباه در آن‌ها نتایج بکتست و داده را بی‌صدا خراب می‌کند."""
    project = config.get('project')
    version = project.get('version') if isinstance(project, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('project.version باید نسخه معتبر پروژه باشد؛ نسخه پیش‌فرض ساختگی استفاده نمی‌شود')
    market = config.get("market", {})
    if market.get("timezone", "Asia/Tehran") != "Asia/Tehran" or market.get("price_unit", "Rial") != "Rial":
        raise ValueError("منطقه زمانی باید Asia/Tehran و واحد قیمت Rial باشد (قانون ۵ AGENTS.md)")
    universe = config.get("universe", {})
    if universe.get("instrument_types", [300, 303]) != [300, 303]:
        raise ValueError("دامنه پایه فقط سهام بورس و فرابورس است")
    for key in ("include_base_market", "include_sme_market", "exclude_block_boards"):
        if key in universe and not isinstance(universe[key], bool):
            raise ValueError(f"universe.{key} باید true یا false باشد")
    for key in ("buy", "sell"):
        _check_range(config, "fees", key, 0.0, 0.05)
    _check_range(config, "backtest", "max_volume_participation_pct", 1e-6, 0.05)   # قانون ۸
    _check_range(config, "backtest", "default_slippage_bps", 0, 1000)
    _check_range(config, "backtest", "initial_capital", 1, 1e15)
    _check_range(config, "backtest", "lock_min_move_pct", 0, 0.02)
    _check_range(config, "data_collection", "request_timeout_seconds", 1, 300)
    _check_range(config, "data_collection", "polling_interval_seconds", 5, 3600)
    _check_range(config, "data_collection", "rate_limit_delay", 0, 10)
    _check_range(config, "research", "top_k", 1, 50)
    _check_range(config, "research", "min_daily_value_rial", 0, 1e15)
    research = config.get("research", {})
    _check_range(config, "research", "random_trials", 1, 10000)
    for key in ("top_k", "random_trials"):
        if key in research and (isinstance(research[key], bool) or not isinstance(research[key], int)):
            raise ValueError(f"research.{key} باید عدد صحیح باشد")
    horizons = research.get("horizons", [1, 2, 3, 5])
    if not isinstance(horizons, list) or not horizons or any(
        isinstance(h, bool) or not isinstance(h, int) or not 1 <= h <= 20 for h in horizons
    ) or len(set(horizons)) != len(horizons):
        raise ValueError("افق‌های پژوهش باید اعداد صحیح مثبت و یکتا بین ۱ و ۲۰ باشند")
    dates = {}
    for key, default in (("dev_start", "2012-03-20"), ("holdout_start", "2025-03-21")):
        value = str(research.get(key, default))
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"تاریخ research.{key} نامعتبر است") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"قالب research.{key} باید YYYY-MM-DD باشد")
        dates[key] = parsed
    if dates["dev_start"] >= dates["holdout_start"]:
        raise ValueError("research.dev_start باید قبل از research.holdout_start باشد")
    for key in ("enforce_point_in_time", "prevent_lookahead_bias"):
        if config.get("governance", {}).get(key, True) is not True:
            raise ValueError(f"حفاظت {key} باید فعال باشد")



CONFIG: Dict[str, Any] = _load_yaml(CONFIG_DIR / "default.yaml")
if ENV == "production":
    CONFIG = _deep_merge(CONFIG, _load_yaml(CONFIG_DIR / "production.yaml"))
validate_config(CONFIG)

_project = CONFIG.get("project", {})
_market = CONFIG.get("market", {})
_universe = CONFIG.get("universe", {})
_dc = CONFIG.get("data_collection", {})
_fees = CONFIG.get("fees", {})
_bt = CONFIG.get("backtest", {})
_ai = CONFIG.get("ai", {})

# ─── پروژه و مسیرها ───
PROJECT_NAME = _project.get("name", "BRS Quant Engine")
VERSION = _project['version']
# امکان تغییر مسیر داده و لاگ با متغیر محیطی (برای CI و نصب‌های جدا)
DATA_DIR = Path(os.getenv("BRS_DATA_DIR", str(BASE_DIR / "data")))
LOG_DIR = Path(os.getenv("BRS_LOG_DIR", str(BASE_DIR / "logs")))
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
    "lock_min_move_pct": float(_bt.get("lock_min_move_pct", 0.005)),
}

# ─── پژوهش (فاز ۱-ج) ───
_rs = CONFIG.get("research", {})
RESEARCH_CONFIG = {
    "horizons": [int(h) for h in _rs.get("horizons", [1, 2, 3, 5])],
    "top_k": int(_rs.get("top_k", 5)),
    "min_daily_value_rial": float(_rs.get("min_daily_value_rial", 20_000_000_000)),
    "dev_start": str(_rs.get("dev_start", "2012-03-20")),
    "holdout_start": str(_rs.get("holdout_start", "2025-03-21")),
    "random_trials": int(_rs.get("random_trials", 100)),
}

# ─── هوش مصنوعی (TypeSafe Jev) ───
TYPESAFE_API_KEY = os.getenv("TYPESAFE_API_KEY", "").strip()
JEV_MODEL = _ai.get("jev_model", "jev-1.13.0")
JEV_TIMEOUT_SECONDS = float(_ai.get("jev_timeout_seconds", 30))
