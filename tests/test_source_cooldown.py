"""Public source limits, native retry headers and durable cross-client admission."""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import settings
from core.daily_publications import _clock
from core.source_cooldown import checked_backoff, deferred, make_backoff, public_url, retry_after, stronger
from core.timeutils import now_tehran_iso
from data_collector import source_cooldown as store
from data_collector.tsetmc import TsetmcProvider

URL = 'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDaily/123/20240103'
AT = '2024-01-03T14:00:00+03:30'


def _windows_denial():
    error = PermissionError('simulated sharing or access denial')
    error.winerror = 5
    return error


def test_windows_transient_state_replacement_retries_without_network(tmp_path, monkeypatch):
    monkeypatch.setattr(store, '_WINDOWS', True, raising=False)
    original = store.os.replace
    attempts = []

    def replace(source, target):
        attempts.append(1)
        if len(attempts) < 3:
            raise _windows_denial()
        return original(source, target)

    monkeypatch.setattr(store.os, 'replace', replace)
    expected = {'version': store.VERSION, 'last_admitted_at': None, 'backoff': None}
    store._write(tmp_path, expected)
    assert len(attempts) == 3
    assert json.loads((tmp_path/'state.json').read_text(encoding='utf-8')) == expected
    assert not list(tmp_path.glob('.state-*.tmp'))


def test_exhausted_windows_lock_retry_preserves_existing_state(tmp_path, monkeypatch):
    monkeypatch.setattr(store, '_WINDOWS', True, raising=False)
    original = b'{"previous":true}'
    (tmp_path/'state.json').write_bytes(original)
    attempts = []
    first = _windows_denial()

    def replace(_source, _target):
        attempts.append(1)
        raise first if len(attempts) == 1 else _windows_denial()

    monkeypatch.setattr(store.os, 'replace', replace)
    with pytest.raises(PermissionError) as caught:
        store._write(tmp_path, {'version': store.VERSION, 'last_admitted_at': None, 'backoff': None})
    assert caught.value is first and len(attempts) == 5
    assert (tmp_path/'state.json').read_bytes() == original
    assert not list(tmp_path.glob('.state-*.tmp'))


@pytest.mark.parametrize(('windows', 'winerror'), [(False, 5), (True, 13)])
def test_state_replacement_does_not_retry_other_permissions(tmp_path, monkeypatch, windows, winerror):
    monkeypatch.setattr(store, '_WINDOWS', windows)
    error = PermissionError('unclassified denial')
    error.winerror = winerror
    attempts = []

    def replace(_source, _target):
        attempts.append(1)
        raise error

    monkeypatch.setattr(store.os, 'replace', replace)
    with pytest.raises(PermissionError) as caught:
        store._write(tmp_path, {'version': store.VERSION, 'last_admitted_at': None, 'backoff': None})
    assert caught.value is error and len(attempts) == 1
    assert not list(tmp_path.glob('.state-*.tmp'))


def observation(*, status=429, seconds=None, unsupported=False, at=AT):
    return dict(url=URL,requested_at=at,received_at=at,http_status=status,
                retry_after_seconds=seconds,retry_after_unsupported=unsupported)


@pytest.mark.parametrize('header,seconds',[(None,None),('0',0),(' 00061 ',61),('-1',None),
    ('نامعلوم',None),('Wed, 03 Jan 2024 10:31:01 GMT',61),('Wed, 03 Jan 2024 10:00:00 GMT',0)])
def test_retry_metadata_is_bounded_and_http_dates_are_aware(header,seconds):
    assert retry_after(header,received_at=AT)==seconds


@pytest.mark.parametrize('header',['2147483648','9'*1000,'Fri, 31 Dec 9999 23:59:59 GMT'])
def test_unrepresentably_long_retry_delay_cannot_be_shortened(header):
    with pytest.raises(ValueError):retry_after(header,received_at=AT)


@pytest.mark.parametrize('url',['http://cdn.tsetmc.com/api/x','https://cdn.tsetmc.com.evil/api/x',
    'https://user:password@cdn.tsetmc.com/api/x','https://cdn.tsetmc.com:443/api/x',
    'https://cdn.tsetmc.com/api/x#fragment','https://example.com/api/x','https://old.tsetmc.com/account',
    'https://cdn.tsetmc.com/api/../account','https://cdn.tsetmc.com/api/%2e%2e/account'])
def test_only_public_source_origins_are_admissible(url):
    with pytest.raises(ValueError):public_url(url)


@pytest.mark.parametrize('status,delay',[(429,3600),(503,60)])
def test_fallback_is_explicit_local_policy_and_reconstructable(status,delay):
    value=make_backoff(observation(status=status))
    assert value['delay_source']=='fallback' and value['delay_seconds']==delay
    assert checked_backoff(value)==value
    assert deferred(value,at=AT)
    assert not deferred(value,at=(_clock(AT)+timedelta(seconds=delay)).isoformat())


