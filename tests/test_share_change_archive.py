"""گزارش خودبسنده در مسیر موقت؛ بایت نامعتبر، تلاش ناموفق و جعل دوباره هش‌شده."""

import hashlib
import json
import shutil
import socket
from pathlib import Path

import pytest

from config import settings
from ops import share_changes as archive
from ops.capital_actions import create_capital_study
from tests.test_capital_actions import source as source
from tests.test_share_changes import CODE, END, START, observation

INSTRUMENTS=[dict(symbol_fa='آزمون',ins_code=CODE,instrument_type_code=300)]


class Provider:
    def __init__(self,value='valid'):self.value=value;self.calls=[]
    def get_share_change_observation(self,code):
        self.calls.append(code)
        if self.value=='unavailable':return None
        if self.value=='failure':raise OSError('test only')
        meta,body=observation(body=b'{broken' if self.value=='invalid' else None)
        return dict(meta,content=body)


@pytest.fixture
def capital(source):
    return create_capital_study(**source)['folder']


def capture(capital,provider=None):
    return archive.capture_share_changes(Provider() if provider is None else provider,INSTRUMENTS,
        start=START,end_exclusive=END,capital_study=capital,native_event_date='2024-02-27')


def change(folder,name,fn):
    path=Path(folder)/name;data=json.loads(path.read_bytes());fn(data)
    path.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8')
    manifest=Path(folder)/'manifest.json';m=json.loads(manifest.read_bytes());m['files']=archive._all_files(Path(folder))
    manifest.write_text(json.dumps(m),encoding='utf-8')


def test_plan_is_saved_before_request_and_source_replay_needs_no_database_or_network(capital,monkeypatch):
    class Planned(Provider):
        def get_share_change_observation(self,code):
            plans=list((settings.DATA_DIR/'private/share-changes').glob('SHARES-*/plan.json'))
            assert len(plans)==1 and json.loads(plans[0].read_bytes())['requests'][0]['ins_code']==code
            assert not (plans[0].parent/'result.json').exists()
            return super().get_share_change_observation(code)
    monkeypatch.setattr(socket,'create_connection',lambda *_a,**_k:pytest.fail('network called'))
    monkeypatch.setattr('database.db.DatabaseManager.__init__',lambda *_a,**_k:pytest.fail('database opened'))
    result=capture(capital,Planned());review=archive.review_share_changes(result['folder'])
    assert review['comparison']['status']=='source_ratios_agree' and review['assessments_recomputed']
    assert review['database_read'] is False and review['network_requested'] is False
    assert not review['independent_registration_verified'] and not review['accepted_for_signals']


@pytest.mark.parametrize('state,status',[('unavailable','unavailable'),('failure','unavailable'),('invalid','invalid_response')])
def test_failed_attempts_remain_in_the_denominator_without_fabricated_history(capital,state,status):
    result=capture(capital,Provider(state));folder=Path(result['folder'])
    assert len(result['cases'])==1 and result['cases'][0]['assessment']['status']==status
    assert result['comparison']['status']=='not_comparable'
    assert (folder/'raw/observation-000.json').exists()==(state=='invalid')
    assert archive.review_share_changes(folder)['assessments_recomputed']


def test_copy_is_self_contained_after_original_capital_study_is_removed(capital,tmp_path):
    result=capture(capital)
    original=Path(capital).resolve();assert original.is_relative_to(tmp_path.resolve());shutil.rmtree(original)
    assert archive.review_share_changes(result['folder'])['comparison']['status']=='source_ratios_agree'


@pytest.mark.parametrize('kind',['flag','comparison_flag','numeric_type','extra_file','native_date','early_time','source_count','extra_plan'])
def test_rehashed_changes_cannot_promote_scope_or_skip_source_replay(capital,kind):
    result=capture(capital);folder=Path(result['folder'])
    if kind=='flag':change(folder,'result.json',lambda p:p.update(accepted_for_signals=True))
    elif kind=='comparison_flag':change(folder,'result.json',lambda p:p['comparison'].update(holder_credit_verified=True))
    elif kind=='numeric_type':
        change(folder,'result.json',lambda p:p['cases'][0]['assessment']['events'][0]['source_indices'].__setitem__(0,False))
    elif kind=='extra_file':
        (folder/'extra.json').write_text('{}',encoding='utf-8');change(folder,'result.json',lambda _p:None)
    elif kind=='native_date':change(folder,'plan.json',lambda p:p['comparison'].update(native_event_date='2024-02-28'))
    elif kind=='early_time':change(folder,'result.json',lambda p:p.update(completed_at='2024-01-01T00:00:00+03:30'))
    elif kind=='source_count':change(folder,'result.json',lambda p:p['cases'][0]['assessment']['events'][0].update(source_new_shares=1400))
    else:change(folder,'plan.json',lambda p:p.update(holder_entitlement_note='verified'))
    with pytest.raises(ValueError):archive.review_share_changes(folder)


