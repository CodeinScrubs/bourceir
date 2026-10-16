"""Preflight is read-only, scoped to ordinary shares and development dates."""

import json
import sys

import pandas as pd
import pytest

from config import settings
from ops.research_preflight import research_preflight
from tests.test_client_quality import coherent_history


def store(db, symbol='آزمون', kind=300):
    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=symbol, instrument_type_code=kind))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    return prices, clients, sid


def test_empty_preflight_is_not_a_pass(db):
    result = research_preflight(db)
    assert result['price_rows'] == 0 and result['no_raw_price_errors'] is False
    assert 'no_development_price_rows' in result['blockers']
    assert result['accepted_for_signals'] is False


def test_future_changes_do_not_change_development_data_report_or_input_hash(db):
    prices, clients, sid = store(db)
    before = research_preflight(db)
    prices.loc[:, 'date'] = '2026-10-15'
    clients.loc[:, 'date'] = '2026-10-15'
    prices = prices.head(1).copy()
    clients = clients.head(1).copy()
    prices.loc[:, 'ref_price_raw'] = 0.
    clients.loc[:, 'buy_i_value'] = 99_000_000.
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    changes = db.conn.total_changes
    after = research_preflight(db)
    assert changes == db.conn.total_changes and not db.conn.in_transaction
    for key in ('records', 'raw_inputs', 'price_rows', 'coherent_client_pairs', 'unknown_client_pairs', 'blockers'):
        assert before[key] == after[key]
    assert after['price_rows'] == 25 and after['no_raw_price_errors']
    assert all(r['date_max'] < settings.RESEARCH_CONFIG['holdout_start'] for r in after['records'])


def test_invalid_prices_and_missing_clients_are_reported_without_zero_imputation(db):
    prices, clients, sid = store(db)
    prices.loc[24, 'ref_price_raw'] = 0.
    clients = clients.iloc[:20]
    with db.conn:
        db.conn.execute('DELETE FROM client_types_daily')
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    store(db, symbol='صندوقآزمون', kind=305)
    result = research_preflight(db)
    assert result['universe_count'] == 1 and result['price_rows'] == 25
    assert result['records'][0]['invalid_price_rows'] == 1
    assert result['unknown_client_pairs'] == 5 and result['coherent_client_pairs'] == 20
    assert not result['no_raw_price_errors']
    assert 'invalid_development_prices' in result['blockers']
    assert result['profitability_proven'] is False
    json.dumps(result, allow_nan=False)


def test_preflight_cannot_move_cutoff_past_the_seal(db):
    with pytest.raises(ValueError):
        research_preflight(db, before='2099-01-01')


def test_environment_error_blocks_cli_even_with_coherent_prices(db, tmp_path, monkeypatch):
    from ops.research_preflight import main

    store(db)
    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    output = tmp_path / 'private' / 'preflight.json'
    monkeypatch.setattr(sys, 'argv', ['preflight', '--json', str(output)])

    def mismatch():
        raise ValueError('incompatible numeric environment')

    monkeypatch.setattr('ops.research_preflight.feature_environment', mismatch)
    assert main() == 2
    report = json.loads(output.read_text(encoding='utf-8'))
    assert report['no_raw_price_errors'] and not report['input_contract_passed']
    assert report['status'] == 'blocked' and 'environment_mismatch' in report['blockers']


@pytest.mark.parametrize('cutoff', ['', '2024-1-1', '20240101', '2024-02-30', 20240101])
def test_preflight_requires_a_canonical_real_cutoff(db, cutoff):
    with pytest.raises(ValueError):
        research_preflight(db, before=cutoff)


def test_bad_price_date_is_explicit_and_unknown_pairs_stay_in_denominator(db):
    _, _, sid = store(db)
    with db.conn:
        db.conn.execute("UPDATE daily_prices SET date='2024-01-32' WHERE symbol_id=? AND date='2024-01-01'", (sid,))
    report = research_preflight(db)
    assert report['records'][0]['invalid_price_dates'] == 1
    assert not report['input_contract_passed']
    assert report['unknown_client_pairs'] + report['coherent_client_pairs'] == report['price_rows'] == 25


def test_duplicate_clients_are_reported_instead_of_aborting_or_imputing(db, monkeypatch):
    _, clients, _ = store(db)
    monkeypatch.setattr(db, 'get_client_types_df', lambda *a, **k: pd.concat([clients, clients.head(1)]))
    report = research_preflight(db)
    assert report['records'][0]['duplicate_client_dates'] == 1
    assert report['unknown_client_pairs'] == 25 and not report['input_contract_passed']


def test_missing_clients_do_not_claim_flow_readiness_or_block_price_only_contract(db):
    prices, _, sid = store(db)
    with db.conn:
        db.conn.execute('DELETE FROM client_types_daily WHERE symbol_id=?', (sid,))
    report = research_preflight(db)
    assert report['input_contract_passed']
    assert not report['client_value_pairs_complete'] and report['unknown_client_pairs'] == len(prices)
