"""The recorder must preserve unknowns, time, the full census and first detection."""

import sqlite3
from datetime import timedelta

import pandas as pd
import pytest

from config import settings
from core.news_continuity import NewsWindow
from core.opportunity_radar import (
    REASONS,
    VERSION,
    notice_records,
    observe_history,
    scope_rows,
)
from database.opportunity_radar import RadarRepository
from ops import opportunity_radar as radar
from research.surge_patterns import build_pre_move_features, pattern_masks
from tests.test_surge_patterns import source


def identity(i=1, **extra):
    return dict(symbol_fa='الف'+chr(1575+i),ins_code=str(i),instrument_type_code=300,**extra)


def protocol():
    return dict(id='test-radar',version=VERSION,registered_at='2023-04-10T13:00:00+03:30',accepted_for_signals=False)


def report(at='2023-04-10T14:00:00+03:30', *, active=True, date='2023-04-10'):
    return dict(version=VERSION,run_id='test-radar',observed_at=at,price_scan=True,scope=[identity()],
        rows=[dict(symbol=identity()['symbol_fa'],ins_code='1',date=date,
                   reasons=dict(breakout=active,value_surge=None,locked_up=False))],
        notices=[],accepted_for_signals=False)


def test_scope_keeps_inactive_unknown_sector_and_renamed_id_without_funds_or_numeric_suffix():
    result=scope_rows([identity(is_active=0),dict(identity(2),symbol_fa='شماره2'),
        dict(identity(3),instrument_type_code=305),dict(identity(),symbol_fa='نامجدید')])
    assert len(result)==1 and result[0]['symbol_fa']=='نامجدید'
    assert identity()['symbol_fa'] in result[0]['previous_symbols']
    assert scope_rows(result)==result
    ambiguous=scope_rows([identity(),dict(identity(2),symbol_fa=identity()['symbol_fa'])])
    assert len(ambiguous)==2 and all(r['symbol_identity_ambiguous'] for r in ambiguous)
    letter=dict(symbol_fa=identity()['symbol_fa'],tracing_no=1,title='افشا',
        url='https://codal.ir/Reports/Decision.aspx',published_at='2023-04-10T14:00:00+03:30')
    assert notice_records(dict(letters=[letter]),ambiguous,received_at='2023-04-10T15:00:00+03:30')==([],1)


def test_factual_flags_match_previously_studied_definitions_without_research_holdout_bypass():
    prices,days=source()
    prices.loc[99,'volume']*=4
    prices.loc[99,'value']*=4
    reference=build_pre_move_features(prices,prices[['symbol','date']],days,min_cross_section=1)
    masks=pattern_masks(reference).iloc[-1]
    facts=observe_history(prices,days,session_date=days[-1])
    for key in REASONS:
        assert facts['reasons'][key]==bool(masks[key])
    assert facts['value_ratio_prior20']==pytest.approx(reference.value_ratio.iloc[-1])
    assert facts['breakout_ratio_prior60']==pytest.approx(reference.breakout_60.iloc[-1])
    assert facts['accepted_for_signals'] is False and facts['probability_of_surge'] is None


@pytest.mark.parametrize('column,value',[('volume',0),('volume',2.5),('value',1e20),
    ('ref_price_raw',0),('close_raw',999999),('close_final_raw',True)])
def test_invalid_current_bar_never_becomes_a_positive_attention(column,value):
    prices,days=source()
    prices[column]=prices[column].astype(object)
    prices.loc[99,column]=value
    facts=observe_history(prices,days,session_date=days[-1])
    assert facts['status']=='invalid_current_bar'
    assert all(v is None for v in facts['reasons'].values())


def test_missing_history_stale_and_recent_gap_stay_unknown_not_compressed():
    prices,days=source()
    assert observe_history(None,days,session_date=days[-1])['status']=='unavailable'
    assert observe_history(prices.iloc[:-1],days,session_date=days[-1])['status']=='stale_session'
    assert observe_history(prices.drop(index=90),days,session_date=days[-1])['reasons']['breakout'] is None
    assert observe_history(prices.drop(index=90),days,session_date=days[-1])['reasons']['value_surge'] is None
    assert observe_history(prices.iloc[:3],days[:3],session_date=days[2])['reasons']['breakout'] is None


