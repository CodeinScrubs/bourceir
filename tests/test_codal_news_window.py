"""Real provider path with bounded, deterministic public-response fixtures."""

import hashlib
import json

import pytest

from config import settings
from data_collector.codal import MAX_RETRY_AFTER_SECONDS, CodalProvider, parse_retry_after
from tests.test_news_continuity import START, page


def raw(trace=1, **extra):
    return dict(TracingNo=trace, Symbol='آزمون', Title='افشا',
                PublishDateTime='۱۴۰۵/۰۸/۰۱ ۱۰:۰۰:۰۰', **extra)


class Response:
    status_code = 200
    def __init__(self, payload):
        self.body = json.dumps(payload).encode(); self.closed = False
    def iter_content(self, chunk_size):
        yield self.body
    def close(self):
        self.closed = True


class Session:
    headers = {}
    def __init__(self, payload):
        self.response = Response(payload)
    def get(self, *args, **kwargs):
        assert kwargs['stream'] and not kwargs['allow_redirects']
        return self.response


@pytest.mark.parametrize('bad', [True, 1.5, '1', None])
def test_partial_rows_preserved_and_bad_native_identifier_counted(bad):
    session = Session(dict(IsAttacker=False,Total=2,Page=1,Letters=[raw(),raw(trace=bad)]))
    report = CodalProvider(session=session).get_news_page(1)
    assert report['status'] == 'partial' and report['raw_count'] == 2 and report['invalid_rows'] == 1
    assert len(report['letters']) == 1 and session.response.closed


@pytest.mark.parametrize('payload', [
    dict(IsAttacker=True,Total=0,Page=0,Letters=[]),
    dict(Total=0,Page=0,Letters=[]),
    dict(IsAttacker=False,Total=20,Page=1,Letters=[]),
    dict(IsAttacker=False,Total=0,Page=0,Letters=[raw()]),
    dict(IsAttacker=False,Total=True,Page=1,Letters=[]),
])
def test_invalid_or_verification_payload_is_not_empty_success(payload):
    result = CodalProvider(session=Session(payload)).get_news_page(1)
    assert result['status'] == 'unavailable'


def test_native_empty_page_and_actual_page_count_claim_are_distinct():
    result = CodalProvider(session=Session(dict(IsAttacker=False,Total=0,Page=0,Letters=[]))).get_news_page(1)
    assert result['status'] == 'observed' and result['raw_count'] == 0
    result = CodalProvider(session=Session(dict(IsAttacker=False,Total=629923,Page=31497,Letters=[raw()]))).get_news_page(1)
    assert result['status'] == 'observed' and result['page_count_claim'] == 31497
    assert result['requested_page'] == 1


@pytest.mark.parametrize('body,status', [(b'x'*(1024*1024+1),200), (b'{}',302), (b'not json',200)],
                         ids=['oversize','redirect','invalid-json'])
def test_request_bounds_and_invalid_json(body,status):
    session = Session({}); session.response.body = body; session.response.status_code = status
    report = CodalProvider(session=session).get_news_page(1)
    assert report['status'] == 'unavailable' and session.response.closed


