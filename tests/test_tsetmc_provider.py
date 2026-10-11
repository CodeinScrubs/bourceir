# -*- coding: utf-8 -*-
"""
آزمون‌های تامین‌کننده TSETMC با پاسخ‌های ضبط‌شده (بدون اینترنت)
"""

import math

import pytest

from data_collector.base import DAILY_COLUMNS
from data_collector.tsetmc import TsetmcProvider
from data_collector.universe import UniverseManager
from tests.conftest import FOOLAD_INS, FakeSession


def test_history_mapping_is_explicit_and_point_in_time(closing_price_daily):
    provider = TsetmcProvider(session=FakeSession({"GetClosingPriceDailyList": {"closingPriceDaily": closing_price_daily}}))
    df = provider.get_symbol_history(FOOLAD_INS)
    assert list(df.columns) == DAILY_COLUMNS
    assert df["date"].is_monotonic_increasing
    assert "2026-08-05" not in set(df["date"])  # روز توقف (حجم صفر) کندل ندارد
    row = df.set_index("date").loc["2026-10-05"]
    assert (row["close_raw"], row["close_final_raw"], row["ref_price_raw"]) == (3800, 3770, 3690)
    # آخرین معامله خارج از بازه روز: فقط همان فیلد خالی می‌شود، روز حذف نمی‌شود
    bad = df.set_index("date").loc["2026-10-04"]
    assert math.isnan(bad["close_raw"]) and bad["close_final_raw"] == 3690


def test_history_returns_none_on_server_error():
    provider = TsetmcProvider(session=FakeSession({}))
    assert provider.get_symbol_history(FOOLAD_INS) is None  # هیچ داده ساختگی تولید نمی‌شود


def test_universe_keeps_only_bourse_and_farabourse_stocks(market_watch_rows):
    provider = TsetmcProvider(session=FakeSession({"GetMarketWatch": {"marketwatch": market_watch_rows}}))
    universe = provider.get_stock_universe()
    assert [u["symbol_fa"] for u in universe] == ["فولاد", "خزامیا"]
    foolad, khazamia = universe
    assert foolad["market"] == "bourse" and foolad["ins_code"] == FOOLAD_INS and foolad["board_code"] == "N1"
    assert khazamia["market"] == "farabourse" and khazamia["instrument_type_code"] == 303


def test_base_market_can_be_enabled(monkeypatch, market_watch_rows):
    from config import settings

    monkeypatch.setitem(settings.UNIVERSE, "include_base_market", True)
    provider = TsetmcProvider(session=FakeSession({"GetMarketWatch": {"marketwatch": market_watch_rows}}))
    assert "لپیام" in [u["symbol_fa"] for u in provider.get_stock_universe()]


def test_snapshot_from_market_watch(market_watch_rows, client_type_all):
    snap = TsetmcProvider.snapshot_from_market_watch(market_watch_rows[0], client_type_all[FOOLAD_INS], "2026-10-11T12:30:00+03:30")
    assert snap["captured_at"] == "2026-10-11T12:30:00+03:30"
    assert (snap["price_limit_up"], snap["price_limit_down"], snap["ref_price"]) == (4220.0, 3980, 4100.0)
    assert (snap["bid_price_1"], snap["bid_vol_1"], snap["bid_count_1"]) == (4220.0, 3_837_639, 31)
    assert snap["ask_count_2"] == 0
    assert snap["buy_queue_volume"] == 3_837_639   # بهترین تقاضا روی سقف دامنه = صف خرید
    assert snap["sell_queue_volume"] == 0
    assert snap["real_buy_count"] == 11508 and snap["legal_sell_volume"] == 2_240_805_983.0
    assert snap["last_trade_time"] == "12:29:59"


def test_market_snapshots_combine_watch_and_client_types(market_watch_rows, client_type_all):
    session = FakeSession({
        "GetMarketWatch": {"marketwatch": market_watch_rows[:1]},
        "GetClientTypeAll": {"clientTypeAllDto": list(client_type_all.values())},
    })
    snaps = TsetmcProvider(session=session).get_market_snapshots()
    assert len(snaps) == 1 and snaps[0]["real_buy_volume"] == 5_781_283_335.0 and snaps[0]["captured_at"]


def test_client_types_history_mapping():
    payload = {"clientType": [{
        "recDate": 20261010, "insCode": FOOLAD_INS, "buy_I_Volume": 808443566.0, "buy_N_Volume": 115092117.0,
        "buy_I_Value": 3.3e12, "buy_N_Value": 4.7e11, "buy_N_Count": 75, "sell_I_Volume": 227155706.0,
        "buy_I_Count": 1062.0, "sell_N_Volume": 696379977.0, "sell_I_Value": 9.3e11, "sell_N_Value": 2.8e12,
        "sell_N_Count": 13, "sell_I_Count": 2002,
    }]}
    df = TsetmcProvider(session=FakeSession({"GetClientTypeHistory": payload})).get_client_types_history(FOOLAD_INS)
    row = df.iloc[0]
    assert row["date"] == "2026-10-10" and row["buy_i_count"] == 1062 and row["sell_n_volume"] == 696379977.0


def test_client_types_incremental_request_by_date():
    session = FakeSession({"GetClientTypeHistory": {"clientType": {"recDate": 20261010, "buy_I_Volume": 1.0}}})
    provider = TsetmcProvider(session=session)
    assert provider.get_client_types_history(FOOLAD_INS, dates=[]).empty
    assert session.calls == []  # فهرست خالی یعنی هیچ درخواستی لازم نیست (نه دانلود کل تاریخچه)
    df = provider.get_client_types_history(FOOLAD_INS, dates=["2026-10-10"])
    assert session.calls[-1].endswith(f"{FOOLAD_INS}/20261010") and len(df) == 1


@pytest.mark.parametrize("symbol,isin,expected", [
    ("فولاد", "IRO1FOLD0001", True),
    ("خودرو", "IRO1IKCO0001", True),
    ("خزامیاح", None, False),       # حق تقدم
    ("اخزا101", None, False),       # اسناد خزانه
    ("اهرم", None, False),          # صندوق
    ("دارا یکم", None, False),      # صندوق
    ("فولاد2", None, False),        # تابلوی بلوکی
    ("ضستا1069", None, False),      # اختیار معامله
])
def test_name_based_common_stock_heuristic(symbol, isin, expected):
    assert UniverseManager.is_common_stock(symbol, isin) is expected
