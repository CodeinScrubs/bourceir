"""Cross-run news debt and retained history-call intents survive real ledger transitions."""

import hashlib
from datetime import timedelta
from pathlib import Path

import pytest

from core.opportunity_radar import VERSION, digest, stamp
from database.db import DatabaseManager
from database.opportunity_radar import RadarRepository, encode
from ops import opportunity_radar as radar
from ops import radar_continuity as continuity
from ops.report_files import read_private_report, write_private_report
from tests.test_opportunity_radar import PublicProvider, WindowNews, identity, report
from tests.test_opportunity_radar import radar_root as radar_root


@pytest.fixture
def clock(monkeypatch):
    state=[stamp('2023-04-10T14:00:00+03:30')]
    monkeypatch.setattr(radar,'now_tehran_iso',lambda:state[0].isoformat())
    monkeypatch.setattr(radar,'now_tehran',lambda:state[0])
    return state


def boundary(root,at,**limits):
    with DatabaseManager(root/'radar.db',read_only=True)as db:
        return continuity.news_boundary(root,db,as_of=at.isoformat(),**limits)


def legacy_cycle(root,at,*,name,since,news):
    p=dict(id=name,version=VERSION,registered_at=at.isoformat(),news_initial_since=since,
           initial_scope=[identity()],accepted_for_signals=False)
    folder=root/name/'CYCLE-test'
    folder.mkdir(parents=True)
    write_private_report(folder/'news-source.json',news)
    coverage={k:news.get(k)for k in ('window_start','checkpoint_through','window_scan_satisfied',
        'head_recheck_stable','boundary_reached','reached_end','errors','failed_page','page_limit_reached',
        'invalid_rows','stop_reason')}
    value=report(at.isoformat())|dict(run_id=name,price_scan=False,price_observed_at=None,news_coverage=coverage,
        artifacts_folder=str(folder.relative_to(root)),
        artifacts_sha256={'news-source.json':hashlib.sha256((folder/'news-source.json').read_bytes()).hexdigest()})
    with DatabaseManager(root/'radar.db')as db:
        repo=RadarRepository(db)
        repo.activate(p)
        repo.append(name,value)
    return p


def test_failed_then_two_empty_upgrades_and_collection_keep_old_debt(radar_root,clock):
    first=radar.activate(radar_root,seed=[identity()])
    radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(fail=True))
    clock[0]+=timedelta(hours=1)
    radar.activate(radar_root,new_run=True)
    clock[0]+=timedelta(hours=1)
    third=radar.activate(radar_root,new_run=True)
    assert third['news_initial_since']==first['news_initial_since']
    recovered=WindowNews()
    radar.collect_cycle(radar_root,third,provider=PublicProvider(),codal=recovered)
    assert recovered.windows==[first['news_initial_since']]
    assert radar.verify(radar_root)['verified_cycles']==1


def test_narrow_success_even_with_older_notice_does_not_close_global_gap(radar_root,clock):
    first=radar.activate(radar_root,seed=[identity()])
    radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(fail=True))
    clock[0]+=timedelta(hours=3)
    narrower=(clock[0]-timedelta(days=1)).isoformat()
    news=WindowNews().get_news_window(since=narrower,pages=50,time_budget_seconds=120,progress=lambda _:None)
    assert news['window_scan_satisfied']
    legacy_cycle(radar_root,clock[0],name='legacy-narrow',since=narrower,news=news)
    state=boundary(radar_root,clock[0])
    assert state['required_since']==first['news_initial_since']
    assert any(d.get('reason')=='narrow_success_does_not_cover_debt'for d in state['decisions'])
    clock[0]+=timedelta(minutes=1)
    upgraded=radar.activate(radar_root,new_run=True)
    receiver=WindowNews()
    radar.collect_cycle(radar_root,upgraded,provider=PublicProvider(),codal=receiver)
    assert receiver.windows==[first['news_initial_since']]


@pytest.mark.parametrize('fault',['filtered','future_checkpoint','missing_head','missing_aggregate',
                                  'bad_raw_count','boolean_page','empty_body_claim','unknown_transport'])
