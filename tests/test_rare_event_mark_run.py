"""Temporary registered studies with synthetic prices/publication bytes only."""
import base64
import hashlib
import io
import json
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from database.db import DatabaseManager
from research.rare_event_mark_run import fetch_study, finish_study, plan_study, review_study, select_gap_keys
from research.rare_event_run import run_census
from research.run import _sha256
from tests.test_no_trade_valuation import CODE, NOW, publication, receipt


@pytest.fixture
def setup(db,tmp_path,monkeypatch):
    monkeypatch.setitem(settings.RESEARCH_CONFIG,'dev_start','2024-01-01')
    monkeypatch.setattr('research.rare_event_run.now_tehran_iso',lambda:NOW)
    monkeypatch.setattr('research.rare_event_mark_run.now_tehran_iso',lambda:NOW)
    monkeypatch.setattr('data_collector.daily_publications.now_tehran_iso',lambda:NOW)
    days=pd.date_range('2024-01-01',periods=65).strftime('%Y-%m-%d')
    raw=pd.DataFrame([dict(symbol=name,date=day,close_final_raw=100.,ref_price_raw=100.,volume=10)
        for name in ('آزمون','کنترل') for day in days if not(name=='آزمون' and day=='2024-01-02')])
    source=tmp_path/'prices.parquet';raw.to_parquet(source,index=False)
    parent=run_census(db,prices_path=source,prices_sha256=_sha256(source),root=tmp_path/'parents',min_symbols=1)
    registry=tmp_path/'registry.json'
    registry.write_text(json.dumps(dict(symbols=[dict(symbol_id=1,symbol='آزمون',ins_code=CODE,instrument_type_code=300)])),encoding='utf-8')
    with DatabaseManager(tmp_path/'study.db') as child:
        params=dict(parent_folder=tmp_path/'parents'/parent['experiment_id'],parent_db=db,
            registry_path=registry,registry_sha256=_sha256(registry),root=tmp_path/'studies',max_requests=2)
        yield child,params


def planned(setup,tmp_path):
    db,params=setup
    plan=plan_study(db,**params)
    assert len(plan['tasks'])==1
    r=receipt(plan['tasks'][0]['date']);r['task']=plan['tasks'][0]
    path=tmp_path/'receipts.json';path.write_text(json.dumps([r]),encoding='utf-8')
    return db,Path(plan['folder']),path


def test_full_study_registers_and_recomputes_both_contracts(setup,tmp_path):
    db,folder,source=planned(setup,tmp_path)
    assert not (folder/'result.json').exists()
    result=finish_study(db,folder,receipts_path=source,receipts_sha256=_sha256(source))
    review=review_study(db,folder)
    assert review['marked_outcomes_recomputed']==24 and review['parent_outcomes_recomputed']==24
    assert review['current_code_matches_capture'] and not review['accepted_for_signals']
    assert result['metrics']['ledger_statuses']=={'observed_flat_mark':1}
    assert [r['resolved_outcomes'] for r in result['metrics']['comparison']]==[1,1,1]
    assert all(r['established_outcomes_changed']==0 for r in result['metrics']['comparison'])
    assert result['protocol']['strategy_trials_added']==0 and result['protocol']['predictions_fitted']==0


def test_plan_never_computes_marked_outcomes(setup,monkeypatch):
    import research.rare_event_mark_run as module
    monkeypatch.setattr(module,'compute_marked_rare_events',lambda *a,**k:pytest.fail('outcomes before receipt registration'))
    db,params=setup
    plan=plan_study(db,**params)
    assert plan['tasks'][0]['date']=='2024-01-02'
    row=db.conn.execute('SELECT parameters_json FROM experiments WHERE experiment_id=?',(plan['experiment_id']+'-PROTOCOL',)).fetchone()
    assert json.loads(row[0])['tasks']==plan['tasks']


def test_gap_priority_does_not_depend_on_price_returns():
    days=['2024-01-01','2024-01-02',*pd.date_range('2024-01-03',periods=61).strftime('%Y-%m-%d')]
    prices=pd.DataFrame([dict(symbol='آزمون',date=day,close_final_raw=100.) for day in days if day!='2024-01-02'])
    cohort=prices.iloc[[0]][['symbol','date']]
    before=select_gap_keys(prices,cohort,days,max_requests=1)
    prices.loc[prices.date.gt('2024-01-02'),'close_final_raw']=10000
    assert before==select_gap_keys(prices,cohort,days,max_requests=1)
    assert before['potential_windows_selected']==3


@pytest.mark.parametrize('change',['missing','extra','task','pre-registration','future-completion'])
def test_receipts_cannot_leave_the_registered_plan(setup,tmp_path,change):
    db,folder,path=planned(setup,tmp_path)
    rows=json.loads(path.read_text())
    if change=='missing': rows=[]
    elif change=='extra': rows+=rows
    elif change=='task': rows[0]['task']['date']='2024-01-03'
    elif change=='pre-registration': rows[0]['attempted_at']='2024-04-01T13:59:59+03:30'
    else: rows[0]['completed_at']='2024-04-02T14:00:00+03:30'
    path.write_text(json.dumps(rows),encoding='utf-8')
    with pytest.raises(ValueError):
        finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    assert not (folder/'result.json').exists()


def test_failed_publication_stays_in_the_cohort(setup,tmp_path):
    db,folder,path=planned(setup,tmp_path)
    rows=json.loads(path.read_text());rows[0]['source']=None
    path.write_text(json.dumps(rows),encoding='utf-8')
    result=finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    assert result['metrics']['ledger_statuses']=={'source_unavailable':1}
    assert all(row['resolved_outcomes']==0 for row in result['metrics']['comparison'])
    assert all(summary['opportunities']==8 for summary in result['metrics']['summary'])
    review_study(db,folder)


