"""پرونده واقعی‌نما فقط در DB موقت؛ ثبت پیشین و بازاجرای مستقل حسابداری."""

import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from research import cash_dividend_case as case
from tests.test_portfolio import history

pytest_plugins = ['tests.test_dividend_reconciliation']


@pytest.fixture
def case_inputs(db, archives):
    from ops.dividend_reconciliation import compare_archived_dividend

    compared = compare_archived_dividend(**archives)
    frame, _ = history(7, ('آزمون',))
    frame['date'] = pd.date_range('2023-07-17', periods=7).strftime('%Y-%m-%d')
    for column in ('open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value'):
        frame[column] *= 10
        frame.loc[frame.date >= '2023-07-21', column] *= .9
    frame = frame.loc[~frame.date.isin(['2023-07-19', '2023-07-20'])]
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, frame.drop(columns='symbol'))
    return dict(comparison=compared['folder'], decision_date='2023-07-17', record_date='2023-07-18',
                ex_date='2023-07-21', end_exclusive='2023-07-24', initial_capital_rial=10000)


def test_full_case_registers_before_evaluation_and_preserves_unknown_economic_return(db, case_inputs, monkeypatch):
    calls, original = [], db.log_experiment

    def log(**kwargs):
        calls.append(kwargs['strategy_name'])
        return original(**kwargs)

    compare = case._compare

    def watched(*args):
        assert calls == ['cash-dividend-accounting-protocol']
        return compare(*args)

    monkeypatch.setattr(db, 'log_experiment', log)
    monkeypatch.setattr(case, '_compare', watched)
    result = case.run_cash_dividend_case(db, **case_inputs)
    assert calls == ['cash-dividend-accounting-protocol', 'cash-dividend-accounting-case']
    modes = result['modes']
    assert modes['legacy_synthetic']['sold_shares'] > modes['fixed_shares_unpaid_claim']['sold_shares']
    assert modes['fixed_shares_unpaid_claim']['sold_shares'] == modes['fixed_shares_unpaid_claim']['bought_shares']
    assert modes['fixed_shares_unpaid_claim']['metrics']['total_return'] is None
    assert not result['holdout_opened'] and not result['accepted_for_signals']
    monkeypatch.setattr(case, '_compare', compare)
    report = case.review_cash_dividend_case(result['folder'])
    assert report['replay_verified'] and report['source_comparison_verified']
    assert report['modes'] == result['modes']


def test_failed_registration_cannot_compute_or_publish_financial_results(db, case_inputs, monkeypatch):
    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    monkeypatch.setattr(case, '_compare', lambda *args: pytest.fail('unregistered financial evaluation'))
    with pytest.raises(RuntimeError, match='پروتکل'):
        case.run_cash_dividend_case(db, **case_inputs)
    from config import settings

    for folder in (settings.DATA_DIR / 'private' / 'cash-dividend-cases').iterdir():
        assert (folder / 'protocol.json').is_file() and not (folder / 'result.json').exists()


def test_rehashed_forged_result_cannot_pass_reexecution(db, case_inputs):
    result = case.run_cash_dividend_case(db, **case_inputs)
    folder = Path(result['folder'])
    payload = json.loads((folder / 'result.json').read_bytes())
    payload['modes']['fixed_shares_unpaid_claim']['cash_final_rial'] += 1
    (folder / 'result.json').write_text(json.dumps(payload), encoding='utf-8')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['files']['result.json'] = hashlib.sha256((folder / 'result.json').read_bytes()).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='بازحساب'):
        case.review_cash_dividend_case(folder)


def test_sealed_case_dates_fail_before_reading_comparison_or_prices(db, monkeypatch):
    monkeypatch.setattr(case, 'load_dividend_comparison', lambda *a: pytest.fail('sealed source must not be read'))
    with pytest.raises(ValueError, match='مرز'):
        case.run_cash_dividend_case(db, comparison='never-read', decision_date='2025-03-21',
            record_date='2025-03-22', ex_date='2025-03-23', end_exclusive='2025-03-24')


def test_sealed_parquet_is_checked_before_prices_or_equity_are_decoded(tmp_path, monkeypatch):
    path = tmp_path / 'prices.parquet'
    pd.DataFrame(dict(date=['2025-03-21'], close_raw=[999])).to_parquet(path)
    original, calls = pd.read_parquet, []

    def watched(*args, **kwargs):
        calls.append(kwargs.get('columns'))
        return original(*args, **kwargs)

    monkeypatch.setattr(case.pd, 'read_parquet', watched)
    with pytest.raises(ValueError, match='مرز'):
        case._load_prices(path, '2025-03-21')
    assert calls == [['date']]


def test_reviewer_remains_offline_and_does_not_open_database(db, case_inputs, monkeypatch, capsys):
    result = case.run_cash_dividend_case(db, **case_inputs)
    monkeypatch.setattr('database.db.DatabaseManager', lambda *a, **k: pytest.fail('offline review opened database'))
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **k: pytest.fail('offline review used network'))
    assert case.main(['review', '--study', result['folder']]) == 0
    assert json.loads(capsys.readouterr().out)['replay_verified']


@pytest.mark.parametrize('mutation', ['event_amount', 'event_symbol', 'event_evidence', 'missing_code'])
def test_rehashed_protocol_cannot_lose_source_binding_or_drop_code(db, case_inputs, mutation):
    result = case.run_cash_dividend_case(db, **case_inputs)
    folder = Path(result['folder'])
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    if mutation == 'event_amount':
        protocol['event']['dps_rial'] = '99'
    elif mutation == 'event_symbol':
        protocol['event']['symbol'] = 'دیگر'
    elif mutation == 'event_evidence':
        protocol['event']['evidence_sha256'] = '0' * 64
    else:
        protocol['code_sha256'] = {}
    (folder / 'protocol.json').write_text(json.dumps(protocol), encoding='utf-8')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    manifest['files']['protocol.json'] = hashlib.sha256((folder / 'protocol.json').read_bytes()).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='پیوند|کامل'):
        case.review_cash_dividend_case(folder)