def test_unsupported_server_duration_stays_blocked_and_derived_tampering_fails():
    value=make_backoff(observation(unsupported=True))
    assert value['not_before'] is None and deferred(value,at='2025-01-03T14:00:00+03:30')
    for field,changed in [('not_before',AT),('delay_seconds',0),('receipt_sha256','0'*64)]:
        broken=deepcopy(value);broken[field]=changed
        with pytest.raises(ValueError):checked_backoff(broken)
    with pytest.raises(ValueError,match='rollback'):deferred(value,at='2024-01-03T13:59:59+03:30')


def test_a_concurrent_shorter_limit_or_success_does_not_clear_longer_limit():
    long=make_backoff(observation(seconds=900))
    short=make_backoff(observation(seconds=1,at='2024-01-03T14:00:01+03:30'))
    assert stronger(long,short)==long and stronger(long,None)==long
    unknown=make_backoff(observation(unsupported=True))
    assert stronger(long,unknown)==unknown and stronger(unknown,long)==unknown


def test_derived_boolean_and_integer_metadata_are_not_interchangeable():
    value=make_backoff(observation(seconds=0))
    for field,replacement in [('manual_review_required',0),('delay_seconds',False)]:
        broken=deepcopy(value);broken[field]=replacement
        with pytest.raises(ValueError):checked_backoff(broken)


class Response:
    def __init__(self,status=429,retry='600'):
        self.status_code=status;self.headers={'Retry-After':retry};self.closed=False
    def close(self):self.closed=True
    def raise_for_status(self):
        if self.status_code>=400:raise requests.HTTPError('test response')
    def json(self):return {'observed':True}


