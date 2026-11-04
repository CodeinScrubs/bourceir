"""An unclassified native identity is present even when strict scope rejects it."""

import copy
import shutil
from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

from core.opportunity_radar import scope_rows, stamp
from dashboard import opportunity_radar as panel
from data_collector.tsetmc import TsetmcProvider
from database.db import DatabaseManager
from database.opportunity_radar import RadarRepository
from ops import opportunity_radar as radar
from ops.report_files import write_private_report
from tests.test_opportunity_radar import PublicNews, PublicProvider, identity
from tests.test_opportunity_radar import radar_root as radar_root


def native(i,**changes):
    return dict(insCode=str(i),lva=identity(i)['symbol_fa'],yVal=300,dEven=0)|changes


def observation(rows):
    provider=TsetmcProvider(realtime=True)
    provider._get_json=lambda *args,**kwargs:dict(marketwatch=rows)
    try:
        return provider.get_market_observation()
    finally:
        provider.close()


@pytest.mark.parametrize('kind',['absent',None,0,True,-1,'invalid',300.5])
def test_native_presence_never_inherits_registry_type_or_implies_admission(kind):
    unknown=native(1,yVal=kind)
    if kind=='absent':
        unknown.pop('yVal')
    result=radar.watch_coverage(observation([unknown,native(2),native(4,yVal=309)]),
                               [identity(i) for i in range(1,5)])
    assert result['source_scope_count']==1
    assert result['known_native_present_codes']==['1','2','4']
    assert result['known_native_absent_codes']==['3']
    assert result['known_native_present_unclassified_codes']==['1']
    assert result['known_native_present_outside_scope_codes']==['4']
    assert result['known_not_admitted_codes']==result['known_missing_from_watch']==['1','3','4']
    assert 'not native-payload absence' in result['known_missing_from_watch_semantics']


def test_native_code_without_symbol_is_present_but_not_admitted():
    result=radar.watch_coverage(observation([native(1,lva='')]),[identity()])
    assert result['known_native_present_codes']==['1']
    assert result['known_native_absent_codes']==[]
    assert result['known_native_present_unclassified_codes']==[]
    assert result['known_not_admitted_codes']==['1'] and result['source_scope_count']==0


@pytest.mark.parametrize('value',[None,{},dict(payload=[]),dict(payload=dict(marketwatch={})),
                                   dict(payload=dict(marketwatch=[None]))])
def test_unavailable_or_malformed_native_payload_does_not_mean_all_known_codes_absent(value):
    result=radar.watch_coverage(value,[identity()])
    assert result['native_presence_available'] is False
    assert result['known_native_present_codes'] is None
    assert result['known_native_absent_codes'] is None
    assert result['known_native_present_unclassified_codes'] is None


def test_explicit_empty_native_list_is_distinct_from_missing_native_payload():
    result=radar.watch_coverage(observation([]),[identity()])
    assert result['native_presence_available'] is True
    assert result['native_row_count']==0 and result['known_native_present_codes']==[]
    assert result['known_native_absent_codes']==['1']


def test_duplicates_and_malformed_codes_do_not_inflate_identity_counts_or_admission():
    raw=[native(1),native(1,yVal=303)]
    raw.extend(native(2,insCode=value) for value in (None,True,0,'001','1/2',1.5,'۱۲۳'))
    result=radar.watch_coverage(observation(raw),[identity(),identity(2)])
    assert result['native_row_count']==9
    assert result['native_invalid_identity_rows']==7 and result['native_duplicate_identity_rows']==1
    assert result['known_native_present_codes']==['1'] and result['known_native_absent_codes']==['2']
    assert result['known_native_present_unclassified_codes']==['1']
    assert result['source_scope_count']==0


def test_newly_admitted_identity_is_compared_to_merged_retained_scope():
    watch=observation([native(1),native(2)])
    admitted,_=radar.market_scope(watch)
    merged=scope_rows([identity(),*[item['metadata'] for item in admitted]])
    result=radar.watch_coverage(watch,merged)
    assert result['retained_scope_count']==2
    assert result['known_native_present_codes']==['1','2']
    assert result['known_native_absent_codes']==result['known_not_admitted_codes']==[]


