"""Synthetic public market records. All output stays in the temporary test workspace."""

import csv
import hashlib
import json
from datetime import datetime

import pytest

from core.minute_candles import closed_trade_day, reconcile_session
from core.price_revision import daily_request
from core.queue_fall_study import trade_observation, trade_request
from core.timeutils import TEHRAN_TZ
from ops.minute_candles import export_minute_session

CODE, DAY = '12345', '2024-02-03'
LATER = '2024-02-04T00:01:00+03:30'


def print_row(number, clock, price, shares, **changes):
    return dict(nTran=number, hEven=clock, pTran=price, qTitTran=shares, canceled=0,
                insCode=int(CODE), dEven=20240203) | changes


@pytest.fixture
def sources():
    common = dict(source='tsetmc', price_unit='Rial', ins_code=CODE, requested_date=DAY)
    official = dict(insCode=int(CODE), dEven=20240203, priceFirst=100, priceMax=104, priceMin=100,
                    pDrCotVal=102, pClosing=101, qTotTran5J=9, zTotTran=3, qTotCap=920)
    daily = dict(common, url=daily_request(CODE, DAY)[1], received_at=LATER,
                 payload=dict(closingPriceDaily=official))
    opening = print_row(1, 90010, 100, 2)
    tape = dict(common, source_url=trade_request(CODE, DAY), captured_at=LATER, endpoint_is_grouped=False,
                response=dict(tradeHistory=[print_row(3, 90203, 102, 4), print_row(2, 90010, 104, 3),
                                            opening, dict(opening), print_row(4, 90301, 990, 50, canceled=1)]))
    return daily, tape


def test_closed_trade_day_is_tehran_calendar_day_and_accepts_jalali(monkeypatch):
    monkeypatch.setattr('core.minute_candles.now_tehran', lambda: datetime(2026, 11, 5, 0, 1, tzinfo=TEHRAN_TZ))
    for day in ('yesterday', 'دیروز', '۱۴۰۵/۸/۱۳', '2026-11-04'):
        assert closed_trade_day(day) == '2026-11-04'
    for day in ('2026-11-05', '2026-11-06', '20261104', '1405/13/01'):
        with pytest.raises(ValueError):
            closed_trade_day(day)


def test_candle_order_empty_minutes_and_daily_reconciliation(sources):
    bars, report = reconcile_session(CODE, DAY, *sources)
    assert report['status'] == 'reconciled' and report['bar_count'] == 2
    assert report['duplicate_rows'] == report['canceled_rows'] == 1
    assert set(report['deltas'].values()) == {0}
    assert report['official_closing_price_rial'] == 101 and report['aggregate']['close_rial'] == 102
    assert bars == [
        dict(time_tehran=f'{DAY}T09:00:00+03:30', time_utc=f'{DAY}T05:30:00+00:00',
             open_rial=100, high_rial=104, low_rial=100, close_rial=104, volume_shares=5, trade_count=2, trade_value_rial=512),
        dict(time_tehran=f'{DAY}T09:02:00+03:30', time_utc=f'{DAY}T05:32:00+00:00',
             open_rial=102, high_rial=102, low_rial=102, close_rial=102, volume_shares=4, trade_count=1, trade_value_rial=408),
    ]


def test_unbound_row_metadata_is_reported_separately_from_exact_totals(sources):
    daily, tape = sources
    for row in tape['response']['tradeHistory']:
        row.update(insCode=None, dEven=0)
    bars, report = reconcile_session(CODE, DAY, daily, tape)
    assert len(bars) == 2
    assert not report['trade_payload_date_verified'] and not report['trade_payload_identity_verified']


@pytest.mark.parametrize('fractional_price', [False, True])
def test_minute_reconciliation_consumes_preserved_trade_tape_with_exact_prices(sources, fractional_price):
    daily, tape = sources
    rows = tape['response']['tradeHistory']
    for row in rows:
        row['pTran'] = float(row['pTran'])
    if fractional_price:
        rows[0]['pTran'] += .25
    content = json.dumps(tape['response']).encode()
    archived = trade_observation(dict(source='tsetmc', ins_code=CODE, url=tape['source_url'], http_status=200,
        content=content, response_bytes=len(content), body_sha256=hashlib.sha256(content).hexdigest(),
        requested_at=LATER, received_at=LATER), CODE, DAY)
    if fractional_price:
        with pytest.raises(ValueError, match='pTran'):
            reconcile_session(CODE, DAY, daily, archived)
    else:
        _, report = reconcile_session(CODE, DAY, daily, archived)
        assert report['status'] == 'reconciled' and set(report['deltas'].values()) == {0}


@pytest.mark.parametrize('change', ['daily_date', 'trade_date', 'id', 'clock', 'sequence', 'canceled_conflict',
                                    'grouped', 'other_url', 'early_capture'])
