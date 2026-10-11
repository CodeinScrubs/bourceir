# -*- coding: utf-8 -*-
"""
پیکربندی مشترک آزمون‌ها

داده‌های نمونه زیر عیناً از ساختار پاسخ‌های واقعی TSETMC (اکتبر ۲۰۲۶) برداشته شده‌اند تا آزمون‌ها
بدون اینترنت و قطعی اجرا شوند. این داده‌ها فقط در آزمون استفاده می‌شوند و هرگز وارد دیتابیس اصلی نمی‌شوند.
"""

import sys
from pathlib import Path

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from database.db import DatabaseManager  # noqa: E402

FOOLAD_INS = "46348559193224090"


def _daily(deven, first, low, high, last, closing, yesterday, volume, value=None, count=100):
    return {
        "priceChange": 0.0, "priceMin": low, "priceMax": high, "priceYesterday": yesterday, "priceFirst": first,
        "last": False, "id": 0, "insCode": FOOLAD_INS, "dEven": deven, "hEven": 122949, "pClosing": closing,
        "iClose": False, "yClose": True, "pDrCotVal": last, "zTotTran": count, "qTotTran5J": volume,
        "qTotCap": value if value is not None else volume * closing,
    }


@pytest.fixture
def closing_price_daily():
    """تاریخچه روزانه فولاد (نزولی، مثل پاسخ واقعی) شامل: روز توقف، روز افزایش سرمایه، صف خرید قفل و فیلد خراب."""
    return [
        _daily(20261010, 4100, 4100, 4100, 4100, 4100, 3990, 923_535_683),      # صف خرید قفل
        _daily(20261007, 3990, 3990, 3990, 3990, 3990, 3880, 1_581_454_223),    # صف خرید قفل
        _daily(20261006, 3880, 3880, 3880, 3880, 3880, 3770, 2_365_867_335),    # صف خرید قفل
        _daily(20261005, 3690, 3590, 3800, 3800, 3770, 3690, 4_227_500_464),
        _daily(20261004, 3680, 3600, 3810, 3820, 3690, 3710, 4_528_850_916),    # آخرین معامله خارج از بازه!
        _daily(20261003, 3720, 3630, 3720, 3710, 3710, 3620, 3_000_000_000),
        _daily(20260805, 0, 0, 0, 2549, 2549, 2549, 0),                          # توقف نماد (بدون معامله)
        _daily(20260804, 2600, 2580, 2620, 2604, 2604, 2590, 900_000_000),
        _daily(20250312, 4000, 3950, 4100, 4050, 4000, 3982, 800_000_000),      # روز اقدام شرکتی
        _daily(20250311, 5500, 5450, 5560, 5530, 5530, 5480, 700_000_000),
    ]


@pytest.fixture
def market_watch_rows():
    """چند ردیف دیده‌بان بازار: سهم بورس، سهم فرابورس، تابلوی بلوکی، صندوق، بازار پایه."""
    def row(lva, ins, y_val, flow, board, **kw):
        base = {
            "lva": lva, "lvc": f"شركت {lva}", "insCode": ins, "insID": f"IRO1{ins[:4]}0001", "yVal": y_val,
            "flow": flow, "cGrValCot": board, "csv": "27 ", "ztd": 1e9, "bv": 1.0, "hEven": 122959, "dEven": 20261011,
            "pdv": 4220.0, "pcl": 4180.0, "pf": 4220.0, "pmx": 4220.0, "pmn": 4000.0, "py": 4100.0,
            "pMax": 4220.0, "pMin": 3980, "qtj": 8_380_424_275.0, "qtc": 35_038_504_504_210.0, "ztt": 79969.0,
            "blDs": [
                {"n": 1, "qmd": 3_837_639, "zmd": 31, "pmd": 4220.0, "pmo": 4220.0, "zmo": 3, "qmo": 900_000},
                {"n": 2, "qmd": 1_226_602, "zmd": 20, "pmd": 4210.0, "pmo": 0.0, "zmo": 0, "qmo": 0},
            ],
        }
        base.update(kw)
        return base

    return [
        row("فولاد", FOOLAD_INS, 300, 1, "N1"),
        row("خزاميا", "11111111111111111", 303, 2, "Z1"),
        row("فولاد2", "22222222222222222", 300, 1, "F3"),
        row("اهرم", "33333333333333333", 305, 1, "51"),
        row("لپيام", "44444444444444444", 309, 4, "P1"),
    ]


@pytest.fixture
def client_type_all():
    return {
        FOOLAD_INS: {
            "insCode": FOOLAD_INS, "buy_I_Volume": 5_781_283_335.0, "buy_N_Volume": 2_599_140_940.0,
            "buy_CountI": 11508, "buy_CountN": 175, "sell_I_Volume": 6_139_618_292.0,
            "sell_N_Volume": 2_240_805_983.0, "sell_CountI": 9482, "sell_CountN": 104,
        }
    }


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """جایگزین requests.Session: هر URL را با اولین الگوی منطبق در routes پاسخ می‌دهد."""

    def __init__(self, routes):
        self.routes = routes
        self.headers = {}
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for pattern, payload in self.routes.items():
            if pattern in url:
                return FakeResponse(payload)
        return FakeResponse({}, status=404)


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """بدون مکث بین درخواست‌ها در آزمون‌ها."""
    from config import settings

    monkeypatch.setattr(settings, "RATE_LIMIT_DELAY", 0.0)


@pytest.fixture
def db(tmp_path):
    database = DatabaseManager(db_path=tmp_path / "test_brs.db")
    yield database
    database.close()
