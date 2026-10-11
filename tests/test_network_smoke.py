# -*- coding: utf-8 -*-
"""
آزمون‌های دود (Smoke) با اینترنت واقعی — در CI اجرا نمی‌شوند.

اجرای دستی:  python -m pytest -m network
"""

import pytest

pytestmark = pytest.mark.network


def test_tsetmc_market_watch_live():
    from data_collector.tsetmc import TsetmcProvider

    snaps = TsetmcProvider().get_market_snapshots()
    assert snaps and len(snaps) > 500
    foolad = next(s for s in snaps if s["symbol_fa"] == "فولاد")
    assert foolad["price_limit_up"] > foolad["price_limit_down"] > 0


def test_tsetmc_history_live():
    from data_collector.tsetmc import TsetmcProvider

    df = TsetmcProvider().get_symbol_history("46348559193224090", last_n=10)
    assert df is not None and len(df) >= 5 and df["ref_price_raw"].gt(0).all()


def test_codal_live():
    from data_collector.codal import CodalProvider

    assert CodalProvider().get_letters(page=1)


def test_jev_live():
    from ai.jev import JevClient

    jev = JevClient()
    if not jev.available:
        pytest.skip("کلید TYPESAFE_API_KEY تنظیم نشده است")
    assert jev.ping()
