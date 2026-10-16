"""داده‌های مصنوعی فقط در دیتابیس/پوشه موقت آزمون؛ قیمت واقعی ساخته نمی‌شود."""

import copy
import json
import sqlite3

import pandas as pd
import pytest

from core.price_quality import json_price_row
from core.price_recovery import recovery_candidate
from core.query_screener import trade_evidence
from database.price_recovery import apply_recoveries
from features.provenance import raw_input_snapshot
from tests.test_query_screener import bar, trades

CUTOFF = '2025-03-21'


def issue(**changes):
    return dict(symbol='آزمون', symbol_id=1, ins_code='123', date='2024-02-03', phase='development',
                raw=json_price_row(bar(open_raw=None) | changes))


def test_missing_open_can_be_recovered_from_complete_surviving_path_not_closing_price():
    found = recovery_candidate(issue(), trades(), cutoff=CUTOFF)
    assert found['after']['open_raw'] == 100 and found['after']['close_final_raw'] == 102
    assert found['changed_fields'] == ['open_raw'] and found['status'] == 'verified_same_source_candidate'
    assert not found['applied'] and not found['historical_availability_verified']


@pytest.mark.parametrize('missing', ['close_raw', 'both'])
def test_first_and_last_are_independent_recoverable_endpoints(missing):
    raw = dict(open_raw=100, close_raw=None) if missing == 'close_raw' else dict(open_raw=None, close_raw=None)
    result = recovery_candidate(issue(**raw), trades(), cutoff=CUTOFF)
    assert result['after']['open_raw'] == 100 and result['after']['close_raw'] == 101


def test_canceled_only_revisions_are_not_conflicting_active_trades():
    source = trades()
    canceled = dict(source['response']['tradeHistory'][0], nTran=10, canceled=1)
    source['response']['tradeHistory'] += [canceled, canceled | dict(hEven=140000)]
    checked = trade_evidence(source, bar())
    assert checked['complete'] and checked['canceled_revision_rows'] == 1
    assert recovery_candidate(issue(), source, cutoff=CUTOFF)['after']['open_raw'] == 100


@pytest.mark.parametrize('damage', ['active_conflict', 'active_canceled_conflict', 'partial', 'value', 'count',
                                   'date', 'identity', 'unknown_cancel', 'clock', 'known_open', 'reference'])
def test_incomplete_conflicting_and_unrecoverable_prices_cannot_be_repaired(damage):
    source, row = trades(), issue()
    rows = source['response']['tradeHistory']
    if damage == 'active_conflict': rows.append(rows[1] | dict(pTran=99))
    elif damage == 'active_canceled_conflict': rows.append(rows[1] | dict(canceled=1))
    elif damage == 'partial': rows.pop()
    elif damage == 'value': row['raw']['value'] += 1
    elif damage == 'count': row['raw']['trade_count'] += 1
    elif damage == 'date': rows[0]['dEven'] = 20240204
    elif damage == 'identity': rows[0]['insCode'] = '999'
    elif damage == 'unknown_cancel': rows[0].pop('canceled')
    elif damage == 'clock': rows[0]['hEven'] = 990000
    elif damage == 'known_open': row['raw'].update(open_raw=99, close_raw=None)
    else: row['raw']['ref_price_raw'] = None
    result = recovery_candidate(row, source, cutoff=CUTOFF)
    assert result['after'] is None and result['status'] != 'verified_same_source_candidate'


def test_dated_request_without_payload_identity_needs_every_known_total():
    source = trades()
    for row in source['response']['tradeHistory']:
        row.update(insCode=None, dEven=0)
    result = recovery_candidate(issue(), source, cutoff=CUTOFF)
    assert result['status'] == 'verified_same_source_candidate'
    assert result['date_binding'] == 'dated_request_and_full_known_daily_reconciliation'
    assert not result['payload_date_verified'] and not result['payload_identity_verified']
    source['response']['tradeHistory'].pop()
    assert recovery_candidate(issue(), source, cutoff=CUTOFF)['after'] is None