def test_positive_claim_requires_bound_complete_global_feed_evidence(radar_root,clock,fault):
    since=(clock[0]-timedelta(days=1)).isoformat()
    source=WindowNews().get_news_window(since=since,pages=50,time_budget_seconds=120,progress=lambda _:None)
    if fault=='filtered':
        source['pages'][0]['url']+='&Symbol=other'
    elif fault=='future_checkpoint':
        source['checkpoint_through']=(clock[0]+timedelta(days=1)).isoformat()
    elif fault=='missing_head':
        source['head_recheck']=None
    elif fault=='missing_aggregate':
        source['letters']=[]
    elif fault=='bad_raw_count':
        source['pages'][0]['raw_count']=100
    elif fault=='boolean_page':
        source['pages'][0]['requested_page']=True
    elif fault=='empty_body_claim':
        source['pages'][0]['response_bytes']=0
    else:
        for page in [*source['pages'],source['head_recheck']]:
            page.pop('url',None)
            page.pop('http_status',None)
    legacy_cycle(radar_root,clock[0],name='legacy-invalid',since=since,news=source)
    state=boundary(radar_root,clock[0])
    assert state['required_since']==since
    assert not any(d.get('credited')for d in state['decisions'])


def test_source_hash_tampering_fails_closed_and_final_path_escape_is_rejected(radar_root,clock,tmp_path,monkeypatch):
    since=(clock[0]-timedelta(days=1)).isoformat()
    news=WindowNews().get_news_window(since=since,pages=50,time_budget_seconds=120,progress=lambda _:None)
    legacy_cycle(radar_root,clock[0],name='legacy-source',since=since,news=news)
    target=radar_root/'legacy-source'/'CYCLE-test'/'news-source.json'
    original=target.read_bytes()
    target.write_bytes(b'{}')
    with pytest.raises(ValueError,match='fingerprint'):
        boundary(radar_root,clock[0])
    target.write_bytes(original)
    outside=tmp_path/'outside-native-source.json'
    outside.write_bytes(original)
    # Exercise the resolved symlink/junction outcome without requiring Windows
    # symlink privileges. The outside file is real, and must never be hashed.
    resolve=Path.resolve
    monkeypatch.setattr(Path,'resolve',lambda path,*a,**k:resolve(outside)if path==target else resolve(path,*a,**k))
    hashed=[]
    real_sha=continuity._sha
    def guarded(path):
        hashed.append(path)
        assert path.resolve()!=outside.resolve(),'An escaped source was hashed before the path guard'
        return real_sha(path)
    monkeypatch.setattr(continuity,'_sha',guarded)
    with pytest.raises(ValueError):
        boundary(radar_root,clock[0])
    assert not hashed


def test_partial_intent_survives_failure_upgrade_and_defers_history_only(radar_root,clock,monkeypatch):
    first=radar.activate(radar_root,seed=[identity()])
    original=radar._histories
    def interrupted(*args,**kwargs):
        raise OSError('collector interrupted after registered intent')
    monkeypatch.setattr(radar,'_histories',interrupted)
    with pytest.raises(OSError,match='interrupted'):
        radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(fail=True),price_scan=True)
    attempt=next((radar_root/first['id']).glob('CYCLE-*/price-attempt.json'))
    before=attempt.read_bytes()
    clock[0]+=timedelta(seconds=1)
    upgraded=radar.activate(radar_root,new_run=True)
    provider=PublicProvider()
    deferred,_=radar.collect_cycle(radar_root,upgraded,provider=provider,codal=WindowNews(fail=True),
                                  price_scan=True,bootstrap=True)
    assert not provider.calls  # The independent watch call is still permitted.
    assert deferred['price_scan_requested']and not deferred['price_scan']
    assert deferred['price_observed_at']is None and deferred['price_session_date']is None
    assert all(row['status']=='awaiting_price_scan'and row['date']is None for row in deferred['rows'])
    assert not (radar_root/deferred['artifacts_folder']/'prices.parquet').exists()
    assert attempt.read_bytes()==before
    assert read_private_report(attempt)['http_delivery_verified']is False
    monkeypatch.setattr(radar,'_histories',original)
    clock[0]+=timedelta(seconds=900)
    observed,_=radar.collect_cycle(radar_root,upgraded,provider=provider,codal=WindowNews(fail=True),
                                  price_scan=True,bootstrap=True)
    assert observed['price_scan']and provider.calls
    assert radar.verify(radar_root)['verified_cycles']==2


