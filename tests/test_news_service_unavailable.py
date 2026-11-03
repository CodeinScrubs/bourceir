"""HTTP 503 must not lose the publisher's retry deadline or rewrite old archives."""

from copy import deepcopy

import pytest

from core.news_backoff import checked_backoff, deferred, make_backoff
from core.news_continuity import NewsWindow
from data_collector.codal import CodalProvider
from ops import opportunity_radar as radar
from ops.report_files import read_private_report
from tests.test_codal_news_window import Session
from tests.test_news_continuity import SINCE, START, page
from tests.test_news_rate_limit import limited
from tests.test_opportunity_radar import PublicProvider, identity
from tests.test_opportunity_radar import radar_root as radar_root


def unavailable(seconds=None, *, unsupported=False):
    return dict(limited(seconds=seconds, unsupported=unsupported), http_status=503,
                failure_code='http_service_unavailable')


@pytest.mark.parametrize('header,expected,unsupported', [
    ('7200', 7200, False), (None, None, False), ('invalid', None, False),
    ('2147483648', None, True),
])
def test_503_provider_retains_bounded_retry_and_never_reads_body(monkeypatch, header, expected, unsupported):
    monkeypatch.setattr('data_collector.codal.now_tehran_iso', lambda: START)
    session = Session({})
    session.response.status_code = 503
    session.response.headers = {} if header is None else {'Retry-After': header, 'Set-Cookie': 'secret'}
    session.response.iter_content = lambda **_: pytest.fail('A 503 body must not be read')
    result = CodalProvider(session=session).get_news_page(1)
    assert result['status'] == 'unavailable' and session.response.closed
    assert result['failure_stage'] == 'http' and result['failure_code'] == 'http_service_unavailable'
    assert result['http_status'] == 503 and result['rate_limit_retry_after_seconds'] == expected
    assert result['rate_limit_retry_after_unsupported'] is unsupported
    assert not result['response_complete'] and result['response_sha256'] is None
    assert not {'headers', 'cookies', 'content'} & result.keys()


@pytest.mark.parametrize('seconds,delay', [(None, 3600), (0, 0), (7200, 7200)])
def test_503_state_is_reproducible_and_respects_its_deadline(seconds, delay):
    state = make_backoff(unavailable(seconds))
    assert state['version'] == 'codal-service-backoff-v1'
    assert state['delay_seconds'] == delay and checked_backoff(state) == state
    assert deferred(state, at=START) is (delay > 0)
    assert not deferred(state, at=state['not_before'])
    broken = deepcopy(state)
    broken['observation']['http_status'] = 429
    with pytest.raises(ValueError):
        checked_backoff(broken)


def test_unsupported_503_delay_never_invents_an_earlier_retry():
    state = make_backoff(unavailable(unsupported=True))
    assert state['manual_review_required'] and state['not_before'] is None
    assert deferred(state, at='2099-10-23T12:00:00+03:30')


def test_503_page_and_head_recheck_preserve_failure_and_deadline():
    scan = NewsWindow(since=SINCE, requested_at=START)
    scan.add(unavailable(7200))
    result = scan.result(stop_reason='source_error')
    assert result['version'] == 'news-window-v4'
    assert result['rate_limit_state']['delay_seconds'] == 7200
    assert not result['window_scan_satisfied'] and result['checkpoint_through'] is None
    scan = NewsWindow(since=SINCE, requested_at=START)
    scan.add(page(1, '09:00'))
    result = scan.result(head_recheck=unavailable(7200), stop_reason='boundary')
    assert result['rate_limit_state']['delay_seconds'] == 7200
    assert not result['head_recheck_stable'] and result['checkpoint_through'] is None


def test_v3_replay_keeps_its_original_503_semantics():
    old = NewsWindow(since=SINCE, requested_at=START, version='news-window-v3')
    old.add(unavailable(7200))
    result = old.result(stop_reason='source_error')
    assert result['version'] == 'news-window-v3' and result['rate_limit_state'] is None
    assert not result['window_scan_satisfied'] and result['checkpoint_through'] is None
    assert make_backoff(unavailable(7200), include_service_unavailable=False) is None


class ServiceNews:
    def __init__(self):
        self.calls = 0

    def get_news_window(self, *, since, pages, time_budget_seconds, progress):
        self.calls += 1
        at = radar.now_tehran_iso()
        scan = NewsWindow(since=since, requested_at=at)
        scan.add(dict(unavailable(7200), requested_at=at, received_at=at))
        return scan.result(stop_reason='source_error')


def test_503_persists_through_restart_and_replays_without_a_new_request(radar_root):
    first_protocol = radar.activate(radar_root, seed=[identity()])
    source = ServiceNews()
    first, _ = radar.collect_cycle(radar_root, first_protocol, provider=PublicProvider(), codal=source)
    state = first['news_coverage']['rate_limit_state']
    assert source.calls == 1 and state['delay_seconds'] == 7200
    assert read_private_report(radar_root/'news-backoff.json')['state'] == state
    new_protocol = radar.activate(radar_root, new_run=True)
    second, _ = radar.collect_cycle(radar_root, new_protocol, provider=PublicProvider(), codal=source)
    assert source.calls == 1 and second['news_coverage']['rate_limit_deferred']
    assert second['news_coverage']['window_start'] == first['news_coverage']['window_start']
    assert second['news_coverage']['rate_limit_state'] == state
    assert radar.verify(radar_root)['replayed_news_windows'] == 1