def test_two_provider_instances_share_persisted_limit_and_make_no_fake_prices(isolated_public_source_state,monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    calls=[]
    first=TsetmcProvider(session=SimpleNamespace(get=lambda *a,**kw:(calls.append(a[0])or Response())))
    assert first._get_json(URL) is None
    state=store.source_status()
    assert state['backoff']['delay_source']=='server'
    assert state['backoff']['delay_seconds']==600 and calls==[URL]
    second=TsetmcProvider(session=SimpleNamespace(get=lambda *a,**kw:pytest.fail('No HTTP during a shared cooldown')))
    assert second._get_quote_context_json('123',URL) is None
    assert second.last_transport_evidence['status']=='source_cooldown'
    assert store.checked_transport_evidence(second.last_transport_evidence)==second.last_transport_evidence
    text=(isolated_public_source_state/'state.json').read_text(encoding='utf-8')
    assert 'headers' not in text and 'cookie' not in text.lower()


def test_a_fresh_native_python_process_cannot_reset_shared_limit(isolated_public_source_state,monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    store.SourceGate().get(SimpleNamespace(get=lambda *a,**kw:Response()),URL,timeout=5)
    code='''
import sys
from pathlib import Path
from types import SimpleNamespace
from data_collector import source_cooldown as s
s.STATE_DIR=Path(sys.argv[1])
def forbidden(*a,**kw):raise AssertionError('No child HTTP dispatch')
try:s.SourceGate().get(SimpleNamespace(get=forbidden),sys.argv[2],timeout=5)
except s.SourceDeferred:print('persisted-source-limit')
else:raise AssertionError('Child lost the preserved source limit')
'''
    result=subprocess.run([sys.executable,'-c',code,str(isolated_public_source_state),URL],
                          cwd=settings.BASE_DIR,capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='persisted-source-limit'


def test_default_adapter_exposes_first_limited_response_and_unsafe_injected_adapter_is_rejected(monkeypatch):
    source=TsetmcProvider()
    try:
        retry=source.session.get_adapter(URL).max_retries
        assert retry.total==settings.MAX_RETRIES and retry.status==0
        assert not retry.is_retry('GET',429,has_retry_after=True)
        assert not retry.is_retry('GET',503,has_retry_after=True)
    finally:source.close()
    session=requests.Session()
    session.mount('https://',HTTPAdapter(max_retries=Retry(total=2,status_forcelist=(429,))))
    monkeypatch.setattr(session,'get',lambda *a,**kw:pytest.fail('Unsafe hidden HTTP retry must be rejected before dispatch'))
    try:
        assert TsetmcProvider(session=session)._get_json(URL) is None
    finally:session.close()


@pytest.mark.parametrize('body',[b'{"version":1,"version":2}',b'not-json',b' '*65537],
                         ids=['duplicate-keys','invalid-json','oversized-state'])
def test_corrupt_state_cannot_be_reinterpreted_as_an_empty_cooldown(isolated_public_source_state,body):
    isolated_public_source_state.mkdir()
    (isolated_public_source_state/'state.json').write_bytes(body)
    gate=store.SourceGate()
    with pytest.raises(ValueError):
        gate.get(SimpleNamespace(get=lambda *a,**kw:pytest.fail('No network on corrupt state')),URL,timeout=5)
    assert gate.last_evidence['status']=='gate_error'
    assert (isolated_public_source_state/'state.json').read_bytes()==body


def test_shared_request_spacing_is_global_across_threads(monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',.02)
    evidence=[]
    lock=threading.Lock()
    def task(_):
        gate=store.SourceGate()
        gate.get(SimpleNamespace(get=lambda *a,**kw:Response(200)),URL,timeout=5)
        with lock:evidence.append(gate.last_evidence['requested_at'])
    with ThreadPoolExecutor(max_workers=4)as executor:list(executor.map(task,range(8)))
    ordered=sorted(map(_clock,evidence))
    assert len(ordered)==8
    assert all((b-a).total_seconds()>=.02 for a,b in zip(ordered,ordered[1:]))


def test_concurrent_limited_responses_preserve_the_longest_deadline(monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    barrier=threading.Barrier(4)
    def task(seconds):
        def get(*a,**kw):barrier.wait(timeout=10);return Response(retry=str(seconds))
        gate=store.SourceGate()
        gate.get(SimpleNamespace(get=get),URL,timeout=5)
        return gate.last_evidence['backoff']
    with ThreadPoolExecutor(max_workers=4)as executor:values=list(executor.map(task,[1,60,900,2]))
    actual=store.source_status()['backoff']
    assert _clock(actual['not_before'])==max(_clock(v['not_before'])for v in values)
    assert actual['delay_seconds']==900


def test_state_clock_rollback_defers_without_a_request(isolated_public_source_state,monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    gate=store.SourceGate()
    gate.get(SimpleNamespace(get=lambda *a,**kw:Response(200)),URL,timeout=5)
    future=store.source_status()
    future['last_admitted_at']=(_clock(now_tehran_iso())+timedelta(hours=1)).isoformat()
    (isolated_public_source_state/'state.json').write_text(json.dumps(future),encoding='utf-8')
    with pytest.raises(ValueError,match='rollback'):
        store.SourceGate().get(SimpleNamespace(get=lambda *a,**kw:pytest.fail('No admission after rollback')),URL,timeout=5)


def test_state_write_failure_closes_the_limited_response(monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    response=Response()
    original=store._write
    def write(root,value):
        if value['backoff']is not None:raise PermissionError('test-only storage failure')
        return original(root,value)
    monkeypatch.setattr(store,'_write',write)
    with pytest.raises(PermissionError):
        store.SourceGate().get(SimpleNamespace(get=lambda *a,**kw:response),URL,timeout=5)
    assert response.closed
    assert store.source_status()['backoff']['delay_seconds']==600


def test_a_new_process_recovers_rate_limit_when_main_state_replacement_was_interrupted(isolated_public_source_state,monkeypatch):
    monkeypatch.setattr(settings,'RATE_LIMIT_DELAY',0)
    original=store._write
    def interrupted(root,value):
        if value['backoff']is not None:raise PermissionError('test-only state replace interruption')
        return original(root,value)
    monkeypatch.setattr(store,'_write',interrupted)
    with pytest.raises(PermissionError):
        store.SourceGate().get(SimpleNamespace(get=lambda *a,**kw:Response()),URL,timeout=5)
    assert len(list(isolated_public_source_state.glob('limit-*.json')))==1
    code='''
from pathlib import Path
from types import SimpleNamespace
import sys
from data_collector import source_cooldown as s
s.STATE_DIR=Path(sys.argv[1])
def forbidden(*a,**kw):raise AssertionError('No HTTP after interrupted cooldown persistence')
try:s.SourceGate().get(SimpleNamespace(get=forbidden),sys.argv[2],timeout=5)
except s.SourceDeferred:print('recovered-source-limit')
else:raise AssertionError('Source cooldown lost')
'''
    result=subprocess.run([sys.executable,'-c',code,str(isolated_public_source_state),URL],cwd=settings.BASE_DIR,
                          capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    assert result.stdout.strip()=='recovered-source-limit'


@pytest.mark.parametrize('realtime',[False,True])
def test_both_profiles_do_not_follow_redirects_or_accept_partial_json(realtime):
    calls=[]
    def get(url,**options):
        calls.append(options)
        return Response(302)
    provider=TsetmcProvider(session=SimpleNamespace(get=get),realtime=realtime)
    assert provider._get_json(URL)is None
    assert len(calls)==1 and calls[0]['allow_redirects']is False
