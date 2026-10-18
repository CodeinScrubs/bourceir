"""نمونه مصنوعی فقط در آزمون؛ بایت و دیتابیس عملیاتی تولید نمی‌شود."""

import copy
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from core.capture_evidence import binding_problems, receipts_from_cycle
from core.forward_observations import quote_evidence, receipt_quote_evidence, select_receipts
from core.legacy_quotes import (
    MAX_BYTES,
    archive_observation,
    parse_quote,
    quote_request,
    restore_observation,
)
from core.risk import intraday_entry_constraints
from core.timeutils import TEHRAN_TZ
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from database.capture import CaptureRepository, capture_code_identity
from ops.intraday_audit import audit_intraday
from tests.test_dashboard import dashboard_db as dashboard_db
from tests.test_dividend_evidence import Response, Session


def packet(*, price_changes=None, orders='2@400@999@1001@500@3,', extra_column=None, index_info=''):
    fields = ['10:00:00', 'A ', '1000', '1000', '990', '980', '1010', '970',
              '10', '1000', '1000000', '1', '20261014', '100000', '', '']
    for index, value in (price_changes or {}).items():
        fields[index] = value
    if extra_column is not None:
        fields.insert(14, extra_column)
    return ';'.join([','.join(fields), index_info, orders, '', '', '', '', '', '']).encode()


def observation(body=None, code='123', received='2026-10-14T10:00:01+03:30'):
    body = packet() if body is None else body
    _, url = quote_request(code)
    return dict(source='tsetmc', ins_code=code, url=url, content=body,
                requested_at=received, received_at=received,
                response_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest())


def test_native_date_and_high_low_mapping_keep_unknowns_and_exact_evidence():
    source = observation()
    parsed = parse_quote(source, 'آزمون')
    assert parsed['provider_timestamp'] == '2026-10-14T10:00:00+03:30'
    assert (parsed['high_price'], parsed['low_price']) == (1010, 970)
    assert (parsed['bid_count_1'], parsed['bid_vol_1'], parsed['bid_price_1']) == (2, 400, 999)
    assert (parsed['ask_price_1'], parsed['ask_vol_1'], parsed['ask_count_1']) == (1001, 500, 3)
    assert parsed['bid_vol_2'] is None and parsed['real_buy_count'] is None
    assert parsed['price_limit_up'] is None and parsed['buy_queue_volume'] is None
    evidence = archive_observation(source)
    assert 'content' not in evidence and 'headers' not in evidence and 'cookies' not in evidence
    assert restore_observation(evidence)['content'] == source['content']
    assert parsed == parse_quote(restore_observation(evidence), 'آزمون')


@pytest.mark.parametrize('code', ['0', '-1', '1.0', '۱۲۳', '1?x=2', '1/2', True, '1'*21])
def test_identity_cannot_turn_into_another_request(code):
    with pytest.raises(ValueError):
        quote_request(code)


@pytest.mark.parametrize('index', [2, 5, 8, 9, 10])
@pytest.mark.parametrize('value', ['True', 'NaN', '1.5', '-1', '1e3', '9223372036854775808'])
def test_financial_fields_never_coerce_invalid_numbers(index, value):
    with pytest.raises(ValueError):
        parse_quote(observation(packet(price_changes={index: value})), 'آزمون')


@pytest.mark.parametrize('changes', [{12: '0'}, {12: '20260230'}, {13: '250000'},
                                     {13: '100002'}, {6: '960'}, {10: '2000000'}])
def test_missing_date_future_clock_and_inconsistent_daily_stats_are_rejected(changes):
    with pytest.raises(ValueError):
        parse_quote(observation(packet(price_changes=changes)), 'آزمون')


def test_old_native_date_remains_old_even_when_received_today():
    parsed = parse_quote(observation(packet(price_changes={12: '20261013'})), 'آزمون')
    assert parsed['provider_timestamp'].startswith('2026-10-13T')
    assert quote_evidence(parsed, '2026-10-14T10:00:01+03:30')['last_price_rial'] is None


def test_documented_duplicate_column_needs_exact_agreement():
    assert parse_quote(observation(packet(extra_column='1000000')), 'آزمون')['value'] == 1000000
    with pytest.raises(ValueError):
        parse_quote(observation(packet(extra_column='999999')), 'آزمون')


