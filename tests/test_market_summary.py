"""جمع نامعلوم، پوشش و تفاوت کندل تک‌قیمت با صف واقعی در خانه."""

import numpy as np

from core.market_summary import summarize_daily_market
from tests.test_client_quality import coherent_history


def test_home_retains_missing_symbols_and_unknown_returns():
    prices, clients = coherent_history()
    frame = prices.head(2).merge(clients, on='date')
    frame.loc[0, 'ref_price_raw'] = np.nan
    result = summarize_daily_market(frame, expected_count=5)
    assert result['count'] == 2 and result['missing_count'] == 3
    assert result['return_unknown_count'] == 1
    assert result['return_coverage'] == 1
    assert result['lock_classification'] == 'one_price_proxy_not_verified_order_queue'


def test_home_unknown_or_inconsistent_value_is_not_zero_or_valid_flow():
    prices, clients = coherent_history()
    frame = prices.head(2).merge(clients, on='date')
    for bad in (np.nan, -100, 1e100):
        frame['value'] = bad
        result = summarize_daily_market(frame, expected_count=2)
        assert result['value'] is None and result['value_coverage'] == 0
        assert result['real_flow'] is None and result['flow_coverage'] == 0


def test_doctor_does_not_accept_four_day_window_instead_of_expected_session(db, monkeypatch):
    from datetime import datetime

    import pandas as pd

    from config import settings
    from core.timeutils import TEHRAN_TZ
    from ops.doctor import DoctorReport, check_database
    from tests.test_market_review import closing

    sid = db.upsert_symbol(dict(symbol_fa='کیان', ins_code='1', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, pd.DataFrame([closing(date='2026-10-14')['candle']]))
    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    monkeypatch.setattr('core.timeutils.now_tehran', lambda: datetime(2026, 10, 17, 15, tzinfo=TEHRAN_TZ))
    report = DoctorReport()
    check_database(report, read_only=True)
    result = next(row for row in report.checks if row.title == 'تازگی داده روزانه')
    assert result.status == 'warn' and '2026-10-17' in result.detail
    db.close()


def test_doctor_weekend_calendar_and_future_rows_have_explicit_status(db, monkeypatch):
    from datetime import datetime

    import pandas as pd

    from config import settings
    from core.timeutils import TEHRAN_TZ
    from ops.doctor import DoctorReport, check_database
    from tests.test_market_review import closing

    sid = db.upsert_symbol(dict(symbol_fa='کیان', ins_code='1', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, pd.DataFrame([closing(date='2026-10-14')['candle']]))
    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    monkeypatch.setattr('core.timeutils.now_tehran', lambda: datetime(2026, 10, 16, 15, tzinfo=TEHRAN_TZ))
    report = DoctorReport()
    check_database(report, read_only=True)
    assert next(row for row in report.checks if row.title == 'تازگی داده روزانه').status == 'ok'
    db.upsert_daily_prices_df(sid, pd.DataFrame([closing(date='2026-10-18')['candle']]))
    report = DoctorReport()
    check_database(report, read_only=True)
    result = next(row for row in report.checks if row.title == 'تازگی داده روزانه')
    assert result.status == 'warn' and 'ردیف تاریخ آینده 1' in result.detail
