"""Concurrent WAL commits cannot mix a chain head with a later replay or cache."""

import copy
import sqlite3
from datetime import timedelta

import pytest

from core.opportunity_radar import stamp
from database.db import DatabaseManager
from database.opportunity_radar import RadarRepository, read_snapshot
from ops import opportunity_radar as radar
from tests.test_opportunity_radar import (
    PublicNews,
    identity,
    protocol,
    report,
)
from tests.test_opportunity_radar import (
    radar_root as radar_root,
)
from tests.test_radar_history_evidence import NativeProvider


def test_latest_uses_same_snapshot_for_cycle_and_episode_cache(tmp_path,monkeypatch):
    path=tmp_path/'radar.db'
    with DatabaseManager(path) as writer:
        target=RadarRepository(writer)
        plan=protocol()
        target.activate(plan)
        first=target.append(plan['id'],report())
        with DatabaseManager(path,read_only=True) as reader:
            observer=RadarRepository(reader)
            original=observer._episodes
            commits=[]
            def concurrent_episodes(run_id):
                if not commits:
                    commits.append(target.append(run_id,report(at='2023-04-10T15:00:00+03:30',active=False)))
                return original(run_id)
            monkeypatch.setattr(observer,'_episodes',concurrent_episodes)
            actual=observer.latest(plan['id'])
            assert actual['cycle_id']==first['cycle_id']
            assert any(item['active'] for item in actual['episodes'])
            assert not reader.conn.in_transaction
            assert observer.latest(plan['id'])['cycle_id']==commits[0]['cycle_id']


def test_repository_verify_pins_snapshot_through_final_latest_check(tmp_path,monkeypatch):
    path=tmp_path/'radar.db'
    with DatabaseManager(path) as writer:
        target=RadarRepository(writer)
        plan=protocol()
        target.activate(plan)
        first=target.append(plan['id'],report())
        with DatabaseManager(path,read_only=True) as reader:
            observer=RadarRepository(reader)
            original=observer.latest
            commits=[]
            def concurrent_latest(run_id):
                if not commits:
                    commits.append(target.append(run_id,report(at='2023-04-10T15:00:00+03:30',active=False)))
                return original(run_id)
            monkeypatch.setattr(observer,'latest',concurrent_latest)
            checked=observer.verify(plan['id'])
            assert checked['verified_cycles']==1
            assert checked['chain_sha256']==first['chain_sha256']
            assert not reader.conn.in_transaction
            assert observer.verify(plan['id'])['verified_cycles']==2


def _next_report(previous):
    value=copy.deepcopy(previous)
    value['observed_at']=(stamp(previous['observed_at'])+timedelta(minutes=1)).isoformat()
    value['rows'][0]['reasons']['breakout']=True
    return value


def test_status_does_not_mix_old_latest_with_new_events(radar_root,monkeypatch):
    plan=radar.activate(radar_root,seed=[identity()])
    previous,first=radar.collect_cycle(radar_root,plan,provider=NativeProvider(),codal=PublicNews(),
                                      price_scan=True,bootstrap=True)
    before=radar.status(radar_root)
    original=RadarRepository.latest
    commits=[]
    def concurrent_latest(observer,run_id):
        latest=original(observer,run_id)
        if not commits:
            with DatabaseManager(radar_root/'radar.db') as writer:
                commits.append(RadarRepository(writer).append(run_id,_next_report(previous)))
        return latest
    monkeypatch.setattr(RadarRepository,'latest',concurrent_latest)
    observed=radar.status(radar_root)
    assert observed['latest']['cycle_id']==first['cycle_id']
    assert observed['events']==before['events']
    after=radar.status(radar_root)
    assert after['latest']['cycle_id']==commits[0]['cycle_id']
    assert len(after['events'])>len(before['events'])


def test_verify_chain_and_native_replay_use_same_snapshot(radar_root,monkeypatch):
    plan=radar.activate(radar_root,seed=[identity()])
    previous,first=radar.collect_cycle(radar_root,plan,provider=NativeProvider(),codal=PublicNews(),
                                      price_scan=True,bootstrap=True)
    original=RadarRepository.verify
    commits=[]
    def concurrent_verify(observer,run_id):
        checked=original(observer,run_id)
        if not commits:
            # A genuine next cycle has its own predecessor inventory and retry
            # budget; cloning an old receipt would now correctly fail replay.
            later=stamp(previous['observed_at'])+timedelta(seconds=901)
            monkeypatch.setattr(radar,'now_tehran_iso',lambda:later.isoformat())
            monkeypatch.setattr(radar,'now_tehran',lambda:later)
            _,receipt=radar.collect_cycle(radar_root,plan,provider=NativeProvider(),codal=PublicNews(),
                                         price_scan=True,bootstrap=True)
            commits.append(receipt)
        return checked
    monkeypatch.setattr(RadarRepository,'verify',concurrent_verify)
    observed=radar.verify(radar_root)
    assert observed['verified_cycles']==1
    assert observed['chain_sha256']==first['chain_sha256']
    assert observed['replayed_instruments']==25
    assert observed['replayed_native_histories']==25
    after=radar.verify(radar_root)
    assert after['verified_cycles']==2
    assert after['replayed_instruments']==50
    assert after['replayed_native_histories']==50


@pytest.mark.parametrize('method',['latest','verify'])
def test_repository_read_preserves_callers_pending_write(tmp_path,method):
    path=tmp_path/'radar.db'
    with DatabaseManager(path) as writer:
        target=RadarRepository(writer)
        plan=protocol()
        target.activate(plan)
        target.append(plan['id'],report())
        writer.conn.execute('CREATE TABLE caller_pending(value INTEGER)')
        writer.conn.execute('INSERT INTO caller_pending VALUES(1)')
        assert writer.conn.in_transaction
        getattr(target,method)(plan['id'])
        assert writer.conn.in_transaction
        assert writer.conn.execute('SELECT COUNT(*) FROM caller_pending').fetchone()[0]==1
        with DatabaseManager(path,read_only=True) as reader:
            assert reader.conn.execute('SELECT COUNT(*) FROM caller_pending').fetchone()[0]==0
        writer.conn.rollback()
        assert writer.conn.execute('SELECT COUNT(*) FROM caller_pending').fetchone()[0]==0


@pytest.mark.parametrize('caller_transaction',[False,True])
def test_nested_snapshot_error_releases_only_its_own_transaction(caller_transaction):
    connection=sqlite3.connect(':memory:')
    try:
        connection.execute('CREATE TABLE pending(value INTEGER)')
        if caller_transaction:
            connection.execute('INSERT INTO pending VALUES(1)')
        with pytest.raises(RuntimeError,match='reader failed'):
            with read_snapshot(connection):
                with read_snapshot(connection):
                    assert connection.in_transaction
                    raise RuntimeError('reader failed')
        assert connection.in_transaction is caller_transaction
        assert connection.execute('SELECT COUNT(*) FROM pending').fetchone()[0]==int(caller_transaction)
    finally:
        connection.close()