class MixedProvider(PublicProvider):
    def get_market_observation(self):
        return observation([native(i,**(dict(yVal=None) if i==1 else {})) for i in [*range(1,26),27]])


def test_collect_and_verify_bind_presence_and_scope_to_same_archived_payload(radar_root):
    frozen=radar.activate(radar_root,seed=[identity(),identity(26)])
    out,_=radar.collect_cycle(radar_root,frozen,provider=MixedProvider(),codal=PublicNews(),
                              price_scan=True,bootstrap=True)
    coverage=out['source_coverage']
    assert coverage['retained_scope_count']==27 and coverage['source_scope_count']==25
    assert coverage['known_native_absent_codes']==['26']
    assert coverage['known_native_present_unclassified_codes']==['1']
    assert coverage['known_not_admitted_codes']==['1','26']
    assert radar.verify(radar_root)['replayed_instruments']==27
    incorrect=copy.deepcopy(out)
    incorrect['observed_at']=(stamp(out['observed_at'])+timedelta(minutes=1)).isoformat()
    incorrect['source_coverage']['known_native_absent_codes']=['1','26']
    with DatabaseManager(radar_root/'radar.db') as db:
        RadarRepository(db).append(frozen['id'],incorrect)
    with pytest.raises(ValueError,match='watch coverage differs'):
        radar.verify(radar_root)


def test_legacy_coverage_is_not_reinterpreted_or_rewritten(radar_root,monkeypatch):
    frozen=radar.activate(radar_root,seed=[identity()])
    legacy=dict(frozen,id='RADAR-legacy-coverage')
    for key in ('watch_coverage_version','continuity_version','news_boundary_registration','price_retry_version'):
        del legacy[key]
    shutil.copytree(radar_root/frozen['id'],radar_root/legacy['id'])
    write_private_report(radar_root/legacy['id']/'protocol.json',legacy)
    with DatabaseManager(radar_root/'radar.db') as db:
        RadarRepository(db).activate(legacy)
    modern=radar.watch_coverage
    keys={'public_watch_available','source_scope_count','retained_scope_count','exclusions',
          'known_missing_from_watch','historical_membership_verified'}
    monkeypatch.setattr(radar,'watch_coverage',lambda *args:{k:v for k,v in modern(*args).items() if k in keys})
    out,_=radar.collect_cycle(radar_root,legacy,provider=PublicProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    folder=radar_root/out['artifacts_folder']
    before={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
    monkeypatch.setattr(radar,'watch_coverage',lambda *args:pytest.fail('Legacy coverage must not use new semantics'))
    assert radar.verify(radar_root)['replayed_instruments']==25
    assert before=={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}


@pytest.mark.parametrize('legacy',[False,True])
def test_dashboard_distinguishes_present_unknown_type_from_native_absence(monkeypatch,legacy):
    coverage=radar.watch_coverage(observation([native(1,yVal=None)]),[identity(),identity(2)])
    if legacy:
        coverage={k:v for k,v in coverage.items() if k in ('source_scope_count','known_missing_from_watch')}
    at='2026-10-25T13:45:00+03:30'
    report=dict(scope=[],rows=[],price_session_date='2026-10-24',price_observed_at=at,
                news_coverage={},source_coverage=coverage)
    monkeypatch.setattr(panel,'status',lambda:dict(active=True,events=[],latest=dict(report=report,episodes=[]),heartbeat={}))
    monkeypatch.setattr(panel.jobs,'job_status',lambda _:dict(running=False))
    app=AppTest.from_string('from dashboard.opportunity_radar import radar_view\nradar_view()').run()
    assert not app.exception
    captions=[item.value for item in app.caption]
    assert not any('شناسهٔ محفوظ در پاسخ دیده نشده' in text for text in captions)
    if legacy:
        assert any('حضور شناسه‌ها در بدنهٔ بومی این آرشیو معلوم نیست' in text for text in captions)
    else:
        assert any('1 شناسهٔ محفوظ دیده شده و 1 شناسهٔ محفوظ در این بدنه وجود ندارد' in text for text in captions)
        assert any('1 شناسه نامعلوم یا متناقض است' in text for text in captions)
