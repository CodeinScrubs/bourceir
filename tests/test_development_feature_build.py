"""ساخت پژوهشی نباید حتی برای گرم‌کردن فیچر دوره مهر و موم را بخواند."""

import pandas as pd

from config import settings
from features.store import build_feature_store, load_feature_snapshot


def test_development_build_reads_price_and_client_rows_only_before_seal(db, tmp_path, monkeypatch):
    sid = db.upsert_symbol(dict(symbol_fa='الف', ins_code='123', instrument_type_code=300))
    frame = pd.DataFrame(dict(date=['2024-01-01', '2026-01-01'], open_raw=[100., 123456.], high_raw=[101., 123456.],
        low_raw=[99., 123456.], close_raw=[100., 123456.], close_final_raw=[100., 123456.],
        ref_price_raw=[100., 123456.], volume=[100, 100], value=[10000., 12345600.]))
    db.upsert_daily_prices_df(sid, frame)
    prices, clients = db.get_daily_prices_df, db.get_client_types_df
    calls = []

    def price_read(symbol, **kwargs):
        calls.append(kwargs['end_date'])
        return prices(symbol, **kwargs)

    def client_read(symbol, **kwargs):
        calls.append(kwargs['end_date'])
        return clients(symbol, **kwargs)

    monkeypatch.setattr(db, 'get_daily_prices_df', price_read)
    monkeypatch.setattr(db, 'get_client_types_df', client_read)
    manifest = build_feature_store(db, root=tmp_path, before=settings.RESEARCH_CONFIG['holdout_start'])
    assert calls == ['2025-03-20', '2025-03-20']
    result, _ = load_feature_snapshot(root=tmp_path)
    assert result.date.tolist() == ['2024-01-01']
    assert manifest['exclusive_end_date'] == '2025-03-21'
    assert all(day < '2025-03-21' for partitions in manifest['provenance']['raw_inputs']['tables'].values() for day in partitions
               if day[:4].isdigit())
