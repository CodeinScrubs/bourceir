"""Market batch validation with synthetic fixtures and a private temporary archive."""

import csv
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest

from core.farabi_industries import (
    MENU_VIEW,
    PEER_VIEW,
    join_activity,
    read_broker_bindings,
    read_industry_roster,
    read_peer_roster,
)
from core.market_day import market_day_request, parse_market_day
from core.minute_candles import reconcile_session
from core.price_revision import daily_request
from core.queue_fall_study import trade_request
from ops.farabi_industry_candles import load_roster, run_batch, run_folder
from ops.report_files import read_private_report, write_private_report

DAY = '2024-02-03'
AT = '2024-02-04T00:01:00+03:30'
CODE = '43203863097260295'
ISIN = 'IRO3TEST0001'


def capture():
    return dict(version='farabi-industries-dom-v1', source_url='https://farabixo.irfarabi.com/', captured_at=AT,
                menu=[dict(name='بازار', heading=True), dict(name='صندوق', heading=False), dict(name='گروه دوم', heading=False)],
                categories=[dict(name='صندوق', status='observed', row_count=2, symbols=['آزمون۱', 'بی‌سابقه']),
                            dict(name='گروه دوم', status='observed', row_count=1, symbols=['آزمون1'])])


def native_market_row(**changes):
    return dict(insCode=int(CODE), instrumentID=ISIN, lVal18AFC='آزمون', dEven=0,
                priceFirst=100, priceMin=100, priceMax=104, pDrCotVal=104,
                qTotTran5J=5, zTotTran=2, qTotCap=512, pClosing=102) | changes


def market_observation(day, rows):
    body = json.dumps(dict(closingPriceDailyHistoryWithInstDetails=rows)).encode()
    return dict(source='tsetmc', url=market_day_request(day), requested_date=day, content=body,
                requested_at=AT, received_at=AT, http_status=200, response_bytes=len(body),
                body_sha256=hashlib.sha256(body).hexdigest())


def daily_observation(**changes):
    return dict(source='tsetmc', price_unit='Rial', ins_code=CODE, requested_date=DAY,
                url=daily_request(CODE, DAY)[1], received_at=AT,
                payload=dict(closingPriceDaily=native_market_row(dEven=20240203) | changes))


def tape_observation():
    return dict(source='tsetmc', price_unit='Rial', ins_code=CODE, requested_date=DAY,
                captured_at=AT, endpoint_is_grouped=False, source_url=trade_request(CODE, DAY),
                response=dict(tradeHistory=[dict(nTran=1, hEven=90001, pTran=100, qTitTran=2, canceled=0),
                                            dict(nTran=2, hEven=90202, pTran=104, qTitTran=3, canceled=0)]))


def bindings():
    return dict(version='farabi-industry-isin-v1', captured_at=AT,
                rows=[dict(symbol='آزمون۱', category='صندوق', isin=ISIN,
                           url=f'https://farabixo.irfarabi.com/?isin={ISIN}')])


def test_market_day_retains_exact_large_identity_and_absent_native_date():
    rows, meta = parse_market_day(DAY, market_observation(DAY, [native_market_row()]))
    assert list(rows) == [CODE] and rows[CODE]['isin'] == ISIN
    assert meta['row_count'] == 1 and meta['native_date_verified'] is False
    for bad in ([native_market_row(dEven=20240202)], [native_market_row(), native_market_row()]):
        with pytest.raises(ValueError):
            parse_market_day(DAY, market_observation(DAY, bad))


def test_menu_coverage_identity_aliases_and_missing_trade_day():
    labels, categories = read_industry_roster(capture())
    assert len(labels) == 2 and categories[1]['group'] == 'بازار'
    native, _ = parse_market_day(DAY, market_observation(DAY, [native_market_row()]))
    rows, active = join_activity(labels, {DAY:native}, DAY)
    assert len(active) == 0 and all(x['activity_status'] == 'activity_unobserved' for x in rows)
    rows, active = join_activity(labels, {'2024-02-02':native}, DAY, bindings=read_broker_bindings(bindings(), labels))
    assert active[CODE]['categories'] == ['صندوق', 'گروه دوم']
    assert active[CODE]['target_day_status'] == 'unknown'
    assert active[CODE]['identity_basis'] == 'farabi_board_isin'
    assert active[CODE]['activity_days'] == 1 and rows[1]['activity_days'] is None
    broken = capture()
    broken['categories'].pop()
    with pytest.raises(ValueError):
        read_industry_roster(broken)