def test_completed_price_scan_cooldown_survives_new_run_and_wakes_at_expiry(radar_root,clock):
    first=radar.activate(radar_root,seed=[identity()])
    radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(),price_scan=True,bootstrap=True)
    clock[0]+=timedelta(seconds=30)
    fresh=radar.activate(radar_root,new_run=True)
    provider=PublicProvider()
    value,_=radar.collect_cycle(radar_root,fresh,provider=provider,codal=WindowNews(),price_scan=True,bootstrap=True)
    assert not value['price_scan']and not provider.calls
    assert value['price_retry']['last_attempt']['kind']=='completed_price_scan'
    until=value['price_retry']['not_before']
    later=clock[0].replace(hour=20)
    # Outside the active news period, a future retry boundary shortens the hour.
    target=(later+timedelta(seconds=80)).isoformat()
    assert radar.next_poll_delay(later,fresh,price_not_before=target)==80
    assert stamp(until)==stamp(first['registered_at'])+timedelta(seconds=900)


def test_intent_write_failure_prevents_any_history_dispatch(radar_root,clock,monkeypatch):
    frozen=radar.activate(radar_root,seed=[identity()])
    original=radar.write_private_report
    def fail(path,value):
        if path.name=='price-attempt.json':
            raise OSError('intent storage unavailable')
        return original(path,value)
    monkeypatch.setattr(radar,'write_private_report',fail)
    monkeypatch.setattr(radar,'_histories',lambda *args,**kwargs:pytest.fail('Unregistered history dispatch'))
    with pytest.raises(OSError,match='intent storage'):
        radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=WindowNews(),price_scan=True)
    assert radar.status(radar_root)['latest']is None


def test_future_intent_and_request_tampering_are_fail_closed(radar_root,clock,monkeypatch):
    frozen=radar.activate(radar_root,seed=[identity()])
    monkeypatch.setattr(radar,'_histories',lambda *args,**kwargs:(_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):
        radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=WindowNews(),price_scan=True)
    with DatabaseManager(radar_root/'radar.db',read_only=True)as db:
        with pytest.raises(ValueError,match='future|Future|rollback'):
            continuity.price_budget(radar_root,db,as_of=(clock[0]-timedelta(seconds=1)).isoformat(),retry_seconds=900)
    attempt=next((radar_root/frozen['id']).glob('CYCLE-*/price-attempt.json'))
    original=read_private_report(attempt)
    write_private_report(attempt,dict(original,attempted_at=(clock[0]+timedelta(seconds=1)).isoformat()))
    with DatabaseManager(radar_root/'radar.db',read_only=True)as db:
        with pytest.raises(ValueError,match='Future history attempt'):
            continuity.price_budget(radar_root,db,as_of=clock[0].isoformat(),retry_seconds=900)
    write_private_report(attempt,original)
    path=next((radar_root/frozen['id']).glob('CYCLE-*/request.json'))
    value=read_private_report(path)
    value['price_scan']=False
    write_private_report(path,value)
    with DatabaseManager(radar_root/'radar.db',read_only=True)as db:
        with pytest.raises(ValueError,match='bound'):
            continuity.price_budget(radar_root,db,as_of=clock[0].isoformat(),retry_seconds=900)


def test_global_cycle_clock_rollback_cannot_be_hidden_by_sorting_events(radar_root,clock):
    since=(clock[0]-timedelta(days=1)).isoformat()
    news=WindowNews(fail=True).get_news_window(since=since,pages=50,time_budget_seconds=120,progress=lambda _:None)
    legacy_cycle(radar_root,clock[0],name='legacy-earlier',since=since,news=news)
    clock[0]+=timedelta(hours=1)
    legacy_cycle(radar_root,clock[0],name='legacy-later',since=since,news=news)
    with DatabaseManager(radar_root/'radar.db')as db:
        previous=RadarRepository(db).latest('legacy-earlier')['report']
        previous['observed_at']=(clock[0]-timedelta(minutes=30)).isoformat()
        RadarRepository(db).append('legacy-earlier',previous)
    with pytest.raises(ValueError,match='Global radar cycle clock rollback'):
        boundary(radar_root,clock[0])