def test_raw_pit_reference_reset_preserves_flags_and_chart_adjustments_are_unused():
    prices,days=source()
    before=observe_history(prices,days,session_date=days[-1])
    for column in ['open_raw','high_raw','low_raw','close_raw','close_final_raw','ref_price_raw']:
        prices.loc[50:,column]/=2
    prices.loc[50:,'volume']*=2
    prices['adj_close']=1e12
    after=observe_history(prices,days,session_date=days[-1])
    assert before['reasons']==after['reasons']
    assert before['breakout_ratio_prior60']==pytest.approx(after['breakout_ratio_prior60'])


def test_future_ambiguous_date_and_duplicate_input_fail_closed():
    prices,days=source()
    assert observe_history(prices,days[:-1],session_date=days[-2])['status']=='invalid_history'
    assert observe_history(pd.concat([prices,prices.tail(1)]),days,session_date=days[-1])['status']=='invalid_history'
    with pytest.raises(ValueError):
        observe_history(prices,days[::-1],session_date=days[-1])


def test_notice_is_neutral_dated_matched_revisions_are_distinct_and_untrusted_url_rejected():
    letter=dict(symbol_fa=identity()['symbol_fa'],tracing_no=1,title='افزایش نرخ',
        url='https://codal.ir/Reports/Decision.aspx?LetterSerial=public',published_at='2023-04-10T14:00:00+03:30')
    notices,excluded=notice_records(dict(letters=[letter,dict(letter,url='https://codal.ir.evil.test/')]),
        [identity()],received_at='2023-04-10T14:01:00+03:30')
    assert len(notices)==1 and excluded==1
    assert notices[0]['financial_improvement_verified'] is False and notices[0]['document_reviewed'] is False
    later,_=notice_records(dict(letters=[letter]),[identity()],received_at='2023-04-10T15:00:00+03:30')
    assert notices[0]['notice_key']==later[0]['notice_key']
    revised,_=notice_records(dict(letters=[dict(letter,title='اصلاحیه')]),[identity()],received_at='2023-04-10T15:00:00+03:30')
    assert notices[0]['notice_key']!=revised[0]['notice_key']
    future,_=notice_records(dict(letters=[letter]),[identity()],received_at='2023-04-10T13:00:00+03:30')
    assert future[0]['publication_time_usable'] is False
    assert future[0]['notice_key']==notices[0]['notice_key']  # receipt-clock state is not a source revision


def test_episode_first_detection_survives_restart_gap_and_repeat_without_spamming(db):
    repo=RadarRepository(db); repo.activate(protocol())
    first=repo.append('test-radar',report())
    assert first['new_attention']==1
    same=repo.append('test-radar',report('2023-04-10T14:05:00+03:30'))
    assert not same['events']
    RadarRepository(db).append('test-radar',report('2023-04-10T14:10:00+03:30',active=None))
    resumed=RadarRepository(db).append('test-radar',report('2023-04-10T14:15:00+03:30'))
    assert resumed['events'][0]['kind']=='attention_observed_after_gap'
    assert resumed['events'][0]['first_seen_at']=='2023-04-10T14:00:00+03:30'
    repo.append('test-radar',report('2023-04-10T14:20:00+03:30',active=False))
    next_episode=repo.append('test-radar',report('2023-04-10T14:25:00+03:30'))['events'][0]
    assert next_episode['first_seen_at']=='2023-04-10T14:25:00+03:30'
    assert next_episode['first_ever_seen_at']=='2023-04-10T14:00:00+03:30'
    assert repo.verify('test-radar')['verified_cycles']==6
    assert not db.conn.execute('SELECT * FROM journal_executions').fetchall()


def test_baseline_is_not_fresh_and_new_identity_is_retained_event(db):
    repo=RadarRepository(db); repo.activate(protocol())
    first=repo.append('test-radar',report(date='2023-04-09'))
    assert first['new_attention']==0 and first['events'][0]['baseline']
    next_report=report('2023-04-10T15:00:00+03:30',date='2023-04-09')
    next_report['scope'].append(identity(2))
    second=repo.append('test-radar',next_report)
    assert second['new_attention']==1 and second['events'][0]['kind']=='new_scope_identity'


def test_transaction_rollback_tamper_immutable_ledger_and_monotonic_clock(db):
    repo=RadarRepository(db); repo.activate(protocol()); repo.append('test-radar',report())
    with pytest.raises(ValueError,match='rollback'):
        repo.append('test-radar',report('2023-04-10T13:59:00+03:30'))
    assert db.conn.execute('SELECT COUNT(*) FROM radar_cycles').fetchone()[0]==1
    for table in ['radar_runs','radar_cycles','radar_attention_events']:
        with pytest.raises(sqlite3.IntegrityError,match='immutable'):
            db.conn.execute('DELETE FROM '+table)
        db.conn.rollback()
    db.conn.execute("UPDATE radar_episodes SET payload_json='{}'"); db.conn.commit()
    with pytest.raises(ValueError,match='cache'):
        repo.latest('test-radar')
    with pytest.raises(ValueError,match='cache'):
        repo.append('test-radar',report('2023-04-10T15:00:00+03:30'))


