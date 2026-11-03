"""Future daily generation, preserved failed attempts, and fully offline budget replay."""

import hashlib
import json
from datetime import datetime, timedelta

import pytest

from config import settings
from core.source_cooldown import make_backoff
from data_collector import daily_publications as archive
from data_collector import source_cooldown as gate
from ops import radar_publications as producer
from ops.radar_prices import collect_composition
from tests.test_daily_publications import NOW, observation
from tests.test_opportunity_radar import identity


@pytest.fixture
def environment(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    monkeypatch.setattr(settings,'DB_PATH',tmp_path/'account.db')
    clock=[datetime.fromisoformat(NOW)]
    monkeypatch.setattr(producer,'now_tehran',lambda:clock[0])
    monkeypatch.setattr(producer,'now_tehran_iso',lambda:clock[0].isoformat())
    monkeypatch.setattr(archive,'now_tehran_iso',lambda:clock[0].isoformat())
    monkeypatch.setattr('requests.sessions.Session.request',lambda *a,**kw:pytest.fail('All producer tests are offline'))
    protocol=dict(publication_producer_policy=dict(producer.POLICY),calendar_exceptions={},
        registered_at='2024-01-03T13:30:00+03:30',
        source_files_sha256={'ops/radar_publications.py':hashlib.sha256((settings.BASE_DIR/'ops/radar_publications.py').read_bytes()).hexdigest()})
    root=settings.DATA_DIR/'private'/'producer-tests'
    root.mkdir(parents=True)
    return root,clock,protocol


class Native:
    supports_dated_publications=True
    def __init__(self,clock,*,missing=(),zero=()):
        self.clock,self.missing,self.zero,self.calls=clock,set(missing),set(zero),[]
    def get_daily_publication_observation(self,code,day,kind):
        self.calls.append(code)
        if code in self.missing:return None
        value=observation(kind,code,day)
        at=self.clock[0].isoformat()
        value.update(requested_at=at,received_at=at)
        if code in self.zero:
            body=json.loads(value['content'])
            body['closingPriceDaily'].update(qTotTran5J=0,qTotCap=0,zTotTran=0)
            content=json.dumps(body).encode()
            value.update(content=content,response_bytes=len(content),body_sha256=hashlib.sha256(content).hexdigest())
        return value


def run(environment,*,missing=(),zero=(),label='one',scope=None):
    root,clock,protocol=environment
    scope=scope or [identity(1),identity(2)]
    folder=root/label;folder.mkdir()
    provider=Native(clock,missing=missing,zero=zero)
    result=producer.produce(folder,scope,protocol,provider=provider)
    collect_composition(folder,{r['ins_code']:None for r in scope},scope,
        session_date=clock[0].date().isoformat(),observed_at=clock[0].isoformat(),registered_at=protocol['registered_at'])
    inventory={p.name:hashlib.sha256(p.read_bytes()).hexdigest()for p in folder.iterdir()if p.is_file()}
    replay=producer.replay_producer(folder,scope,protocol,allowed_files=inventory,observed_at=clock[0].isoformat())
    assert replay==result
    return result,provider,folder,inventory


def test_missing_public_prices_are_generated_without_database_ids_and_replay_without_live_state(environment,monkeypatch):
    result,provider,folder,inventory=run(environment)
    assert provider.calls==['1','2'] and result['decision']['mode']=='capture'
    assert result['archive_statuses']=={'valid_price':2}
    intent=json.loads((folder/'producer-current-intent.json').read_text(encoding='utf-8'))
    assert all(task['symbol_id']is None for task in intent['tasks'])
    monkeypatch.setattr(producer,'capture_cache',lambda *a,**kw:pytest.fail('Offline replay cannot reread live cache'))
    monkeypatch.setattr(producer,'source_status',lambda:pytest.fail('Offline replay cannot use current source health'))
    assert producer.replay_producer(folder,[identity(1),identity(2)],environment[2],allowed_files=inventory,observed_at=NOW)==result
    assert not settings.DB_PATH.exists()


def test_known_valid_or_zero_observations_are_not_refetched_on_restarts(environment):
    result,provider,_,_=run(environment,zero=['2'])
    assert result['archive_statuses']=={'valid_price':1,'observed_zero_activity':1}
    environment[1][0]+=timedelta(minutes=16)
    second,new_provider,_,_=run(environment,label='two')
    assert second['decision']['mode']=='no_unobserved_price_tasks' and not new_provider.calls
    assert second['decision']['observed_codes']==['1','2']
    assert second['created_intent']is None and second['decision']['prior_intents']==1


def test_missing_keys_only_retry_and_daily_budget_survives_registered_restart(environment):
    first,provider,_,_=run(environment,missing=['2'])
    assert provider.calls==['1','2']
    environment[1][0]+=timedelta(seconds=30)
    early,provider,_,_=run(environment,missing=['2'],label='early')
    assert early['decision']['mode']=='producer_retry_cooldown' and not provider.calls
    for index in (2,3):
        environment[1][0]+=timedelta(minutes=16)
        result,provider,_,_=run(environment,missing=['2'],label=str(index))
        assert result['decision']['mode']=='capture' and provider.calls==['2']
    environment[1][0]+=timedelta(minutes=16)
    environment[2]['registered_at']=environment[1][0].isoformat()
    last,provider,_,_=run(environment,missing=['2'],label='new-registered-run')
    assert last['decision']['mode']=='daily_budget_exhausted' and not provider.calls
    assert last['decision']['prior_intents']==3


def test_early_or_closed_session_has_no_capture(environment):
    environment[1][0]=datetime.fromisoformat('2024-01-03T13:00:00+03:30')
    environment[2]['registered_at']='2024-01-03T12:00:00+03:30'
    result,provider,_,_=run(environment)
    assert result['decision']['mode']=='outside_registered_session_boundary' and not provider.calls
    environment[1][0]=datetime.fromisoformat('2024-01-05T14:00:00+03:30')
    result,provider,_,_=run(environment,label='friday')
    assert result['decision']['mode']=='outside_registered_session_boundary' and not provider.calls


def test_preserved_source_limit_defers_publisher_without_spending_daily_capture_budget(environment):
    root=gate._directory()
    backoff=make_backoff(dict(url='https://cdn.tsetmc.com/api/test',requested_at=NOW,received_at=NOW,
                            http_status=429,retry_after_seconds=3600,retry_after_unsupported=False))
    gate._write(root,dict(version=gate.VERSION,last_admitted_at=NOW,backoff=backoff))
    result,provider,_,_=run(environment)
    assert result['decision']['mode']=='source_cooldown' and not provider.calls
    assert result['decision']['prior_intents']==0 and result['created_intent']is None


def test_interrupted_capture_reserves_an_attempt_before_any_network_and_cannot_reset_budget(environment,monkeypatch):
    root,clock,protocol=environment
    folder=root/'interrupted';folder.mkdir()
    original=archive.DailyPublicationArchive.capture
    monkeypatch.setattr(archive.DailyPublicationArchive,'capture',lambda *a,**kw:(_ for _ in ()).throw(RuntimeError('test-only interruption')))
    with pytest.raises(RuntimeError):producer.produce(folder,[identity(1)],protocol,provider=Native(clock))
    assert (folder/'producer-current-intent.json').exists()
    monkeypatch.setattr(archive.DailyPublicationArchive,'capture',original)
    clock[0]+=timedelta(minutes=16)
    result,provider,_,_=run(environment,label='after-crash',scope=[identity(1)])
    assert result['decision']['prior_intents']==1 and provider.calls==['1']


@pytest.mark.parametrize('mutation',['decision','tasks','source_gate','before_bytes'])
def test_producer_replay_rejects_changed_decision_or_source_even_after_outer_rehash(environment,mutation):
    result,_,folder,inventory=run(environment)
    if mutation=='tasks':
        file=folder/'producer-current-intent.json'
        value=json.loads(file.read_text(encoding='utf-8'));value['tasks'][0]['symbol_id']=1
    elif mutation=='before_bytes':
        file=folder/'producer-before-snapshot.json'
        value=json.loads(file.read_text(encoding='utf-8'));value['scope']={'1':300}
    else:
        file=folder/'publication-producer.json'
        value=json.loads(file.read_text(encoding='utf-8'))
        if mutation=='decision':value['decision']['pending_codes']=[]
        else:value['source_gate']['last_admitted_at']='2099-01-01T14:00:00+03:30'
    file.write_text(json.dumps(value),encoding='utf-8')
    inventory[file.name]=hashlib.sha256(file.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        producer.replay_producer(folder,[identity(1),identity(2)],environment[2],allowed_files=inventory,observed_at=NOW)


def test_registered_radar_actually_invokes_native_producer_and_does_not_repeat_it(environment,monkeypatch):
    from ops import opportunity_radar as radar
    from tests.test_opportunity_radar import PublicNews, PublicProvider

    root,clock,_=environment
    monkeypatch.setattr(radar,'now_tehran',lambda:clock[0])
    monkeypatch.setattr(radar,'now_tehran_iso',lambda:clock[0].isoformat())
    class NativeRadar(PublicProvider):
        supports_dated_publications=True
        def __init__(self):
            super().__init__()
            self.native=Native(clock)
        def get_daily_publication_observation(self,*args):return self.native.get_daily_publication_observation(*args)
    provider=NativeRadar()
    protocol=radar.activate(root,seed=[identity()])
    first,_=radar.collect_cycle(root,protocol,provider=provider,codal=PublicNews(),price_scan=True,bootstrap=True)
    assert first['publication_producer']['decision']['mode']=='capture'
    assert len(provider.native.calls)==25
    assert first['price_composition']['statuses']=={'appended_whole_dated_candle':25}
    assert all(row['status']=='observed'for row in first['rows'])
    clock[0]+=timedelta(minutes=16)
    second,_=radar.collect_cycle(root,protocol,provider=provider,codal=PublicNews(),price_scan=True)
    assert second['publication_producer']['decision']['mode']=='no_unobserved_price_tasks'
    assert len(provider.native.calls)==25
    checked=radar.verify(root)
    assert checked['verified_cycles']==2 and checked['replayed_instruments']==50
    assert not settings.DB_PATH.exists()


def test_midnight_during_history_collection_keeps_the_published_session_pinned(environment,monkeypatch):
    from ops import opportunity_radar as radar
    from tests.test_opportunity_radar import PublicNews, PublicProvider

    root,clock,_=environment
    clock[0]=datetime.fromisoformat('2024-01-03T23:59:00+03:30')
    monkeypatch.setattr(radar,'now_tehran',lambda:clock[0])
    monkeypatch.setattr(radar,'now_tehran_iso',lambda:clock[0].isoformat())
    class Midnight(PublicProvider):
        supports_dated_publications=True
        def get_daily_publication_observation(self,*args):return Native(clock).get_daily_publication_observation(*args)
        def get_symbol_history(self,*args,**kwargs):
            clock[0]=datetime.fromisoformat('2024-01-04T00:01:00+03:30')
            return super().get_symbol_history(*args,**kwargs)
    protocol=radar.activate(root,seed=[identity()])
    value,_=radar.collect_cycle(root,protocol,provider=Midnight(),codal=PublicNews(),price_scan=True,bootstrap=True)
    assert value['observed_at'].startswith('2024-01-04')
    assert value['price_session_date']=='2024-01-03'
    assert value['publication_producer']['session_date']=='2024-01-03'
    assert value['price_composition']['requested_session']=='2024-01-03'
    assert radar.verify(root)['replayed_instruments']==25