def test_future_partial_and_registration_cannot_change_past_boundary_or_budget(radar_root,clock):
    first=radar.activate(radar_root,seed=[identity()])
    original,_=radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(),
                                  price_scan=True,bootstrap=True)
    folder=radar_root/original['artifacts_folder']
    old_boundary=read_private_report(folder/'news-boundary-source.json')
    old_budget=read_private_report(folder/'price-budget-source.json')
    clock[0]+=timedelta(minutes=16)
    radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(fail=True),price_scan=True)
    clock[0]+=timedelta(minutes=1)
    second=radar.activate(radar_root,new_run=True)
    radar.collect_cycle(radar_root,second,provider=PublicProvider(),codal=WindowNews(fail=True))
    clock[0]+=timedelta(minutes=16)
    partial=radar_root/first['id']/'CYCLE-later-failed'
    partial.mkdir()
    write_private_report(partial/'request.json',dict(run_id=first['id'],price_scan=True,
                                                   registered_at=clock[0].isoformat()))
    write_private_report(partial/'price-attempt.json',continuity.attempt_intent(radar_root,first,partial,
        attempted_at=clock[0].isoformat(),scope_count=25))
    with DatabaseManager(radar_root/'radar.db',read_only=True)as db:
        assert continuity.news_boundary(radar_root,db,as_of=original['started_at'],
                                         run_limit=1,cycle_limit=0)==old_boundary
        assert continuity.price_budget(radar_root,db,as_of=original['started_at'],retry_seconds=900,
                                        run_limit=1,cycle_limit=0,partial_refs=[],
                                        daily_limit=first['price_daily_limit'])==old_budget
    assert radar.verify(radar_root)['verified_cycles']==1


@pytest.mark.parametrize('fault',['run_identity','registration_time','report_parent','report_version'])
def test_rehashed_sql_anchors_cannot_relabel_ledger_identity(radar_root,clock,fault):
    p=dict(id='registered',version=VERSION,registered_at=clock[0].isoformat(),accepted_for_signals=False)
    with DatabaseManager(radar_root/'radar.db')as db:
        if fault=='run_identity':
            p['id']='different-payload-id'
        sql_time=(clock[0]-timedelta(seconds=1)).isoformat()if fault=='registration_time'else p['registered_at']
        db.conn.execute('INSERT INTO radar_runs VALUES(?,?,?,?)',('registered',sql_time,encode(p),digest(p)))
        if fault.startswith('report_'):
            value=dict(version='different-version'if fault=='report_version'else VERSION,
                run_id='different-parent'if fault=='report_parent'else 'registered',observed_at=clock[0].isoformat(),
                events=[],news_coverage={},price_scan=False)
            chain=digest(dict(previous=digest(p),report=digest(value),run_id='registered',recorded_at=value['observed_at']))
            db.conn.execute('INSERT INTO radar_cycles(run_id,recorded_at,report_json,report_sha256,previous_sha256,'
                'chain_sha256,episodes_sha256)VALUES(?,?,?,?,?,?,?)',
                ('registered',value['observed_at'],encode(value),digest(value),digest(p),chain,digest({})))
        db.conn.commit()
        with pytest.raises(ValueError,match='registration|predecessor'):
            continuity.inventory(db,as_of=clock[0].isoformat())


def test_partial_intent_final_path_escape_is_rejected_before_read(radar_root,clock,tmp_path,monkeypatch):
    p=radar.activate(radar_root,seed=[identity()])
    folder=radar_root/p['id']/'CYCLE-escaped'
    folder.mkdir()
    target=folder/'price-attempt.json'
    write_private_report(target,{})
    outside=tmp_path/'outside-intent.json'
    outside.write_text('{}',encoding='utf-8')
    resolve=Path.resolve
    monkeypatch.setattr(Path,'resolve',lambda path,*a,**k:resolve(outside)if path==target else resolve(path,*a,**k))
    with DatabaseManager(radar_root/'radar.db',read_only=True)as db:
        with pytest.raises(ValueError,match='owning folder'):
            continuity.price_budget(radar_root,db,as_of=clock[0].isoformat(),retry_seconds=900)