def test_roster_receipt_matches_the_used_bytes_when_capture_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DATA_DIR', tmp_path)
    root = run_folder(DAY)
    source = write_private_report(root / 'farabi-industries.json', capture())
    initial_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    replacement = capture()
    replacement['captured_at'] = '2024-02-04T00:02:00+03:30'

    def replace_after_read(path, **kwargs):
        report = read_private_report(path, **kwargs)
        if path == source:
            write_private_report(source, replacement)
        return report

    monkeypatch.setattr('ops.farabi_industry_candles.read_private_report', replace_after_read)
    _, _, _, receipt = load_roster(root)
    assert receipt['captured_at'] == AT and receipt['capture_sha256'] == initial_sha
    assert hashlib.sha256(source.read_bytes()).hexdigest() != initial_sha


def test_market_open_reference_needs_same_six_controls_and_exact_tape():
    daily = daily_observation(priceFirst=90)
    native, meta = parse_market_day(DAY, market_observation(DAY, [native_market_row()]))
    reference = dict(row=native[CODE]['raw'], url=meta['url'], body_sha256=meta['body_sha256'])
    with pytest.raises(ValueError, match='قیمت'):
        reconcile_session(CODE, DAY, daily, tape_observation())
    bars, audit = reconcile_session(CODE, DAY, daily, tape_observation(), market_reference=reference)
    assert len(bars) == 2 and audit['status'] == 'reconciled' and set(audit['deltas'].values()) == {0}
    assert audit['reference_basis'] == 'market_day_open_with_six_dated_symbol_controls'
    assert audit['dated_symbol_open_rial'] == 90 and audit['market_open_rial'] == 100
    reference['row'] = native_market_row(zTotTran=3)
    with pytest.raises(ValueError, match='قیمت'):
        reconcile_session(CODE, DAY, daily, tape_observation(), market_reference=reference)
    reference['row'] = native_market_row(priceFirst=90)
    bars, audit = reconcile_session(CODE, DAY, daily_observation(), tape_observation(), market_reference=reference)
    assert len(bars) == 2 and audit['reference_basis'] == 'dated_symbol_daily'


def test_both_daily_sources_can_retain_a_canceled_opening_with_separate_output(tmp_path, monkeypatch):
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DATA_DIR', tmp_path)
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DB_PATH', tmp_path / 'brs.db')
    root = run_folder(DAY)
    write_private_report(root / 'farabi-industries.json', capture())
    write_private_report(root / 'farabi-isin-resolution.json', bindings())
    daily = daily_observation(priceFirst=90)
    market_row = native_market_row(priceFirst=90)
    native, meta = parse_market_day(DAY, market_observation(DAY, [market_row]))
    reference = dict(row=native[CODE]['raw'], url=meta['url'], body_sha256=meta['body_sha256'])
    tape = tape_observation()
    prints = tape['response']['tradeHistory']
    for row in prints:
        row['nTran'] += 1
    prints.append(dict(nTran=1, hEven=90000, pTran=90, qTitTran=1, canceled=1))

    class Source:
        def get_market_day_observation(self, day):
            return market_observation(day, [market_row] if day == DAY else [])

        def get_dated_daily_observation(self, code, day):
            return daily

        def get_trade_session(self, code, day):
            return tape

        def close(self):
            pass

    folder, summary = run_batch(Source, DAY)
    assert summary['candle_status'] == {'canceled_opening_conflict':1}
    assert summary['bars'] == 0 and summary['canceled_opening_bars'] == 2
    with (folder / 'canceled-opening-candles.csv').open(encoding='utf-8-sig', newline='') as stream:
        assert [row['open_rial'] for row in csv.DictReader(stream)] == ['100', '104']
    with (folder / 'audit.csv').open(encoding='utf-8-sig', newline='') as stream:
        row = next(csv.DictReader(stream))
        assert row['open_rial_delta'] == '10' and row['trade_count_delta'] == '0'
        assert json.loads(row['opening_cancellation'])['canceled_trade_numbers'] == [1]

    prints[-1]['pTran'] = 91
    bars, audit = reconcile_session(CODE, DAY, daily, tape, market_reference=reference)
    assert not bars and audit['status'] == 'invalid_source'
    prints[-1]['pTran'] = 90
    prints[0]['qTitTran'] += 1
    bars, audit = reconcile_session(CODE, DAY, daily, tape, market_reference=reference)
    assert not bars and audit['status'] == 'invalid_source'


