"""مخرج شکست‌ها، صف بررسی و پروفایل شبکه فقط با DB و پاسخ آزمون موقت."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests

from brs import build_parser, cmd_live
from config import settings
from core.candidate_queue import inspection_queue
from core.feed_health import feed_latency
from data_collector.tsetmc import TsetmcProvider
from database.capture import CaptureRepository
from ops.capture_audit import read_capture_audit
from tests.test_capture_evidence import add_scope, collector, start_run
from tests.test_capture_evidence import clock as clock
from tests.test_quote_context import provider


def test_latency_keeps_failed_missing_pending_and_unbound_slots_in_denominator():
    result = feed_latency([2., 40., None], [dict(age_seconds=5., bound=True), dict(age_seconds=60., bound=True),
        dict(age_seconds=10., bound=False)], expected_slots=6, interval_seconds=30)
    assert result['attempted_polls'] == 3 and result['unknown_duration_polls'] == 1
    assert result['polls_exceeding_requested_interval'] == 1
    assert result['fresh_bound_slots'] == 1 and result['observed_fresh_bound_fraction'] == 1/6
    assert result['unknown_or_missing_price_age_slots'] == 3
    assert result['poll_service_seconds']['p95'] == pytest.approx(38.1)
    assert not result['full_session_uptime_verified'] and not result['order_fill_latency_measured']


def test_zero_attempts_are_unknown_and_negative_age_is_never_fresh():
    result = feed_latency([], [], expected_slots=0, interval_seconds=30)
    assert result['poll_service_seconds']['p95'] is result['observed_fresh_bound_fraction'] is None
    result = feed_latency([True, float('nan'), -1, 10**1000],
        [dict(age_seconds=-.1, bound=True)], expected_slots=4, interval_seconds=30)
    assert result['unknown_duration_polls'] == 4 and result['future_price_timestamps'] == 1
    assert result['fresh_bound_slots'] == 0


@pytest.mark.parametrize('slots,interval', [(-1, 30), (True, 30), (1., 30), (1, 0), (1, '30')])
def test_latency_rejects_an_invalid_denominator_or_interval(slots, interval):
    with pytest.raises(ValueError):
        feed_latency([], [], expected_slots=slots, interval_seconds=interval)


def test_real_capture_audit_counts_pending_and_errors_without_creating_quotes(db, market_watch_rows, clock):
    add_scope(db)
    market_watch_rows[0].update(dEven=20261014, hEven=90000)
    repo, run = start_run(db)
    source = collector(db, market_watch_rows)
    repo.append(run, 1, 'poll_started')
    report = source.collect_market_snapshot()
    clock.at += timedelta(seconds=10)
    repo.append(run, 1, 'poll_finished', dict(status=report['status'], coverage=report['coverage'], elapsed_seconds=10))
    clock.at += timedelta(seconds=20)
    repo.append(run, 2, 'poll_started')
    clock.at += timedelta(seconds=65)
    repo.append(run, 2, 'poll_finished', dict(status='error', coverage=None, elapsed_seconds=65))
    repo.append(run, 3, 'poll_started')
    before = db.conn.total_changes
    result = read_capture_audit(db, '2026-10-14')['runs'][0]
    assert result['attempted_polls'] == 3 and result['pending_polls'] == 1
    assert result['latency']['expected_instrument_poll_slots'] == 6
    assert result['latency']['fresh_bound_slots'] == 1
    assert result['latency']['observed_fresh_bound_fraction'] == 1/6
    assert result['latency']['unknown_duration_polls'] == 1
    assert result['latency']['polls_exceeding_requested_interval'] == 1
    assert db.conn.total_changes == before and result['request_profile'] is None


def candidate(name, value=100., **kwargs):
    return dict(symbol=name, date='2024-01-02', hypothesis='rule', daily_value_rial=value,
        research_universe_eligible=True, blockers=['research_only'], **kwargs)


def test_queue_is_bounded_deterministic_deduplicated_and_preserves_deferred_and_excluded():
    rows = [candidate(f'نماد{i:04d}', value=i+1) for i in range(1000)]
    rows += [dict(rows[0], hypothesis='other'), dict(candidate('قديم'), date='2024-01-01')]
    result = inspection_queue(rows, session_date='2024-01-02')
    again = inspection_queue(rows[::-1], session_date='2024-01-02')
    assert result['displayed'] == again['displayed'] and len(result['displayed']) == 5
    assert result['displayed'][0]['symbol'] == 'نماد0999'
    assert len(result['deferred_symbols']) == 995 and len(result['excluded']) == 1
    assert result['candidate_rows'] == 1002 and result['unique_eligible_symbols'] == 1000
    assert result['excluded'][0]['reason'] == 'other_session' and not result['accepted_for_signals']


def test_normalized_queue_prefers_existing_manual_plan_for_attention_and_rejects_conflicting_values():
    rows = [candidate('کيان', 1000.), candidate('کیان', 1000., linked_manual_plan_id=1), candidate('ب', 2000.)]
    result = inspection_queue(rows, session_date='2024-01-02', limit=1)
    assert result['displayed'][0]['symbol'] == 'کیان' and result['unique_eligible_symbols'] == 2
    with pytest.raises(ValueError):
        inspection_queue([*rows, candidate('کیان', 1001.)], session_date='2024-01-02')
    with pytest.raises(ValueError):
        inspection_queue(rows, session_date='2024-01-02', limit=6)


@pytest.mark.parametrize('value', [True, '100', None, float('nan'), -1, 0])
def test_queue_keeps_unknown_or_invalid_traded_value_excluded(value):
    result = inspection_queue([candidate('الف', value)], session_date='2024-01-02')
    assert not result['displayed'] and result['excluded'][0]['reason'] == 'unknown_traded_value'


def test_optional_fast_profile_has_no_retry_and_default_profile_is_preserved():
    default, fast = TsetmcProvider(), TsetmcProvider(realtime=True)
    try:
        assert default.session.get_adapter('https://').max_retries.total == settings.MAX_RETRIES
        assert fast.session.get_adapter('https://').max_retries.total == 0
        assert fast.timeout <= 5 and fast.request_profile == 'live-no-retry-5s-io'
        assert default.request_profile == 'retrying-default'
        assert build_parser().parse_args(['live', '--fast-live']).fast_live
        assert not build_parser().parse_args(['live']).fast_live
    finally:
        default.session.close()
        fast.session.close()


@pytest.mark.parametrize('status', [200, 206, 302])
def test_fast_json_path_caps_io_and_rejects_partial_or_redirect_even_from_injected_session(status):
    calls = []
    def get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=status, raise_for_status=lambda: None, json=lambda: {'test': 1})
    fast = TsetmcProvider(session=SimpleNamespace(get=get), realtime=True)
    result = fast._get_json('https://cdn.tsetmc.com/test-only-no-network', timeout=120)
    assert (result is not None) == (status == 200)
    assert calls == [dict(timeout=5, allow_redirects=False)]
    assert fast.request_profile == 'external-session-unknown'


def test_fast_context_wire_paths_retain_receipt_bodies_and_share_the_smaller_timeout(db, monkeypatch):
    source = provider()
    source.realtime, source.timeout = True, 5
    calls = []
    get = source.session.get
    def observe(url, **kwargs):
        calls.append(kwargs)
        return get(url, **kwargs)
    monkeypatch.setattr(source.session, 'get', observe)
    result = source.get_quote_context_observation('123')
    assert result['quote']['content'] and result['instrument']['content'] and result['limits']['content']
    assert len(calls) == 3 and all(c['timeout'] == (5, 5) and c['allow_redirects'] is False for c in calls)
    db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    repo = CaptureRepository(db)
    run = repo.start(db.get_universe_symbols(), interval=30, cycles=1, forced=True, provider='tsetmc',
        explicit_scope=True, quote_source='dated-quote-context', request_profile='external-session-unknown')
    saved = json.loads(db.conn.execute('SELECT protocol_json FROM capture_runs WHERE id=?', (run,)).fetchone()[0])
    assert saved['request_profile'] == 'external-session-unknown'
    assert saved['quote_source'] == 'dated-quote-context' and saved['quote_contract_version']


def test_fast_failure_is_none_and_not_a_retried_or_synthetic_quote():
    calls = []
    def fail(url, **kwargs):
        calls.append(url)
        raise requests.Timeout('test only')
    source = TsetmcProvider(session=SimpleNamespace(get=fail), realtime=True)
    assert source._get_json('https://cdn.tsetmc.com/test-only-no-network') is None
    assert len(calls) == 1


def test_fast_live_cli_selects_profile_and_records_it_in_the_cycle_contract(db, market_watch_rows, monkeypatch):
    from data_collector.scheduler import DataCollector
    from tests.conftest import FOOLAD_INS, FakeSession

    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    class Session(FakeSession):
        def get(self, url, timeout=None, **kwargs):
            assert kwargs['allow_redirects'] is False
            return super().get(url, timeout=timeout)
    source = TsetmcProvider(session=Session({'GetMarketWatch': {'marketwatch': market_watch_rows},
                                           'GetClientTypeAll': {}}), realtime=True)
    selected = []
    def select(*, realtime):
        selected.append(realtime)
        return source
    monkeypatch.setattr('data_collector.tsetmc.TsetmcProvider', select)
    monkeypatch.setattr('data_collector.scheduler.DataCollector',
        lambda *, symbols, provider: DataCollector(db=db, symbols=symbols, provider=provider))
    # finally فرمان اتصال را می‌بندد؛ خود آزمون باید قرارداد محفوظ را بررسی کند.
    monkeypatch.setattr(db, 'close', lambda: None)
    try:
        args = build_parser().parse_args(['live', '--fast-live', '--force', '--cycles', '1', '--symbols', 'فولاد'])
        assert cmd_live(args) == 0 and selected == [True]
        saved = json.loads(db.conn.execute('SELECT protocol_json FROM capture_runs').fetchone()[0])
        assert saved['request_profile'] == 'external-session-unknown'
        assert db.conn.execute("SELECT COUNT(*) FROM capture_events WHERE event='poll_finished'").fetchone()[0] == 1
    finally:
        db.conn.close()