@pytest.mark.parametrize('body', [b'', b'<html>verify</html>', b'\xff', b'1;2;3', b'x'*(MAX_BYTES+1)],
                         ids=['empty', 'html', 'encoding', 'sections', 'too-large'])
def test_bad_packet_is_not_a_quote(body):
    with pytest.raises(ValueError):
        parse_quote(observation(body), 'آزمون')


@pytest.mark.parametrize('orders', ['2@400@999@1001@500', '0@400@999@1001@500@3',
                                    '2@400.5@999@1001@500@3', '2@400@999@1001@500@3,'*6],
                         ids=['columns', 'inconsistent-side', 'fractional-volume', 'too-many-levels'])
def test_ambiguous_or_fractional_depth_does_not_supply_liquidity(orders):
    with pytest.raises(ValueError):
        parse_quote(observation(packet(orders=orders)), 'آزمون')


def test_missing_book_is_unknown_while_explicit_empty_side_stays_zero():
    assert parse_quote(observation(packet(orders='')), 'آزمون')['ask_vol_1'] is None
    assert parse_quote(observation(packet(orders='2@400@999@0@0@0,')), 'آزمون')['ask_vol_1'] == 0


@pytest.mark.parametrize('change', [dict(source='other'), dict(url='https://other.invalid/'),
                                    dict(body_sha256='0'*64), dict(received_at='2026-10-14T10:00:00'),
                                    dict(requested_at='2026-10-14T10:00:02+03:30')])
def test_origin_digest_and_receive_clocks_are_part_of_the_contract(change):
    with pytest.raises(ValueError):
        parse_quote(dict(observation(), **change), 'آزمون')


def test_provider_preserves_bytes_and_closes_without_following_redirects():
    response = Response(body=packet())
    session = Session(response)
    result = TsetmcProvider(session=session).get_legacy_quote_observation('123')
    assert result['content'] == packet() and response.closed
    assert result['body_sha256'] == hashlib.sha256(packet()).hexdigest()
    assert not session.calls[0][1]['allow_redirects']
    assert 'headers' not in result


def test_benign_markup_in_ignored_index_section_does_not_reject_valid_quote():
    body = packet(index_info="05/07/26 18:00:00,F,123,<div class='mn'>(1.23)</div> -1.03%")
    parsed = parse_quote(observation(body), 'آزمون')
    assert parsed['last_price'] == 1000 and parsed['provider_timestamp'].startswith('2026-10-14T')


def test_provider_keeps_index_markup_as_unexecuted_raw_text():
    body = packet(index_info="123,<div class='mn'>(1.23)</div>")
    response = Response(body=body)
    result = TsetmcProvider(session=Session(response)).get_legacy_quote_observation('123')
    assert result is not None and result['content'] == body and response.closed


@pytest.mark.parametrize('changes', [dict(price_changes={2: '<div>1000</div>'}),
                                    dict(orders='2@400@999@<div>1001</div>@500@3,')])
def test_markup_cannot_become_price_or_depth(changes):
    with pytest.raises(ValueError):
        parse_quote(observation(packet(**changes)), 'آزمون')


@pytest.mark.parametrize('change', [dict(status=302), dict(status=206), dict(status=403),
                                    dict(body=b'<html>captcha</html>'), dict(body=b'\xff'),
                                    dict(body=b'x'*(MAX_BYTES+1)), dict(length=str(MAX_BYTES+1)),
                                    dict(url='https://other.invalid/')])
def test_bad_transport_does_not_fabricate_a_quote(change):
    response = Response(**dict(body=packet(), **change)) if 'body' not in change else Response(**change)
    assert TsetmcProvider(session=Session(response)).get_legacy_quote_observation('123') is None
    assert response.closed


@pytest.fixture
def live_clock(monkeypatch):
    from datetime import datetime

    clock = SimpleNamespace(at=datetime(2026, 10, 14, 10, 0, 1, tzinfo=TEHRAN_TZ))
    for module in ('data_collector.tsetmc', 'data_collector.scheduler', 'database.capture'):
        monkeypatch.setattr(module + '.now_tehran_iso', lambda: clock.at.isoformat())
    return clock


