import hashlib
import json
import sqlite3
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from ops.historical_universe_audit import run
from ops.research_review import sha256
from ops.rights_daily_universe import capture, create_plan, day_request
from tests.test_rights_daily_universe import observation

DAY = '2024-01-01'
NEXT = '2024-01-02'
ISIN = 'IRO1TEST0001'


def make_registry(path, rows, *, add_prices=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE symbols (id, ins_code TEXT, tse_code, symbol_fa, instrument_type_code)')
        conn.executemany('INSERT INTO symbols VALUES (?,?,?,?,?)', rows)
        if add_prices:
            conn.execute('CREATE TABLE daily_prices (date, symbol_id, source, price_unit, volume, trade_count)')
            conn.executemany('INSERT INTO daily_prices VALUES (?,?,?,?,?,?)',
                             [(DAY,1,'tsetmc','Rial',1,1),(NEXT,1,'tsetmc','Rial',2,1),('2025-03-21',1,'tsetmc','Rial',1,1)])


def test_historical_coverage_binds_exact_identities_and_keeps_empty_days_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'market.db')
    backup = tmp_path / 'backups' / 'frozen.db'
    make_registry(backup, [(1,'123',ISIN,'قدیمی',300)])
    make_registry(settings.DB_PATH, [(1,'123',ISIN,'جدید',300),(2,'456','IRO3TEST0001','شناسه دیگر',303)], add_prices=True)
    frozen_run = tmp_path / 'research' / 'test'
    frozen_run.mkdir(parents=True)
    prices = frozen_run / 'prices.parquet'
    pd.DataFrame([dict(symbol='قدیمی',date=DAY,volume=1)]).to_parquet(prices)
    manifest = frozen_run / 'manifest.json'
    manifest.write_text(json.dumps(dict(exclusive_end_date='2024-01-03',raw_prices_sha256=sha256(prices))), encoding='utf-8')
    source = create_plan(DAY, '2024-01-03', tmp_path / 'private' / 'archive')

    class Source:
        def get_historical_day_universe_observation(self, day):
            assert day_request(day).endswith(day.replace('-', ''))
            rows = [dict(insCode=123,instrumentID=ISIN,lVal18AFC='تغییرنام',dEven=0),
                    dict(insCode=456,instrumentID='IRO3TEST0002',lVal18AFC='شناسه دیگر',dEven=0),
                    dict(insCode=789,instrumentID='IRO9TEST0011',lVal18AFC='جدید',dEven=0)] if day == DAY else []
            return observation(day, rows)

    assert capture(Source(), source)['new_attempts'] == 2
    original = hashlib.sha256(Path(settings.DB_PATH).read_bytes()).hexdigest()
    folder = tmp_path / 'private' / 'audit'
    result = run(source, frozen_run, backup, sha256(backup), folder)
    assert result['source_status'] == {'observed':1,'observed_empty_unverified':1}
    assert result['coverage_totals']['current'] == {'ordinary_price_present':1,
        'code_with_different_or_missing_isin':1,'unknown_identity':1}
    assert result['coverage_totals']['frozen'] == {'ordinary_price_present':1,'unknown_identity':2}
    assert result['yearly']['2024']['source_without_rows_on_local_positive_day'] == 1
    assert result['yearly']['2024']['local_positive_prices'] == 2
    assert result['native_dated_nonempty_days'] == 0 and result['historical_membership_verified'] is False
    assert len(result['outputs']) == 2 and sha256(settings.DB_PATH) == original

    pd.DataFrame([dict(symbol='قدیمی',date='2025-03-21',volume=1)]).to_parquet(prices)
    manifest.write_text(json.dumps(dict(exclusive_end_date='2024-01-03',raw_prices_sha256=sha256(prices))), encoding='utf-8')
    with pytest.raises(ValueError, match='مهروموم'):
        run(source, frozen_run, backup, sha256(backup), tmp_path / 'private' / 'future')