def test_ambiguous_trade_tape_and_incomplete_session_receipts_are_rejected(sources, change):
    daily, tape = sources
    rows = tape['response']['tradeHistory']
    if change == 'daily_date':
        daily['payload']['closingPriceDaily']['dEven'] = 20240202
    elif change == 'trade_date':
        rows[0]['dEven'] = 20240202
    elif change == 'id':
        rows[0]['insCode'] = 999
    elif change == 'clock':
        rows[0]['hEven'] = 96100
    elif change == 'sequence':
        rows[0]['hEven'] = 85959
    elif change == 'canceled_conflict':
        rows[3]['canceled'] = 1
    elif change == 'grouped':
        tape['endpoint_is_grouped'] = True
    elif change == 'other_url':
        tape['source_url'] = 'https://example.invalid/other'
    else:
        tape['captured_at'] = f'{DAY}T12:31:00+03:30'
    with pytest.raises(ValueError):
        reconcile_session(CODE, DAY, daily, tape)


def test_partial_or_missing_source_produces_no_bars(sources):
    daily, tape = sources
    assert reconcile_session(CODE, DAY, None, tape)[1]['status'] == 'daily_unavailable'
    assert reconcile_session(CODE, DAY, daily, None)[1]['status'] == 'trade_history_unavailable'
    tape['response']['tradeHistory'].pop(0)
    bars, report = reconcile_session(CODE, DAY, daily, tape)
    assert bars == [] and report['status'] == 'daily_mismatch'
    assert report['deltas']['trade_count'] == -1 and report['deltas']['volume_shares'] == -4


def test_dated_no_trade_session_is_distinct_from_an_unavailable_tape(sources):
    daily, _ = sources
    daily['payload']['closingPriceDaily'].update(zTotTran=0, qTotTran5J=0, qTotCap=0)
    bars, report = reconcile_session(CODE, DAY, daily, None)
    assert bars == [] and report['status'] == 'no_trades_reported'


@pytest.mark.parametrize(('symbol', 'kind'), [('آزمون2', 303), ('آزمونح', 400)])
def test_export_rejects_nonordinary_boards_without_fetching(symbol, kind):
    with pytest.raises(ValueError, match='سهام عادی'):
        export_minute_session(None, dict(symbol_fa=symbol, ins_code=CODE, instrument_type_code=kind), DAY)


def test_broker_free_export_writes_audited_csv_and_keeps_valid_run_on_later_mismatch(tmp_path, monkeypatch, sources):
    monkeypatch.setattr('ops.minute_candles.settings.DATA_DIR', tmp_path)
    monkeypatch.setattr('ops.minute_candles.settings.DB_PATH', tmp_path / 'brs.db')
    daily, tape = sources

    class Source:
        def get_dated_daily_observation(self, code, day):
            assert (code, day) == (CODE, DAY)
            return daily

        def get_trade_session(self, code, day):
            assert (code, day) == (CODE, DAY)
            return tape

    symbol = dict(symbol_fa='آزمون', ins_code=CODE, instrument_type_code=303)
    folder, report = export_minute_session(Source(), symbol, DAY)
    target = folder / 'candles.csv'
    assert report['csv_sha256'] == hashlib.sha256(target.read_bytes()).hexdigest()
    with target.open(encoding='utf-8-sig', newline='') as stream:
        assert [int(row['volume_shares']) for row in csv.DictReader(stream)] == [5, 4]
    assert json.loads((folder / 'audit.json').read_text(encoding='utf-8')) == report
    assert (tmp_path / 'private' / report['trade_archive_file']).is_file()
    daily['payload']['closingPriceDaily']['qTotTran5J'] = 10
    failed_folder, failed = export_minute_session(Source(), symbol, DAY)
    assert failed['status'] == 'daily_mismatch' and failed['csv_file'] is None
    assert failed_folder != folder and not (failed_folder / 'candles.csv').exists() and target.is_file()


def test_standard_export_never_publishes_canceled_opening_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr('ops.minute_candles.settings.DATA_DIR', tmp_path)
    monkeypatch.setattr('ops.minute_candles.reconcile_session', lambda *_args: (
        [{'open_rial': 100}], dict(status='canceled_opening_conflict', bar_count=0, deltas={'open_rial': 10})))

    class Source:
        def get_dated_daily_observation(self, code, day):
            return None

    symbol = dict(symbol_fa='آزمون', ins_code=CODE, instrument_type_code=300)
    folder, report = export_minute_session(Source(), symbol, DAY)
    assert report['status'] == 'canceled_opening_conflict' and report['csv_file'] is None
    assert not (folder / 'candles.csv').exists()