def setup_collector(db):
    db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    provider = TsetmcProvider(session=Session(Response(body=packet())))
    return DataCollector(db=db, provider=provider)


def dated_cycle(db, source):
    scope = db.get_universe_symbols()
    repo = CaptureRepository(db)
    run = repo.start(scope, interval=30, cycles=1, forced=False, provider='tsetmc',
                     explicit_scope=True, quote_source='dated-quote')
    repo.append(run, 1, 'poll_started')
    result = source.collect_market_snapshot(instruments=scope, quote_source='dated-quote')
    repo.append(run, 1, 'poll_finished', dict(status=result['status'], coverage=result['coverage']))
    return repo, run, result


def test_dated_cycle_and_unchanged_reobservation_have_bound_native_price_evidence(db, live_clock):
    source = setup_collector(db)
    repo, run, first = dated_cycle(db, source)
    assert first['status'] == 'success' and first['symbols_succeeded'] == 1
    live_clock.at += timedelta(seconds=30)
    repo.append(run, 2, 'poll_started')
    second = source.collect_market_snapshot(instruments=db.get_universe_symbols(), quote_source='dated-quote')
    repo.append(run, 2, 'poll_finished', dict(status=second['status'], coverage=second['coverage']))
    assert second['unchanged_skipped'] == 1 and second['symbols_succeeded'] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 1
    row = dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    receipt = select_receipts(repo.latest_cycles([row['symbol_id']]),
                              [dict(symbol_id=row['symbol_id'], ins_code='123', instrument_type_code=300)],
                              capture_code_identity())[row['symbol_id']]
    assert not binding_problems(row, receipt, live_clock.at.isoformat())
    observed = receipt_quote_evidence(row, live_clock.at.isoformat(), receipt)
    assert observed['last_price_rial'] == 1000 and observed['capture_receipt_verified']
    assert not observed['independent_receipt_verified']
    assert not observed['executable']
    blockers, _ = intraday_entry_constraints(row, 1000, at=live_clock.at)
    assert blockers  # دامنه مجاز نامعلوم است و حدس زده نمی‌شود.
    audit = audit_intraday(db, '2026-10-14')['capture']['runs'][0]
    assert audit['quote_source'] == 'dated-quote' and audit['bound_snapshot_receipts'] == 2
    assert not audit['full_session_coverage_known']


@pytest.mark.parametrize('kind', ['body', 'rehash', 'identity', 'before_poll'])
def test_quote_receipt_rechecks_raw_content_instead_of_trusting_claimed_price(db, live_clock, kind):
    source = setup_collector(db)
    repo, _, _ = dated_cycle(db, source)
    row = dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    receipt = copy.deepcopy(receipts_from_cycle(repo.latest_cycles([row['symbol_id']])[0])[row['symbol_id']])
    raw = receipt['observation']['source_observation']
    if kind in ('body', 'rehash'):
        changed = archive_observation(observation(packet(price_changes={2: '999'})))
        raw['body_base64'] = changed['body_base64']
        if kind == 'rehash':
            raw['body_sha256'] = changed['body_sha256']
            raw['response_bytes'] = changed['response_bytes']
    elif kind == 'identity':
        raw['ins_code'], raw['url'] = quote_request('999')
    else:
        raw['requested_at'] = '2026-10-14T09:59:00+03:30'
    assert 'quote_source_evidence_invalid' in binding_problems(row, receipt, live_clock.at.isoformat())


def test_parse_failure_remains_in_scope_with_raw_evidence_without_a_snapshot(db, live_clock):
    source = setup_collector(db)
    source.provider.session.response.body = packet(price_changes={12: '0'})
    _, _, result = dated_cycle(db, source)
    assert result['status'] == 'failed' and result['coverage']['rejected'] == 1
    assert result['coverage']['per_instrument'][0]['source_observation']['body_base64']
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0