def test_notice_deduplication_and_revision_are_persisted_not_dependent_on_process_memory(db):
    repo=RadarRepository(db); repo.activate(protocol())
    letter=dict(symbol_fa=identity()['symbol_fa'],tracing_no=1,title='افشا',
        url='https://codal.ir/Reports/Decision.aspx?LetterSerial=public',published_at='2023-04-10T13:20:00+03:30')
    incoming,_=notice_records(dict(letters=[letter,letter]),[identity()],received_at='2023-04-10T14:00:00+03:30')
    value=report(active=False); value['notices']=incoming
    assert repo.append('test-radar',value)['new_attention']==1
    value['observed_at']='2023-04-10T15:00:00+03:30'
    assert RadarRepository(db).append('test-radar',value)['new_attention']==0
    with pytest.raises(sqlite3.IntegrityError,match='immutable'):
        db.conn.execute('DELETE FROM radar_notice_versions')


@pytest.fixture
def radar_root(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    monkeypatch.setattr(settings,'DB_PATH',tmp_path/'account.db')
    return tmp_path/'data'/'private'/'radar'


class PublicProvider:
    def __init__(self,*,failed=False):
        self.calls=[]; self.failed=failed
    def get_market_observation(self):
        if self.failed:
            raise OSError('source down')
        return dict(instruments=[dict(metadata=identity(i)) for i in range(1,26)])
    def get_symbol_history(self,code,last_n):
        self.calls.append(code)
        if self.failed:
            return None
        return source()[0]
    def close(self):
        pass


class PublicNews:
    def get_news_window(self,*,since,pages,time_budget_seconds,progress):
        return dict(letters=[],pages_fetched=1,reached_end=True,failed_page=None,page_limit_reached=False,
                    window_start=since,window_scan_satisfied=True,checkpoint_through=radar.now_tehran_iso())


class WindowNews:
    def __init__(self, *, fail=False):
        self.fail=fail
        self.windows=[]
    def get_news_window(self, *, since, pages, time_budget_seconds, progress):
        self.windows.append(since)
        at=radar.now_tehran_iso()
        scan=NewsWindow(since=since,requested_at=at)
        first=dict(requested_page=1,status='observed',requested_at=at,received_at=at,raw_count=1,invalid_rows=0,
            url='https://search.codal.ir/api/search/v2/q?&PageNumber=1',http_status=200,
            response_bytes=1,response_sha256='a'*64,total_claim=1,page_count_claim=1,
            letters=[dict(tracing_no=123,symbol_fa=identity()['symbol_fa'],title='افشا',
                url='https://codal.ir/Reports/Decision.aspx',
                published_at=(radar.stamp(at)-timedelta(minutes=1)).isoformat())])
        scan.add(first)
        scan.add(dict(requested_page=2,status='unavailable') if self.fail else
                 dict(requested_page=2,status='observed',requested_at=at,received_at=at,raw_count=0,invalid_rows=0,
                      letters=[],url='https://search.codal.ir/api/search/v2/q?&PageNumber=2',http_status=200,
                      response_bytes=1,response_sha256='b'*64,total_claim=1,page_count_claim=1))
        return scan.result(head_recheck=None if self.fail else first,stop_reason='source_error' if self.fail else 'end')


def test_news_failure_boundary_survives_cycle_and_registered_upgrade_and_replays(radar_root):
    frozen=radar.activate(radar_root,seed=[identity()])
    failed=WindowNews(fail=True)
    first,_=radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=failed)
    assert not first['news_coverage']['window_scan_satisfied']
    upgraded=radar.activate(radar_root,new_run=True)
    assert upgraded['news_initial_since']==frozen['news_initial_since']
    recovered=WindowNews()
    second,_=radar.collect_cycle(radar_root,upgraded,provider=PublicProvider(),codal=recovered)
    assert recovered.windows==failed.windows
    assert second['news_coverage']['window_scan_satisfied']
    third,_=radar.collect_cycle(radar_root,upgraded,provider=PublicProvider(),codal=recovered)
    expected=(radar.stamp(second['news_coverage']['checkpoint_through'])-timedelta(hours=2)).isoformat()
    assert recovered.windows[-1]==expected and third['news_coverage']['window_start']==expected
    assert radar.verify(radar_root)['replayed_news_windows']==2
    assert not settings.DB_PATH.exists()


