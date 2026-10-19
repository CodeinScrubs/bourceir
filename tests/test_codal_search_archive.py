"""پرونده جست‌وجوی آزمونی فقط در ریشه موقت؛ بازبین شبکه/DB/کد ناشناس اجرا نمی‌کند."""

import hashlib
import json
from pathlib import Path

import pytest

from config import settings
from core.codal_search import FALSE_FLAGS, search_request
from ops import codal_search as archive
from tests.test_codal_search import observation, row

INSTRUMENT=dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300)


class Provider:
    def __init__(self,pages=None):self.pages=pages or {1:[row()],2:[]};self.calls=[]
    def get_historical_search_observation(self,symbol_fa,**kwargs):
        self.calls.append(kwargs['page']);req=search_request(symbol_fa,cutoff='2025-03-21',**kwargs)
        values=self.pages.get(kwargs['page'],[])
        if values is None:return None
        meta,body=observation(payload=dict(IsAttacker=False,Letters=values,Page=1,Total=1),req=req)
        return dict(meta,content=body)


@pytest.fixture
def root(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data');monkeypatch.setattr(settings,'DB_PATH',tmp_path/'data/brs.db')
    monkeypatch.setattr(archive.time,'sleep',lambda _seconds:None)
    return tmp_path


def capture(provider=None,**changes):
    kwargs=dict(start='2024-01-01',end_exclusive='2024-04-01',max_pages=3);kwargs.update(changes)
    return archive.capture_search(provider or Provider(),INSTRUMENT,**kwargs)


def rehash(folder,name,change):
    folder=Path(folder);path=folder/name;value=json.loads(path.read_bytes());change(value)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest=json.loads((folder/'manifest.json').read_bytes());manifest['files']=archive._files(folder)
    (folder/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')


def test_plan_is_written_before_network_and_stop_keeps_all_planned_slots(root):
    class Planned(Provider):
        def get_historical_search_observation(self,*args,**kwargs):
            plans=list((settings.DATA_DIR/'private/codal-search').glob('SEARCH-*/plan.json'))
            assert len(plans)==1 and len(json.loads(plans[0].read_bytes())['requests'])==3
            return super().get_historical_search_observation(*args,**kwargs)
    provider=Planned();result=capture(provider);review=archive.review_search(result['folder'])
    assert provider.calls==[1,2] and review['assessments_recomputed'] and review['capture_code_matches_current']
    summary=review['summary'];assert summary['attempted_pages']==2 and summary['not_requested_pages']==1
    assert summary['stop_reason']=='observed_empty_page' and len(summary['letters'])==1
    assert not review['database_read'] and not review['network_requested'] and not review['external_authenticity_verified']
    assert all(summary[k] is False for k in FALSE_FLAGS)


def test_failed_attempt_is_in_denominator_and_not_retried(root):
    provider=Provider({1:None});result=capture(provider);summary=result['summary']
    assert provider.calls==[1] and summary['attempted_pages']==1 and summary['not_requested_pages']==2
    assert summary['status_counts']=={'unavailable':1,'not_requested_after_stop':2}
    assert summary['source_total_claims']==[] and summary['letters']==[]
    assert archive.review_search(result['folder'])['assessments_recomputed']


@pytest.mark.parametrize('failure',[FileNotFoundError,TimeoutError,ValueError,NotImplementedError])
def test_provider_error_subclasses_remain_reviewable(root,failure):
    class Failed(Provider):
        def get_historical_search_observation(self,*_a,**_kw):raise failure('test')
    result=capture(Failed());assert archive.review_search(result['folder'])['summary']['stop_reason']=='unavailable'


def test_full_page_capacity_is_not_complete_history_and_repeated_pages_do_not_inflate_letters(root):
    provider=Provider({1:[row()],2:[row()],3:[row()]});result=capture(provider)
    assert provider.calls==[1,2,3] and result['summary']['page_capacity_reached']
    assert len(result['summary']['letters'])==1 and result['summary']['duplicate_rows']==2
    assert result['summary']['complete_history_verified'] is False


def test_cross_page_conflict_has_all_origins_but_no_winning_letter(root):
    result=capture(Provider({1:[row()],2:[row(Title='اصلاحیه')],3:[]}))
    assert result['summary']['conflicting_tracing_numbers']==[123] and result['summary']['letters']==[]
    assert archive.review_search(result['folder'])['summary']==result['summary']


@pytest.mark.parametrize('kind',['promote','scope','cutoff','skip_attempt','extra'])
def test_rehashed_outer_manifest_cannot_change_acceptance_scope_or_denominator(root,kind):
    result=capture();folder=Path(result['folder'])
    if kind=='promote':rehash(folder,'result.json',lambda v:v['summary'].update(accepted_for_signals=True))
    if kind=='scope':rehash(folder,'plan.json',lambda v:v['instrument'].update(symbol_fa='دیگر'))
    if kind=='cutoff':rehash(folder,'plan.json',lambda v:v.update(cutoff='2030-01-01'))
    if kind=='skip_attempt':rehash(folder,'page-001.json',lambda v:v.update(requested=False,observation=None))
    if kind=='extra':
        (folder/'extra.json').write_text('{}',encoding='utf-8');rehash(folder,'result.json',lambda _v:None)
    with pytest.raises(ValueError):archive.review_search(folder)


def test_raw_receipt_replay_cannot_be_skipped_by_rehashing_the_raw_file(root):
    result=capture();folder=Path(result['folder'])
    body=folder/'raw/page-001.json';body.write_bytes(body.read_bytes().replace('آگهی'.encode(),'تغییر'.encode()))
    rehash(folder,'page-001.json',lambda v:v['observation'].update(body_sha256=hashlib.sha256(body.read_bytes()).hexdigest(),response_bytes=body.stat().st_size))
    with pytest.raises(ValueError):archive.review_search(folder)


def test_capture_only_archived_code_is_not_executed_or_authenticated(root):
    result=capture();folder=Path(result['folder']);name='data_collector/codal.py'
    (folder/'code'/name).write_text("raise RuntimeError('must not execute')\n",encoding='utf-8')
    rehash(folder,'plan.json',lambda v:v['code_sha256'].update({name:archive._sha(folder/'code'/name)}))
    review=archive.review_search(folder)
    assert not review['capture_code_matches_current'] and review['semantic_code_matches_current']
    assert not review['external_authenticity_verified']


def test_semantic_interpreter_changes_require_explicit_reassessment(root):
    result=capture();folder=Path(result['folder']);name='core/codal_search.py'
    (folder/'code'/name).write_text('raise RuntimeError()\n',encoding='utf-8')
    rehash(folder,'plan.json',lambda v:v['code_sha256'].update({name:archive._sha(folder/'code'/name)}))
    with pytest.raises(ValueError,match='تفسیر'):archive.review_search(folder)


def test_code_change_during_capture_leaves_failed_attempts_without_success(root,monkeypatch):
    original=archive._code
    class Changing(Provider):
        def get_historical_search_observation(self,*a,**kw):
            result=super().get_historical_search_observation(*a,**kw)
            monkeypatch.setattr(archive,'_code',lambda n:b'changed' if n=='ops/codal_search.py' else original(n))
            return result
    with pytest.raises(ValueError,match='کد'):capture(Changing())
    folder,=(settings.DATA_DIR/'private/codal-search').glob('SEARCH-*')
    assert (folder/'failed.json').is_file() and (folder/'raw/page-001.json').is_file() and not (folder/'manifest.json').exists()


def test_future_receipt_is_rejected_before_metadata_interpretation(root,monkeypatch):
    class Future(Provider):
        def get_historical_search_observation(self,*a,**kw):
            value=super().get_historical_search_observation(*a,**kw)
            return dict(value,received_at='2999-01-01T00:00:00+03:30')
    monkeypatch.setattr(archive,'assess_search',lambda *_a:pytest.fail('future receipt interpreted'))
    with pytest.raises(ValueError,match='زمان'):capture(Future())


def test_cli_date_guard_runs_before_database_or_provider(root,monkeypatch):
    from database.db import DatabaseManager
    monkeypatch.setattr(DatabaseManager,'__init__',lambda *_a,**_kw:pytest.fail('database opened'))
    with pytest.raises(ValueError):archive.main(['capture','--symbol','آزمون','--start','2025-03-20','--end-exclusive','2025-03-22'])


def test_instrument_type_and_page_capacity_are_validated_without_database(root):
    for kind in (309,True,'300'):
        with pytest.raises(ValueError):archive.capture_search(Provider(),dict(INSTRUMENT,instrument_type_code=kind),start='2024-01-01',end_exclusive='2024-04-01')
    with pytest.raises(ValueError):capture(max_pages=True)


def test_cli_reads_only_identity_columns_and_never_calls_full_symbol_reader(root,monkeypatch,capsys):
    from data_collector import codal
    from database import db as database
    settings.DB_PATH.parent.mkdir(parents=True,exist_ok=True);settings.DB_PATH.write_bytes(b'test-only DB stand-in')
    class Minimal:
        def __init__(self,*,read_only):assert read_only;self.conn=self
        def __enter__(self):return self
        def __exit__(self,*_a):pass
        def get_symbol(self,*_a):pytest.fail('full metadata reader used')
        def execute(self,sql,params):
            assert sql=='SELECT symbol_fa,ins_code,instrument_type_code FROM symbols WHERE symbol_fa=?'
            assert params==('آزمون',);return self
        def fetchone(self):return INSTRUMENT
    class FailedProvider:
        def __init__(self):self.session=self
        def close(self):pass
        def get_historical_search_observation(self,*_a,**_kw):return None
    monkeypatch.setattr(database,'DatabaseManager',Minimal);monkeypatch.setattr(codal,'CodalProvider',FailedProvider)
    archive.main(['capture','--symbol','آزمون','--start','2024-01-01','--end-exclusive','2024-04-01'])
    assert json.loads(capsys.readouterr().out)['summary']['attempted_pages']==1