def test_dated_source_requires_bounded_explicit_scope_before_network_or_protocol(db, live_clock):
    source = setup_collector(db)
    with pytest.raises(ValueError):
        source.collect_market_snapshot(quote_source='dated-quote')
    scope = db.get_universe_symbols()
    with pytest.raises(ValueError):
        CaptureRepository(db).start(scope, interval=30, cycles=1, forced=False, provider='tsetmc',
                                    explicit_scope=False, quote_source='dated-quote')
    assert source.provider.session.calls == []
    assert db.conn.execute('SELECT COUNT(*) FROM capture_runs').fetchone()[0] == 0
    for index, letter in enumerate(('الف', 'ب', 'پ', 'ت', 'ث', 'ج', 'چ', 'ح', 'خ', 'د')):
        db.upsert_symbol(dict(symbol_fa='نمونه'+letter, ins_code=str(200+index), instrument_type_code=300))
    with pytest.raises(ValueError):
        source.collect_market_snapshot(instruments=db.get_universe_symbols(), quote_source='dated-quote')
    assert source.provider.session.calls == []


def test_explicit_dated_scope_cannot_bypass_the_ordinary_equity_boundary(db, live_clock):
    source = setup_collector(db)
    scope = [dict(db.get_universe_symbols()[0], instrument_type_code=305)]
    with pytest.raises(ValueError):
        source.collect_market_snapshot(instruments=scope, quote_source='dated-quote')
    assert source.provider.session.calls == []


@pytest.mark.parametrize('change', ['unknown-source', 'unknown-contract', 'different-coverage'])
def test_rehashed_capture_does_not_silently_change_source_contract(db, live_clock, change):
    source = setup_collector(db)
    repo, _, _ = dated_cycle(db, source)
    sid = db.get_symbol_id('آزمون')
    cycle = copy.deepcopy(repo.latest_cycles([sid])[0])
    protocol = json.loads(cycle['run']['protocol_json'])
    if change == 'unknown-source':
        protocol['quote_source'] = 'other'
    elif change == 'unknown-contract':
        protocol['quote_contract_version'] = 'unknown'
    else:
        payload = json.loads(cycle['finished']['payload_json'])
        payload['coverage']['quote_source'] = 'market-watch'
        cycle['finished']['payload_json'] = json.dumps(payload)
    text = json.dumps(protocol)
    cycle['run'].update(protocol_json=text, protocol_sha256=hashlib.sha256(text.encode()).hexdigest())
    with pytest.raises(ValueError):
        receipts_from_cycle(cycle)


@pytest.mark.parametrize('symbols', ['', ','.join('نماد'+str(i) for i in range(11))])
def test_cli_rejects_unbounded_dated_scope_before_opening_production_database(monkeypatch, symbols):
    import brs

    monkeypatch.setattr('data_collector.scheduler.DataCollector', lambda **kwargs: pytest.fail('must validate scope first'))
    with pytest.raises(ValueError):
        brs.cmd_live(SimpleNamespace(symbols=symbols, interval=30, cycles=1, force=False, quote_source='dated-quote'))


def test_dashboard_requires_scope_and_passes_selected_mode_without_starting_real_process(dashboard_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD

    calls = []
    monkeypatch.setattr('ops.jobs.start_job', lambda name, args: calls.append((name, args)) or True)
    with DatabaseManager(db_path=dashboard_db) as db:
        db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    next(widget for widget in app.selectbox if widget.label == 'منبع پایش').set_value('قیمت تاریخ‌دار نمونه محدود').run()
    assert not app.exception
    assert next(button for button in app.button if button.label == 'شروع پایش زنده').disabled
    next(widget for widget in app.multiselect if widget.label == 'نمادهای پایش تاریخ‌دار').set_value(['آزمون']).run()
    next(button for button in app.button if button.label == 'شروع پایش زنده').click().run()
    assert not app.exception
    assert calls == [('live', ['live', '--quote-source', 'dated-quote', '--symbols', 'آزمون'])]


def test_empty_new_scope_does_not_disable_stopping_an_existing_capture(dashboard_db, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from tests.test_dashboard import DASHBOARD

    monkeypatch.setattr('ops.jobs.job_status', lambda name: dict(running=name == 'live', started_at=None, log_tail=[]))
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    next(widget for widget in app.selectbox if widget.label == 'منبع پایش').set_value('قیمت تاریخ‌دار نمونه محدود').run()
    assert not app.exception
    assert not next(button for button in app.button if button.label == '⏹ توقف').disabled