@pytest.mark.parametrize('field',['accepted_for_signals','profitability_proven'])
def test_forged_result_acceptance_is_rejected(setup,tmp_path,field):
    db,folder,path=planned(setup,tmp_path)
    result=finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    result[field]=True
    (folder/'result.json').write_text(json.dumps(result),encoding='utf-8')
    with pytest.raises(ValueError): review_study(db,folder)


def test_outcome_change_and_rehash_cannot_avoid_reconstruction(setup,tmp_path):
    db,folder,path=planned(setup,tmp_path)
    result=finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    data=pd.read_parquet(folder/'outcomes.parquet');data.loc[0,'endpoint_multiple']=9.
    data.to_parquet(folder/'outcomes.parquet',index=False)
    result['artifacts_sha256']['outcomes.parquet']=_sha256(folder/'outcomes.parquet')
    (folder/'result.json').write_text(json.dumps(result),encoding='utf-8')
    with pytest.raises(AssertionError): review_study(db,folder)


def test_finished_study_is_not_overwritten(setup,tmp_path):
    db,folder,path=planned(setup,tmp_path)
    finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    with pytest.raises(ValueError): finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))


def test_reviewer_checks_result_dates_before_reading_sealed_values(setup,tmp_path,monkeypatch):
    db,folder,path=planned(setup,tmp_path)
    result=finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    target=folder/'outcomes.parquet'
    frame=pd.read_parquet(target)
    frame.loc[0,'date']=settings.RESEARCH_CONFIG['holdout_start']
    frame.to_parquet(target,index=False)
    digest=_sha256(target)
    result['artifacts_sha256']['outcomes.parquet']=digest
    (folder/'result.json').write_text(json.dumps(result),encoding='utf-8')
    real=pd.read_parquet
    def guarded(source,*args,**kwargs):
        if hasattr(source,'getvalue') and hashlib.sha256(source.getvalue()).hexdigest()==digest:
            assert kwargs.get('columns')==['date'],'sealed numeric columns were read'
        return real(source,*args,**kwargs)
    monkeypatch.setattr(pd,'read_parquet',guarded)
    with pytest.raises(ValueError,match='مهروموم'):
        review_study(db,folder)


def test_registration_failure_does_not_create_result(setup,monkeypatch):
    db,params=setup
    monkeypatch.setattr(db,'log_experiment',lambda **kwargs:None)
    with pytest.raises(RuntimeError):plan_study(db,**params)
    assert not list(params['root'].rglob('result.json'))


def test_fetch_uses_exact_registered_tasks_and_preserves_source_failure(setup,tmp_path):
    db,params=setup
    plan=plan_study(db,**params)
    calls=[]
    class Provider:
        def get_daily_publication_observation(self,code,day,kind):
            calls.append((code,day,kind))
            return publication(receipt(day))
    fetched=fetch_study(db,plan['folder'],provider=Provider())
    assert calls==[(CODE,'2024-01-02','price')]
    assert fetched['publication_review']['byte_replay_verified']
    result=finish_study(db,plan['folder'],receipts_path=fetched['receipts_path'],receipts_sha256=fetched['receipts_sha256'])
    assert all(row['resolved_outcomes']==1 for row in result['metrics']['comparison'])
    review_study(db,plan['folder'])
    with pytest.raises((ValueError,FileExistsError)):
        fetch_study(db,plan['folder'],provider=Provider())


def test_json_hash_is_checked_on_the_bytes_actually_parsed(tmp_path,monkeypatch):
    from research.rare_event_mark_run import _load
    path=tmp_path/'document.json';path.write_bytes(b'{"correct":1}')
    digest=_sha256(path)
    real_open=Path.open
    def changed_open(candidate,*args,**kwargs):
        return io.BytesIO(b'{"forged":9}') if candidate==path else real_open(candidate,*args,**kwargs)
    monkeypatch.setattr(Path,'open',changed_open)
    with pytest.raises(ValueError,match='بایتی'):
        _load(path,digest)


def test_replacing_input_provenance_with_equal_numeric_results_is_rejected(setup,tmp_path):
    db,folder,path=planned(setup,tmp_path)
    result=finish_study(db,folder,receipts_path=path,receipts_sha256=_sha256(path))
    receipts_path=folder/'receipts.json'
    receipts=json.loads(receipts_path.read_text())
    source=receipts[0]['source']
    native=json.loads(base64.b64decode(source['body_base64']))
    native['closingPriceDaily']['unused']='different evidence with identical numeric answers'
    body=json.dumps(native).encode()
    source.update(body_base64=base64.b64encode(body).decode(),body_sha256=hashlib.sha256(body).hexdigest(),response_bytes=len(body))
    receipts_path.write_text(json.dumps(receipts),encoding='utf-8')
    ledger_path=folder/'ledger.json'
    from core.no_trade_valuation import parse_no_trade_receipt
    ledger=[parse_no_trade_receipt(row) for row in receipts]
    assert ledger[0]['status']=='observed_flat_mark' and ledger[0]['factor']==1.
    ledger_path.write_text(json.dumps(ledger),encoding='utf-8')
    for name in ('receipts.json','ledger.json'):
        result['artifacts_sha256'][name]=_sha256(folder/name)
    result['metrics']['receipts_sha256']=_sha256(receipts_path)
    (folder/'result.json').write_text(json.dumps(result),encoding='utf-8')
    with pytest.raises(ValueError,match='ثبت نتیجه'):
        review_study(db,folder)