def test_real_pipeline_interface_archives_all_rows_replays_and_retains_missing_coverage(radar_root):
    seed=[identity(i) for i in range(1,27)]
    frozen=radar.activate(radar_root,seed=seed)
    provider=PublicProvider()
    out,receipt=radar.collect_cycle(radar_root,frozen,provider=provider,codal=PublicNews(),price_scan=True,bootstrap=True)
    assert len(out['scope'])==len(out['rows'])==len(provider.calls)==26
    assert out['source_coverage']['known_missing_from_watch']==['26']
    assert receipt['new_attention']==0
    assert radar.verify(radar_root)['replayed_instruments']==26
    assert radar.status(radar_root)['latest']['report']['price_session_date']=='2023-04-10'
    assert not settings.DB_PATH.exists()
    folder=radar_root/out['artifacts_folder']
    (folder/'market-source.json').write_text('{}')
    with pytest.raises(ValueError,match='artifact'):
        radar.verify(radar_root)


def test_outage_retains_entire_scope_has_unknowns_and_stops_request_storm(radar_root):
    frozen=radar.activate(radar_root,seed=[identity(i) for i in range(1,31)])
    provider=PublicProvider(failed=True)
    out,_=radar.collect_cycle(radar_root,frozen,provider=provider,codal=PublicNews(),price_scan=True,bootstrap=True)
    assert len(out['rows'])==30 and len(provider.calls)==5
    assert all(r['status']=='unavailable' and all(v is None for v in r['reasons'].values()) for r in out['rows'])
    assert len(out['price_request_problems'])==30
    assert radar.status(radar_root)['heartbeat']['status']=='degraded'
    assert radar.verify(radar_root)['replayed_instruments']==30


def test_code_change_requires_new_registered_run_and_lock_refuses_concurrent_writer(radar_root,monkeypatch):
    first=radar.activate(radar_root,seed=[identity()])
    assert radar.activate(radar_root)['id']==first['id']
    monkeypatch.setattr(radar,'code_identity',lambda:dict(changed='yes'))
    with pytest.raises(ValueError,match='code changed'):
        radar.activate(radar_root)
    assert radar.activate(radar_root,new_run=True,seed=[identity()])['id']!=first['id']
    with radar.worker_lock(radar_root), pytest.raises(OSError), radar.worker_lock(radar_root):
        pass


def test_new_run_preserves_discovered_identities_when_live_source_is_unavailable(radar_root,monkeypatch):
    first=radar.activate(radar_root,seed=[identity()])
    previous,_=radar.collect_cycle(radar_root,first,provider=PublicProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    later=radar.stamp(previous['observed_at'])+timedelta(seconds=901)
    monkeypatch.setattr(radar,'now_tehran_iso',lambda:later.isoformat())
    monkeypatch.setattr(radar,'now_tehran',lambda:later)
    second=radar.activate(radar_root,new_run=True)
    assert len(second['initial_scope'])==25
    assert second['inherited_scope_runs'][0]['run_id']==first['id']
    assert second['inherited_scope_runs'][0]['verified_cycles']==1
    out,receipt=radar.collect_cycle(radar_root,second,provider=PublicProvider(failed=True),
        codal=PublicNews(),price_scan=True,bootstrap=True)
    assert len(out['scope'])==len(out['rows'])==25
    assert all(row['status']=='unavailable' for row in out['rows'])
    assert not out['price_retry']['deferred'] and out['price_scan']
    assert receipt['new_attention']==0  # Old discoveries are not new predictive alerts.


def test_scope_inheritance_recovers_older_run_and_keeps_current_name(radar_root,monkeypatch):
    from database.db import DatabaseManager

    first=radar.activate(radar_root,seed=[identity(1),identity(2)])
    # Reproduce a legacy restart which registered a narrower census without deleting history.
    legacy=dict(first,id='legacy-narrowed',initial_scope=[identity(1)])
    with DatabaseManager(radar_root/'radar.db') as local:
        RadarRepository(local).activate(legacy)
    monkeypatch.setattr(radar,'registry_seed',lambda:([dict(identity(2),symbol_fa='نام تازه')],{}))
    new=radar.activate(radar_root,new_run=True)
    assert {r['ins_code'] for r in new['initial_scope']}=={'1','2'}
    assert next(r for r in new['initial_scope'] if r['ins_code']=='2')['symbol_fa']=='نام تازه'
    assert len(new['inherited_scope_runs'])==2


def test_scope_inheritance_refuses_corrupt_previous_chain(radar_root):
    from database.db import DatabaseManager

    radar.activate(radar_root,seed=[identity()])
    with DatabaseManager(radar_root/'radar.db') as local:
        local.conn.execute('DROP TRIGGER radar_runs_no_update')
        local.conn.execute("UPDATE radar_runs SET protocol_sha256='broken'")
        local.conn.commit()
    with pytest.raises(ValueError,match='fingerprint'):
        radar.activate(radar_root,new_run=True)
    with DatabaseManager(radar_root/'radar.db',read_only=True) as local:
        assert local.conn.execute('SELECT count(*) FROM radar_runs').fetchone()[0]==1


def test_migration_is_idempotent_and_leaves_existing_paper_and_market_data_unchanged(db):
    sql=(settings.BASE_DIR/'database/migrations/016_opportunity_radar.sql').read_text(encoding='utf-8')
    before=db.conn.execute('SELECT COUNT(*) FROM daily_prices').fetchone()[0]
    db.conn.executescript(sql); db.conn.executescript(sql)
    assert db.conn.execute('SELECT COUNT(*) FROM daily_prices').fetchone()[0]==before


def test_account_file_cannot_be_the_radar_database_even_through_a_hard_link(radar_root):
    import os

    settings.DB_PATH.write_text('account content',encoding='utf-8')
    radar_root.mkdir(parents=True)
    os.link(settings.DB_PATH,radar_root/'radar.db')
    with pytest.raises(ValueError,match='separate'):
        radar.root_path(radar_root)
    assert settings.DB_PATH.read_text(encoding='utf-8')=='account content'


def test_archived_code_is_verified_as_well_as_input_files(radar_root):
    frozen=radar.activate(radar_root,seed=[identity(i) for i in range(1,26)])
    radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    (radar_root/frozen['id']/'code/core/opportunity_radar.py').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError,match='Archived radar code'):
        radar.verify(radar_root)