@pytest.fixture
def stored(db):
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    row = issue()
    row['symbol_id'] = sid
    db.upsert_daily_prices_df(sid, pd.DataFrame([row['raw']]))
    proposal = recovery_candidate(row, trades(), cutoff=CUTOFF)
    return row, proposal


def apply(db, pairs):
    return apply_recoveries(db, 'RECOVERY-test', pairs, cutoff=CUTOFF, evidence={'sha256': 'test-only'})


def test_atomic_recovery_preserves_old_value_and_append_only_receipt_and_is_idempotent(db, stored):
    row, proposal = stored
    assert apply(db, [stored]) == dict(applied=1, already_applied=0)
    current = db.get_daily_prices_df(row['symbol']).iloc[0]
    assert json_price_row(current) == proposal['after']
    receipt = db.conn.execute('SELECT * FROM price_recoveries').fetchone()
    assert json.loads(receipt['before_json'])['open_raw'] is None
    assert db.conn.execute('SELECT open_raw FROM daily_prices_revisions').fetchone()[0] is None
    assert apply(db, [stored]) == dict(applied=0, already_applied=1)
    for sql in ('UPDATE price_recoveries SET applied_at="changed"', 'DELETE FROM price_recoveries'):
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.conn.execute(sql)
        db.conn.rollback()


def test_changed_local_row_rolls_back_all_candidates(db, stored):
    row, proposal = stored
    bad = copy.deepcopy(stored)
    bad[0]['raw']['value'] += 1
    with pytest.raises(ValueError, match='تغییر'):
        apply(db, [stored, bad])
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0
    assert db.get_daily_prices_df(row['symbol']).open_raw.isna().all()


@pytest.mark.parametrize('single', [False, True])
def test_unchanged_daily_refresh_does_not_erase_recovered_endpoint(db, stored, single):
    row, proposal = stored
    apply(db, [stored])
    if single:
        raw = row['raw']
        assert db.insert_daily_price(row['symbol_id'], row['date'], raw['open_raw'], raw['high_raw'], raw['low_raw'],
                                     raw['close_raw'], volume=4, value=401, trade_count=4, close_final_raw=102, ref_price_raw=100)
    else:
        db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([row['raw']]))
    assert json_price_row(db.get_daily_prices_df(row['symbol']).iloc[0]) == proposal['after']


def test_changed_source_totals_or_new_nonmissing_endpoint_are_not_overridden(db, stored):
    row, proposal = stored
    apply(db, [stored])
    fresh = row['raw'] | dict(open_raw=99)
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([fresh]))
    assert db.get_daily_prices_df(row['symbol']).open_raw.iloc[0] == 99
    fresh = row['raw'] | dict(value=402)
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([fresh]))
    assert db.get_daily_prices_df(row['symbol']).open_raw.isna().all()


def test_other_column_or_holdout_changes_are_never_accepted(db, stored):
    _, proposal = stored
    forged = copy.deepcopy(proposal)
    forged['after']['ref_price_raw'] = 99
    with pytest.raises(ValueError):
        apply(db, [(stored[0], forged)])
    with pytest.raises(ValueError, match='مهر'):
        apply_recoveries(db, 'bad', [stored], cutoff='2026-01-01', evidence={})


def test_recovery_provenance_is_date_scoped_even_when_values_already_match(db, stored):
    before = raw_input_snapshot(db, end_exclusive=CUTOFF)
    assert before['version'] == 'raw-inputs-v2' and before['tables']['price_recoveries'] == {}
    apply(db, [stored])
    after = raw_input_snapshot(db, end_exclusive=CUTOFF)
    assert after['tables']['price_recoveries']['2024-02-03']['rows'] == 1
    assert after['tables']['client_types_daily'] == before['tables']['client_types_daily']
    assert raw_input_snapshot(db, end_exclusive='2024-02-03')['tables']['price_recoveries'] == {}


def test_unrelated_source_change_does_not_resurrect_old_recovery_on_later_refresh(db, stored):
    row, _ = stored
    apply(db, [stored])
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([row['raw'] | dict(value=402)]))
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([row['raw']]))
    assert db.get_daily_prices_df('آزمون').open_raw.isna().all()