def test_date_guard_precedes_database_open_or_request(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DB_PATH',tmp_path/'absent.db')
    monkeypatch.setattr('database.db.DatabaseManager.__init__',lambda *_a,**_k:pytest.fail('database opened'))
    with pytest.raises(ValueError):archive.main(['capture','--symbols','آزمون','--start','2025-03-20','--end-exclusive','2025-03-22'])
    assert not settings.DB_PATH.exists()


def test_future_capital_availability_precedes_financial_interpretation(capital,monkeypatch):
    path=Path(capital)/'result.json';payload=json.loads(path.read_bytes());payload['available_at']='2999-01-01T00:00:00+03:30'
    path.write_text(json.dumps(payload),encoding='utf-8')
    monkeypatch.setattr(archive,'review_capital_study',lambda *_a:pytest.fail('future financial source read'))
    provider=Provider()
    with pytest.raises(ValueError,match='دسترس'):capture(capital,provider)
    assert not provider.calls


def test_capture_without_capital_source_is_observational_only(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path)
    result=archive.capture_share_changes(Provider(),INSTRUMENTS,start=START,end_exclusive=END)
    assert result['comparison'] is None and archive.review_share_changes(result['folder'])['comparison'] is None


def test_raw_hash_disagreement_cannot_pass_after_only_outer_manifest_is_rehashed(capital):
    result=capture(capital);folder=Path(result['folder']);raw=folder/'raw/observation-000.json'
    raw.write_bytes(raw.read_bytes()+b' ')
    change(folder,'result.json',lambda _p:None)
    with pytest.raises(ValueError):archive.review_share_changes(folder)
    assert hashlib.sha256(raw.read_bytes()).hexdigest()!=json.loads((folder/'observation-000.json').read_bytes())['observation']['body_sha256']


def test_oversized_injected_provider_cannot_store_unbounded_body(capital):
    class TooLarge(Provider):
        def get_share_change_observation(self,_code):
            meta,body=observation(body=b' '*(archive.MAX_BYTES+1));return dict(meta,content=body)
    result=capture(capital,TooLarge())
    assert result['cases'][0]['assessment']['status']=='unavailable'
    assert not (Path(result['folder'])/'raw').exists()
    assert archive.review_share_changes(result['folder'])['status_counts']=={'unavailable':1}


def test_future_receipt_cannot_be_interpreted_before_it_fails_closed(capital,monkeypatch):
    class Future(Provider):
        def get_share_change_observation(self,_code):
            value=super().get_share_change_observation(_code)
            value.update(requested_at='2999-01-01T10:00:00+03:30',received_at='2999-01-01T10:00:01+03:30')
            return value
    monkeypatch.setattr(archive,'_assess',lambda *_a:pytest.fail('future receipt financial interpretation'))
    with pytest.raises(ValueError,match='ساعت'):capture(capital,Future())


@pytest.mark.parametrize('failure',[TimeoutError,FileNotFoundError,PermissionError])
def test_oserror_subclasses_keep_a_supported_failure_category_and_can_be_reviewed(capital,failure):
    class Failed(Provider):
        def get_share_change_observation(self,_code):raise failure('test only')
    result=capture(capital,Failed());folder=Path(result['folder'])
    assert json.loads((folder/'observation-000.json').read_bytes())['error_type']=='OSError'
    assert archive.review_share_changes(folder)['status_counts']=={'unavailable':1}


def test_capture_only_implementation_drift_does_not_change_interpretation_or_execute_archived_code(capital,monkeypatch):
    result=capture(capital);original=archive._current_hash
    monkeypatch.setattr(archive,'_current_hash',lambda n:'0'*64 if n in ('ops/share_changes.py','data_collector/tsetmc.py') else original(n))
    review=archive.review_share_changes(result['folder'])
    assert review['semantic_code_matches_current'] and not review['capture_code_matches_current']
    assert review['comparison']['status']=='source_ratios_agree'


@pytest.mark.parametrize('name',['core/share_changes.py','core/timeutils.py','ops/report_files.py','ops/capital_actions.py'])
def test_semantic_interpreter_drift_requires_explicit_reassessment_or_original_runtime(capital,monkeypatch,name):
    result=capture(capital);original=archive._current_hash
    monkeypatch.setattr(archive,'_current_hash',lambda n:'0'*64 if n==name else original(n))
    with pytest.raises(ValueError,match='تفسیر'):archive.review_share_changes(result['folder'])


def test_self_consistent_capture_code_is_data_and_is_never_executed_or_authenticated(capital):
    result=capture(capital);folder=Path(result['folder']);name='data_collector/tsetmc.py'
    (folder/'code'/name).write_text("raise RuntimeError('archived code must never execute')\n",encoding='utf-8')
    change(folder,'plan.json',lambda p:p['code_sha256'].update({name:hashlib.sha256((folder/'code'/name).read_bytes()).hexdigest()}))
    review=archive.review_share_changes(folder)
    assert review['assessments_recomputed'] and not review['capture_code_matches_current']
    assert not review['external_authenticity_verified'] and not review['accepted_for_signals']
