"""Public response bytes, not sorted rows or claimed hashes, are the replay authority."""

import base64
import copy
import hashlib
import json

import pytest

from config import settings
from core.codal_news import MAX_ROWS, NewsDecodeError, parse_news_body, verify_page_evidence
from core.news_continuity import NewsWindow
from core.opportunity_radar import stamp
from data_collector.codal import CodalProvider
from tests.test_codal_news_window import START, Session, raw
from tests.test_opportunity_radar import radar_root as radar_root


def observed(payload=None, *, number=1):
    if payload is None:
        payload = dict(IsAttacker=False, Total=1, Page=1, Letters=[raw()])
    return CodalProvider(session=Session(payload)).get_news_page(number)


def test_exact_public_bytes_and_original_order_replay_without_network():
    payload = dict(IsAttacker=False, Total=2, Page=1, Letters=[
        raw(1, SentDateTime='۱۴۰۵/۰۸/۰۱ ۱۰:۰۰:۰۰'),
        dict(raw(2, SentDateTime='۱۴۰۵/۰۸/۰۱ ۱۰:۰۰:۰۲'), PublishDateTime='۱۴۰۵/۰۸/۰۱ ۱۰:۰۰:۰۲')])
    result = observed(payload)
    assert verify_page_evidence(result)
    body = base64.b64decode(result['native_evidence']['body_base64'], validate=True)
    assert body == json.dumps(payload).encode()
    assert hashlib.sha256(body).hexdigest() == result['response_sha256']
    assert [r['tracing_no'] for r in result['letters']] == [1, 2]
    scan = NewsWindow(since='2026-10-23T09:00:00+03:30', requested_at=result['requested_at'])
    scan.add(result)
    checked = scan.result(stop_reason='page_limit')
    assert checked['publication_order_violation_count'] == 1
    assert not checked['window_scan_satisfied']
    assert checked['letters'][1]['sent_at'] == checked['letters'][1]['published_at']


@pytest.mark.parametrize('fault', ['body', 'normalized', 'raw_count', 'type', 'date', 'page',
    'url', 'hash', 'length', 'http', 'incomplete', 'version', 'extra', 'reason', 'base64'])
def test_native_replay_rejects_changed_body_identity_transport_or_normalization(fault):
    result = observed()
    if fault == 'body':
        result['native_evidence']['body_base64'] = base64.b64encode(b'{}').decode()
    elif fault == 'normalized':
        result['letters'][0]['title'] = 'edited'
    elif fault == 'raw_count':
        result['raw_count'] = 2
    elif fault == 'type':
        result['raw_count'] = True
    elif fault == 'date':
        result['received_at'] = '2000-01-01T00:00:00+03:30'
    elif fault == 'page':
        result['requested_page'] = True
    elif fault == 'url':
        result['url'] += '&Symbol=filtered'
    elif fault == 'hash':
        result['response_sha256'] = '0' * 64
    elif fault == 'length':
        result['response_bytes'] += 1
    elif fault == 'http':
        result['http_status'] = 206
    elif fault == 'incomplete':
        result['response_complete'] = False
    elif fault == 'version':
        result['native_evidence']['version'] = 'unknown'
    elif fault == 'extra':
        result['native_evidence']['unapproved'] = 'metadata'
    elif fault == 'reason':
        result['native_evidence']['reason'] = 'invented'
    else:
        result['native_evidence']['body_base64'] = '!!!'
    with pytest.raises(ValueError):
        verify_page_evidence(result)


@pytest.mark.parametrize('body', [
    b'{"IsAttacker":true,"IsAttacker":false,"Total":0,"Page":0,"Letters":[]}',
    b'{"IsAttacker":false,"Total":0,"Page":0,"Letters":[],"x":NaN}',
    b'{"IsAttacker":false,"Total":0,"Page":0,"Letters":[],"x":Infinity}',
    b'{"IsAttacker":false,"Total":0,"Page":0,"Letters":[{"Title":"a","Title":"b"}]}',
], ids=['duplicate-root', 'nan', 'infinity', 'duplicate-row'])
def test_ambiguous_json_never_becomes_native_evidence(body):
    session = Session({})
    session.response.body = body
    result = CodalProvider(session=session).get_news_page(1)
    assert result['status'] == 'unavailable' and result['failure_code'] == 'json_invalid'
    assert result['response_sha256'] == hashlib.sha256(body).hexdigest()
    assert result['native_evidence']['body_base64'] is None


@pytest.mark.parametrize('extra', ['root', 'row', 'nested'])
def test_unknown_fields_are_not_copied_into_native_public_archive(extra):
    payload = dict(IsAttacker=False, Total=1, Page=1, Letters=[raw()])
    if extra == 'root':
        payload['Set-Cookie'] = 'unapproved-test-value'
    elif extra == 'row':
        payload['Letters'][0]['Password'] = 'unapproved-test-value'
    else:
        payload['Letters'][0]['SuperVision'] = {'Authorization': 'unapproved-test-value'}
    result = observed(payload)
    assert result['status'] == 'observed'
    assert result['native_evidence']['reason'] == 'public_schema_unapproved'
    assert not verify_page_evidence(result)
    assert 'unapproved-test-value' not in json.dumps(result)


def test_partial_known_fields_can_be_replayed_without_hiding_the_invalid_row():
    result = observed(dict(IsAttacker=False, Total=2, Page=1, Letters=[raw(), raw(True)]))
    assert result['status'] == 'partial' and result['invalid_rows'] == 1
    assert result['raw_count'] == 2 and len(result['letters']) == 1
    assert verify_page_evidence(result)