def test_dashboard_radar_without_network_and_with_archived_prices(radar_root,monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from ops import jobs

    monkeypatch.setattr(jobs,'JOBS_DIR',radar_root/'jobs')
    script=str(settings.BASE_DIR/'dashboard/app.py')
    st.cache_data.clear(); st.cache_resource.clear()
    app=AppTest.from_file(script,default_timeout=60).run()
    app.sidebar.radio[0].set_value('📡 رادار فرصت‌های پژوهشی').run()
    assert not app.exception and any('هنوز نسخه' in w.value for w in app.info)
    # The UI uses its normal default root.
    root=settings.DATA_DIR/'private'/'opportunity-radar'
    frozen=radar.activate(root,seed=[identity(i) for i in range(1,26)])
    radar.collect_cycle(root,frozen,provider=PublicProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    app.run()
    assert not app.exception
    assert len(app.dataframe[0].value)==25
    assert any('مربوط به امروز نیستند' in w.value for w in app.warning)
    st.cache_data.clear(); st.cache_resource.clear()


def test_dashboard_start_stop_state_updates_without_waiting_for_manual_reload(radar_root,monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    from ops import jobs

    state={'running':False}
    original=jobs.job_status
    monkeypatch.setattr(jobs,'JOBS_DIR',radar_root/'jobs')
    monkeypatch.setattr(jobs,'job_status',lambda name: dict(original(name),running=state['running'])
                        if name=='opportunity-radar' else original(name))
    def start(name,args):
        assert name=='opportunity-radar' and args==['radar','watch']
        state['running']=True
        return True
    def stop(name):
        assert name=='opportunity-radar'
        state['running']=False
        return True
    monkeypatch.setattr(jobs,'start_job',start)
    monkeypatch.setattr(jobs,'stop_job',stop)
    st.cache_data.clear(); st.cache_resource.clear()
    app=AppTest.from_file(str(settings.BASE_DIR/'dashboard/app.py'),default_timeout=60).run()
    app.sidebar.radio[0].set_value('📡 رادار فرصت‌های پژوهشی').run()
    next(b for b in app.button if b.label=='شروع پایش سراسری').click().run()
    assert not app.exception and state['running']
    assert not next(b for b in app.button if b.label=='توقف پایش سراسری').disabled
    next(b for b in app.button if b.label=='توقف پایش سراسری').click().run()
    assert not app.exception and not state['running']
    assert not next(b for b in app.button if b.label=='شروع پایش سراسری').disabled
    st.cache_data.clear(); st.cache_resource.clear()