def test_peer_roster_adds_only_observed_outside_symbols_and_seed_identity():
    menu, _ = read_industry_roster(capture())
    peer = dict(version='farabi-peer-groups-dom-v1', captured_at=AT,
                groups=[dict(sector_code='31', sector_name='برق', seed_symbol='آزمون', seed_isin=ISIN,
                             url=f'https://farabixo.irfarabi.com/?isin={ISIN}', status='observed',
                             row_count=2, symbols=['آزمون', 'آزمون1'])])
    labels, categories, resolved = read_peer_roster(peer, menu)
    assert set(labels) == {'آزمون'} and labels['آزمون']['broker_views'] == [PEER_VIEW]
    assert categories[0]['broker_view'] == PEER_VIEW and resolved == {'آزمون':ISIN}
    native, _ = parse_market_day(DAY, market_observation(DAY, [native_market_row()]))
    _, active = join_activity(menu | labels, {DAY:native}, DAY, bindings=read_broker_bindings(bindings(), menu) | resolved)
    assert active[CODE]['broker_views'] == [MENU_VIEW, PEER_VIEW]
    assert len(active[CODE]['categories']) == 3
    peer['groups'][0]['symbols'][0] = 'خارج'
    with pytest.raises(ValueError, match='نماد مبنا'):
        read_peer_roster(peer, menu)


def test_batch_resumes_and_revalidates_only_good_candles(tmp_path, monkeypatch):
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DATA_DIR', tmp_path)
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DB_PATH', tmp_path / 'brs.db')
    root = run_folder(DAY)
    write_private_report(root / 'farabi-industries.json', capture())
    write_private_report(root / 'farabi-isin-resolution.json', bindings())
    calls = []

    class Source:
        def get_market_day_observation(self, day):
            calls.append(('market', day))
            return market_observation(day, [native_market_row()] if day == DAY else [])

        def get_dated_daily_observation(self, code, day):
            calls.append(('daily', code, day))
            return daily_observation(priceFirst=90)

        def get_trade_session(self, code, day):
            calls.append(('trade', code, day))
            return tape_observation()

        def close(self):
            pass

    folder, summary = run_batch(Source, DAY)
    assert summary['candle_status'] == {'reconciled':1}
    assert summary['bars'] == 2 and summary['market_open_reference_symbols'] == 1
    assert summary['menu_categories'] == 2 and summary['peer_only_active_symbols'] == 0
    assert summary['market_day_status'] == {'observed':1, 'observed_empty':29}
    with ZipFile(folder / 'farabi-industry-candles.zip') as package:
        assert package.testzip() is None
        assert {'candles.csv', 'active_symbols.csv', 'categories.csv', 'audit.csv'} <= set(package.namelist())
    with (folder / 'audit.csv').open(encoding='utf-8-sig', newline='') as stream:
        row = next(csv.DictReader(stream))
        assert row['ins_code'] == CODE and row['open_rial_delta'] == '0' and row['reference_trade_value_rial'] == '512'
    assert ('trade', CODE, DAY) in calls
    calls.clear()
    _, resumed = run_batch(Source, DAY, retry=True)
    assert not calls and resumed['bars'] == summary['bars']
    receipts = list((root / 'sessions').glob('*.json'))
    assert [Path(path).stem for path in receipts] == [CODE]
    write_private_report(receipts[0], {'bad':True})
    _, corrupted = run_batch(Source, DAY, inventory_only=True)
    assert corrupted['candle_status'] == {'receipt_error':1} and corrupted['bars'] == 0
    _, recovered = run_batch(Source, DAY, retry=True)
    assert recovered['candle_status'] == {'reconciled':1} and calls == [('daily', CODE, DAY)]


def test_adjacent_batches_reuse_market_dates_and_validate_roster_before_network(tmp_path, monkeypatch):
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DATA_DIR', tmp_path)
    monkeypatch.setattr('ops.farabi_industry_candles.settings.DB_PATH', tmp_path / 'brs.db')
    calls = []

    class Source:
        def get_market_day_observation(self, day):
            calls.append(day)
            return market_observation(day, []) | dict(requested_at='2024-02-05T00:01:00+03:30', received_at='2024-02-05T00:01:00+03:30')

        def close(self):
            pass

    with pytest.raises(ValueError, match='برداشت منوی صنایع'):
        run_batch(Source, DAY, inventory_only=True)
    assert not calls
    next_day = '2024-02-04'
    for day in (DAY, next_day):
        write_private_report(run_folder(day) / 'farabi-industries.json', capture())
    run_batch(Source, DAY, inventory_only=True)
    assert len(calls) == 30
    _, summary = run_batch(Source, next_day, inventory_only=True)
    assert calls[-1] == next_day and len(calls) == 31
    assert summary['market_day_status'] == {'observed_empty':30}