def test_unknown_native_row_and_error_body_remain_hash_only():
    result = observed(dict(IsAttacker=False, Total=2, Page=1, Letters=[raw(), {'bad': 1}]))
    assert result['status'] == 'partial' and not verify_page_evidence(result)
    assert result['native_evidence']['body_base64'] is None
    session = Session({}); session.response.status_code = 429
    limited = CodalProvider(session=session).get_news_page(1)
    assert limited['http_status'] == 429 and not verify_page_evidence(limited)


def test_head_recheck_is_also_bound_to_its_original_native_bytes():
    result = observed()
    head = copy.deepcopy(result)
    head['letters'][0]['title'] = 'changed while keeping the original body'
    scan = NewsWindow(since='2026-10-24T00:00:00+03:30', requested_at='2026-10-25T00:00:00+03:30')
    result['requested_at'] = result['received_at'] = '2026-10-25T00:00:00+03:30'
    scan.add(result)
    with pytest.raises(ValueError, match='normalization'):
        scan.result(head_recheck=head, stop_reason='boundary')


def test_legacy_pages_are_explicitly_not_native_replays():
    assert not verify_page_evidence({'status': 'observed', 'letters': []})


@pytest.mark.parametrize('field,value', [
    ('PublishDateTime', '۱۴۰۵/۰۸/۰۱'),
    ('PublishDateTime', '۱۴۰۵/۰۸/۰۱ ۱۰:۰۰'),
    ('PublishDateTime', '۱۴۰۵/۰۸/۰۱ ۱۰:۰۰:۰۰:۹۹'),
    ('PublishDateTime', None),
    ('SentDateTime', '۱۴۰۵/۰۸/۰۱'),
    ('SentDateTime', '۱۴۰۵/۰۸/۰۱ ۱۰:۰۰:۰۱'),
    ('PublishDateTime', '۱۴۰۵/۱۳/۰۱ ۱۰:۰۰:۰۰'),
])
def test_incomplete_invalid_or_reversed_native_time_cannot_invent_availability(field, value):
    result = observed(dict(IsAttacker=False, Total=1, Page=1, Letters=[dict(raw(), **{field: value})]))
    assert result['status'] == 'partial' and result['invalid_rows'] == 1
    assert result['letters'] == [] and result['raw_count'] == 1
    assert verify_page_evidence(result)


def test_native_page_from_before_this_scan_cannot_be_relabelled_as_a_new_observation():
    result = observed()
    scan = NewsWindow(since='2099-01-01T00:00:00+03:30', requested_at='2099-01-02T00:00:00+03:30')
    with pytest.raises(ValueError, match='precedes'):
        scan.add(result)


def test_excessive_native_row_count_is_not_an_empty_success():
    with pytest.raises(NewsDecodeError, match='native_schema_invalid'):
        parse_news_body(json.dumps(dict(IsAttacker=False, Total=MAX_ROWS+1, Page=1,
            Letters=[raw(i+1) for i in range(MAX_ROWS+1)])).encode(), page=1)


def test_window_native_body_budget_stops_requests_without_dropping_already_saved_pages(monkeypatch):
    import data_collector.codal as module

    monkeypatch.setattr(module, 'MAX_NEWS_WINDOW_NATIVE_BYTES', 1)
    monkeypatch.setattr(module, 'now_tehran_iso', lambda: START)
    monkeypatch.setattr(settings, 'RATE_LIMIT_DELAY', 0)
    session = Session(dict(IsAttacker=False, Total=1, Page=1, Letters=[raw()]))
    provider = CodalProvider(session=session)
    calls = []
    original = provider.get_news_page
    def fetch(number, **options):
        calls.append(number)
        return original(number, **options)
    monkeypatch.setattr(provider, 'get_news_page', fetch)
    result = provider.get_news_window(since='2026-10-22T00:00:00+03:30')
    assert calls == [1] and len(result['pages']) == 1
    assert result['stop_reason'] == 'native_body_budget'
    assert not result['window_scan_satisfied'] and result['checkpoint_through'] is None
    assert verify_page_evidence(result['pages'][0])


def test_real_provider_path_is_retained_and_replayed_by_radar(radar_root, monkeypatch):
    from ops import opportunity_radar as radar
    from ops.report_files import read_private_report
    from tests.test_opportunity_radar import PublicProvider, identity

    at = '2023-04-10T14:00:00+03:30'
    monkeypatch.setattr(radar, 'now_tehran_iso', lambda: at)
    monkeypatch.setattr(radar, 'now_tehran', lambda: stamp(at))
    monkeypatch.setattr('data_collector.codal.now_tehran_iso', lambda: at)
    monkeypatch.setattr(settings, 'RATE_LIMIT_DELAY', 0)
    letter = dict(raw(1), Symbol=identity()['symbol_fa'], PublishDateTime='۱۴۰۲/۰۱/۲۰ ۱۰:۰۰:۰۰')
    frozen = radar.activate(radar_root, seed=[identity()])
    provider = CodalProvider(session=Session(dict(IsAttacker=False, Total=1, Page=1, Letters=[letter])))
    report, _ = radar.collect_cycle(radar_root, frozen, provider=PublicProvider(), codal=provider)
    source = read_private_report(radar_root/report['artifacts_folder']/'news-source.json')
    assert source['window_scan_satisfied']
    checked = radar.verify(radar_root)
    assert checked['replayed_native_news_pages'] == 2
    assert checked['replayed_news_windows'] == 1