def test_provider_cannot_commit_a_different_news_boundary(radar_root,clock):
    frozen=radar.activate(radar_root,seed=[identity()])
    class NarrowProvider:
        def get_news_window(self,**kwargs):
            kwargs['since']=(clock[0]-timedelta(hours=1)).isoformat()
            return WindowNews().get_news_window(**kwargs)
    with pytest.raises(ValueError,match='requested continuity boundary'):
        radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=NarrowProvider())
    assert radar.status(radar_root)['latest']is None
    assert list((radar_root/frozen['id']).glob('CYCLE-*/news-source.json'))


def test_expired_history_cooldown_during_collection_does_not_add_an_hour_sleep(radar_root,clock,monkeypatch):
    first=radar.activate(radar_root,seed=[identity()])
    radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(),price_scan=True,bootstrap=True)
    clock[0]+=timedelta(seconds=1)
    new=radar.activate(radar_root,new_run=True)
    radar.collect_cycle(radar_root,new,provider=PublicProvider(),codal=WindowNews(),price_scan=True,bootstrap=True)
    clock[0]+=timedelta(seconds=900)
    monkeypatch.setattr(radar.time,'sleep',lambda *_:pytest.fail('Expired deferred baseline must be retried now'))
    radar._wait_for_poll(radar_root,new)


def test_daily_history_budget_counts_completed_and_partial_once_across_runs(radar_root,clock,monkeypatch):
    first=radar.activate(radar_root,seed=[identity()])
    assert first['price_daily_limit']==3
    initial,_=radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(),
                                  price_scan=True,bootstrap=True)
    clock[0]+=timedelta(seconds=901)
    original=radar._histories
    monkeypatch.setattr(radar,'_histories',lambda *a,**k:(_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError,match='interrupted'):
        radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=WindowNews(),price_scan=True)
    monkeypatch.setattr(radar,'_histories',original)
    clock[0]+=timedelta(seconds=901)
    second=radar.activate(radar_root,new_run=True)
    third,_=radar.collect_cycle(radar_root,second,provider=PublicProvider(),codal=WindowNews(),price_scan=True)
    assert third['price_scan'] and third['price_retry']['attempts_today']==2
    clock[0]+=timedelta(seconds=901)
    third_run=radar.activate(radar_root,new_run=True)
    provider=PublicProvider()
    blocked,_=radar.collect_cycle(radar_root,third_run,provider=provider,codal=WindowNews(),price_scan=True)
    assert not provider.calls and not blocked['price_scan']
    assert blocked['price_retry']['attempts_today']==3
    assert blocked['price_retry']['daily_limit_reached'] is True
    assert stamp(blocked['price_retry']['not_before']).date() > clock[0].date()
    assert initial['price_scan']
    assert radar.verify(radar_root)['verified_cycles']==1


def test_daily_history_budget_resets_at_tehran_midnight_but_keeps_short_cooldown(radar_root,clock):
    clock[0]=clock[0].replace(hour=23,minute=55)
    frozen=radar.activate(radar_root,seed=[identity()])
    radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=WindowNews(),price_scan=True)
    clock[0]+=timedelta(minutes=6)
    provider=PublicProvider()
    deferred,_=radar.collect_cycle(radar_root,frozen,provider=provider,codal=WindowNews(),price_scan=True)
    assert deferred['price_retry']['attempts_today']==0
    assert deferred['price_retry']['daily_limit_reached'] is False
    assert deferred['price_retry']['interval_deferred'] is True
    assert not deferred['price_scan'] and not provider.calls
    clock[0]+=timedelta(minutes=10)
    value,_=radar.collect_cycle(radar_root,frozen,provider=provider,codal=WindowNews(),price_scan=True)
    assert value['price_scan'] and provider.calls


@pytest.mark.parametrize('bad_limit',[0,-1,True,1.5,'3'])
def test_invalid_daily_history_budget_fails_closed(radar_root,clock,bad_limit):
    radar.activate(radar_root,seed=[identity()])
    with DatabaseManager(radar_root/'radar.db',read_only=True)as db:
        with pytest.raises(ValueError,match='daily'):
            continuity.price_budget(radar_root,db,as_of=clock[0].isoformat(),retry_seconds=900,daily_limit=bad_limit)