def test_scan_reaches_eleventh_page_instead_of_silently_stopping_at_ten(monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    monkeypatch.setattr('data_collector.codal.now_tehran_iso',lambda:START)
    calls=[]
    def fetch(number,**_):
        calls.append(number)
        return page(number, f'11:{60-number:02}' if number <= 10 else '09:00')
    provider = CodalProvider(); monkeypatch.setattr(provider,'get_news_page',fetch)
    result = provider.get_news_window(since='2026-10-23T10:00:00+03:30')
    assert calls == [*range(1,12),1]
    assert result['window_scan_satisfied'] and result['pages_fetched'] == 11


def test_failed_page_then_recovery_reuses_same_window_and_changed_head_stays_incomplete(monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    monkeypatch.setattr('data_collector.codal.now_tehran_iso',lambda:START)
    provider = CodalProvider()
    monkeypatch.setattr(provider,'get_news_page',lambda n,**_:page(1,'11:00') if n==1 else dict(requested_page=n,status='unavailable'))
    report = provider.get_news_window(since='2026-10-23T10:00:00+03:30')
    assert report['failed_page'] == 2 and report['checkpoint_through'] is None
    values = iter([page(1,'11:00'),page(2,'09:00'),page(1,'11:30')])
    monkeypatch.setattr(provider,'get_news_page',lambda *a,**k:next(values))
    recovered = provider.get_news_window(since=report['window_start'])
    assert recovered['boundary_reached'] and not recovered['head_recheck_stable']
    assert recovered['checkpoint_through'] is None


def test_request_budget_stops_before_next_network_call(monkeypatch):
    monkeypatch.setattr('data_collector.codal.now_tehran_iso',lambda:START)
    moments = iter([0,121,121])
    monkeypatch.setattr('data_collector.codal.time.monotonic',lambda:next(moments))
    provider = CodalProvider()
    monkeypatch.setattr(provider,'get_news_page',lambda *a,**k:pytest.fail('Expired budget must not fetch'))
    result = provider.get_news_window(since='2026-10-23T10:00:00+03:30')
    assert result['stop_reason']=='time_budget' and result['pages_fetched']==0


def sequence_provider(monkeypatch, values):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    monkeypatch.setattr('data_collector.codal.now_tehran_iso',lambda:START)
    calls=[]
    pending=iter(values)
    def fetch(number,**_):
        calls.append(number)
        value=next(pending)
        assert value['requested_page']==number
        return value
    provider=CodalProvider()
    monkeypatch.setattr(provider,'get_news_page',fetch)
    return provider,calls


def test_order_anomaly_collects_remaining_pages_without_sorting_or_closing_by_timestamp(monkeypatch):
    # The first page crosses the requested boundary, then has a later timestamp.
    # Later pages can still contain in-window notices: a timestamp stop is unsafe.
    values=[page(1,'11:00','09:00','09:01'),page(2,'08:00'),page(3,'10:30'),page(4)]
    provider,calls=sequence_provider(monkeypatch,values)
    progress=[]
    result=provider.get_news_window(since='2026-10-23T10:00:00+03:30',progress=progress.append)
    assert calls==progress==[1,2,3,4]
    assert [v['published_at'] for v in result['letters']]==[
        v['published_at'] for native in values for v in native['letters']]
    assert result['reached_end'] and result['stop_reason']=='end'
    assert result['errors']==['publication_order_changed']
    assert result['head_recheck'] is None and not result['head_recheck_stable']
    assert not result['window_scan_satisfied'] and result['checkpoint_through'] is None


def test_order_anomaly_preserves_page_budget_and_does_not_recheck_head(monkeypatch):
    provider,calls=sequence_provider(monkeypatch,[page(1,'11:00','11:01'),page(2,'09:00')])
    result=provider.get_news_window(since='2026-10-23T10:00:00+03:30',pages=2)
    assert calls==[1,2] and result['page_limit_reached']
    assert result['stop_reason']=='page_limit' and result['pages_fetched']==2
    assert result['head_recheck'] is None and result['checkpoint_through'] is None


def test_order_anomaly_preserves_time_budget_and_partial_results(monkeypatch):
    provider,calls=sequence_provider(monkeypatch,[page(1,'11:00','11:01'),page(2,'09:00')])
    moment=[0.0]
    monkeypatch.setattr('data_collector.codal.time.monotonic',lambda:moment[0])
    underlying=provider.get_news_page
    budgets=[]
    def fetch(number,**options):
        budgets.append(options['timeout_seconds'])
        value=underlying(number,**options)
        moment[0]+=61
        return value
    monkeypatch.setattr(provider,'get_news_page',fetch)
    result=provider.get_news_window(since='2026-10-23T10:00:00+03:30',time_budget_seconds=120)
    assert calls==[1,2] and budgets==[120,59]
    assert result['stop_reason']=='time_budget' and result['pages_fetched']==2
    assert len(result['letters'])==3 and result['checkpoint_through'] is None
    assert result['head_recheck'] is None


def test_unavailable_page_after_anomaly_stops_and_retains_already_observed_letters(monkeypatch):
    values=[page(1,'11:00','11:01'),dict(requested_page=2,status='unavailable')]
    provider,calls=sequence_provider(monkeypatch,values)
    result=provider.get_news_window(since='2026-10-23T10:00:00+03:30')
    assert calls==[1,2] and result['failed_page']==2
    assert result['errors']==['page_unavailable','publication_order_changed']
    assert result['stop_reason']=='source_error' and len(result['letters'])==2
    assert result['head_recheck'] is None and result['checkpoint_through'] is None


@pytest.mark.parametrize('defect',['invalid-row','invalid-time'])
def test_non_order_errors_still_stop_collection_after_order_anomaly(monkeypatch,defect):
    second=page(2,'09:00')
    if defect=='invalid-row':
        second.update(status='partial',invalid_rows=1)
    else:
        second['letters'][0]['published_at']=None
    provider,calls=sequence_provider(monkeypatch,[page(1,'11:00','11:01'),second])
    result=provider.get_news_window(since='2026-10-23T10:00:00+03:30')
    assert calls==[1,2] and result['stop_reason']=='source_error'
    assert 'publication_order_changed' in result['errors'] and len(result['errors'])==2
    assert result['checkpoint_through'] is None and result['head_recheck'] is None


def test_clean_timestamp_boundary_still_stops_and_rechecks_head(monkeypatch):
    first=page(1,'11:00')
    provider,calls=sequence_provider(monkeypatch,[first,page(2,'09:00'),first])
    result=provider.get_news_window(since='2026-10-23T10:00:00+03:30')
    assert calls==[1,2,1] and result['stop_reason']=='boundary'
    assert result['window_scan_satisfied'] and result['head_recheck_stable']
    assert result['checkpoint_through']==START and not result['errors']


@pytest.mark.parametrize('header,expected',[
    (None,None),('',None),('invalid',None),('-1',None),('1.5',None),(True,None),
    ('0',0),(' 120 ',120),('999999999',999999999),('172800',172800),
    ('Fri, 23 Oct 2026 08:32:00 GMT',120),('Fri, 23 Oct 2026 08:20:00 GMT',0),
    ('Sat, 24 Oct 2026 12:00:00 GMT',99000),
    ('Fri, 23 Oct 2026 08:32:00',None),('x'*129,None),
],ids=['missing','empty','invalid','negative','fraction','boolean','zero','numeric','large-numeric','two-days',
       'http-date','past-http-date','long-http-date','naive-date','oversized-header'])
def test_retry_after_retains_only_bounded_seconds(header,expected):
    assert parse_retry_after(header,received_at=START)==expected


def test_retry_after_requires_aware_receipt_clock_for_http_date_and_valid_bound():
    assert parse_retry_after('Fri, 23 Oct 2026 08:32:00 GMT',received_at='2026-10-23T12:00:00') is None
    assert parse_retry_after('Fri, 23 Oct 2026 08:32:00 GMT',received_at='invalid') is None
    with pytest.raises(ValueError,match='exceeds'):
        parse_retry_after('120',received_at=START,max_seconds=60)
    for bound in (0,True,MAX_RETRY_AFTER_SECONDS+1):
        with pytest.raises(ValueError):
            parse_retry_after('120',received_at=START,max_seconds=bound)


def test_http_429_records_retry_seconds_and_status_without_reading_or_archiving_headers(monkeypatch):
    monkeypatch.setattr('data_collector.codal.now_tehran_iso',lambda:START)
    session=Session({})
    session.response.status_code=429
    session.response.headers={'Retry-After':'Fri, 23 Oct 2026 08:32:00 GMT','Set-Cookie':'do-not-retain'}
    session.response.iter_content=lambda **_:pytest.fail('Do not read an HTTP429 body')
    result=CodalProvider(session=session).get_news_page(41)
    assert session.response.closed and result['status']=='unavailable'
    assert result['url']=='https://search.codal.ir/api/search/v2/q?&PageNumber=41'
    assert result['http_status']==429 and result['failure_stage']=='http'
    assert result['failure_code']=='http_rate_limited' and result['rate_limit_retry_after_seconds']==120
    assert not result['rate_limit_retry_after_unsupported']
    assert not result['response_complete'] and result['response_sha256'] is None
    assert 'headers' not in result and 'cookies' not in result and 'content' not in result
    assert 'do-not-retain' not in json.dumps(result)


@pytest.mark.parametrize('header',[None,'invalid','-10'])
def test_missing_or_malformed_retry_after_does_not_invent_zero_delay(header):
    session=Session({});session.response.status_code=429
    session.response.headers={} if header is None else {'Retry-After':header}
    result=CodalProvider(session=session).get_news_page(41)
    assert result['http_status']==429 and result['rate_limit_retry_after_seconds'] is None
    assert not result['rate_limit_retry_after_unsupported']


@pytest.mark.parametrize('header',['2147483648','9'*129,'Wed, 01 Jan 2100 00:00:00 GMT'],
                         ids=['numeric-bound','oversized-numeric','date-bound'])
def test_retry_after_above_supported_range_is_explicitly_unsupported_without_shortening(header):
    with pytest.raises(ValueError,match='exceeds'):
        parse_retry_after(header,received_at=START)
    session=Session({});session.response.status_code=429
    session.response.headers={'Retry-After':header}
    result=CodalProvider(session=session).get_news_page(41)
    assert result['rate_limit_retry_after_seconds'] is None
    assert result['rate_limit_retry_after_unsupported'] is True
    assert result['failure_code']=='http_rate_limited' and session.response.closed
    assert header not in json.dumps(result)


def test_supported_long_retry_after_is_preserved_exactly_in_http429_metadata():
    session=Session({});session.response.status_code=429
    session.response.headers={'Retry-After':'172800'}
    result=CodalProvider(session=session).get_news_page(41)
    assert result['rate_limit_retry_after_seconds']==172800
    assert not result['rate_limit_retry_after_unsupported']
    assert parse_retry_after('0'*200+'172800',received_at=START)==172800


@pytest.mark.parametrize('body,stage,code',[
    (b'\xff','decode','body_encoding_invalid'),
    (b'not-json','json','json_invalid'),
    (b'<html>verification</html>','json','json_invalid'),
    (b'{"IsAttacker":true,"Letters":[]}','schema','native_verification_response'),
    (b'{"IsAttacker":false,"Letters":{}}','schema','native_schema_invalid'),
    (b'{"IsAttacker":false,"Letters":[],"Total":20,"Page":1}','pagination','pagination_claims_invalid'),
],ids=['encoding','json','html','verification','schema','pagination'])
def test_complete_rejected_body_keeps_hash_and_failure_stage_but_no_raw_bytes(body,stage,code):
    session=Session({});session.response.body=body
    result=CodalProvider(session=session).get_news_page(1)
    assert result['status']=='unavailable' and result['http_status']==200
    assert result['failure_stage']==stage and result['failure_code']==code
    assert result['response_complete'] and result['response_bytes']==len(body)
    assert result['response_sha256']==hashlib.sha256(body).hexdigest()
    assert result['response_prefix_bytes'] is None and result['response_prefix_sha256'] is None
    assert not {'content','body_base64','headers','cookies','error_message'} & result.keys()
    assert session.response.closed


def test_stream_size_limit_hashes_only_a_bounded_explicit_prefix():
    session=Session({});session.response.body=b'x'*(1024*1024+1)
    result=CodalProvider(session=session).get_news_page(1)
    assert result['failure_stage']=='body' and result['failure_code']=='body_size_limit'
    assert not result['response_complete'] and result['response_bytes'] is None and result['response_sha256'] is None
    assert result['response_prefix_bytes']==1024*1024
    assert result['response_prefix_sha256']==hashlib.sha256(b'x'*(1024*1024)).hexdigest()
    assert session.response.closed


def test_interrupted_stream_keeps_prefix_fingerprint_without_full_body_claim():
    import requests

    session=Session({})
    def interrupted(**_):
        yield b'{"IsAttacker":'
        raise requests.ReadTimeout('exception text must not enter the receipt')
    session.response.iter_content=interrupted
    result=CodalProvider(session=session).get_news_page(1)
    assert result['failure_stage']=='body' and result['failure_code']=='body_read_failed'
    assert result['response_prefix_bytes']==len(b'{"IsAttacker":')
    assert result['response_prefix_sha256']==hashlib.sha256(b'{"IsAttacker":').hexdigest()
    assert result['response_sha256'] is None and not result['response_complete']
    assert 'exception text' not in json.dumps(result) and session.response.closed


def test_stream_time_limit_is_distinct_from_size_limit(monkeypatch):
    moments=iter([0,31])
    monkeypatch.setattr('data_collector.codal.time.monotonic',lambda:next(moments))
    session=Session({})
    result=CodalProvider(session=session).get_news_page(1,timeout_seconds=30)
    assert result['failure_stage']=='body' and result['failure_code']=='body_time_limit'
    assert not result['response_complete'] and result['response_prefix_bytes']==len(session.response.body)
    assert session.response.closed


def test_successful_page_has_complete_fingerprint_and_no_failure_reason():
    session=Session(dict(IsAttacker=False,Total=1,Page=1,Letters=[raw()]))
    result=CodalProvider(session=session).get_news_page(1)
    assert result['response_complete'] and result['response_bytes']==len(session.response.body)
    assert result['failure_stage'] is None and result['failure_code'] is None
    assert result['http_status']==200 and result['rate_limit_retry_after_seconds'] is None
