"""ثبت سناریو و بازبینی فقط فایل؛ داده و اسناد مصنوعی صرفاً در مسیر موقت آزمون."""

import hashlib
import json
import shutil
import socket
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from ops.capital_actions import create_capital_study
from research import bonus_share_case as case
from tests.test_capital_actions import source as source
from tests.test_portfolio import history


@pytest.fixture
def bonus_case_inputs(db, source):
    capital=create_capital_study(**source)
    prices, _=history(30,('آزمون','تقویم'))
    for symbol in prices.symbol.unique():
        prices.loc[prices.symbol.eq(symbol),'date']=pd.date_range('2024-02-18',periods=30).strftime('%Y-%m-%d')
    after=prices.symbol.eq('آزمون') & prices.date.ge('2024-02-28')
    prices.loc[after,['open_raw','close_raw','close_final_raw','ref_price_raw']]=74.
    prices.loc[after,'low_raw'],prices.loc[after,'high_raw']=73.,75.
    prices.loc[after,'value']=74000.
    prices=prices.loc[~(prices.symbol.eq('آزمون') & prices.date.gt('2024-02-19') & prices.date.lt('2024-02-28'))]
    for index,symbol in enumerate(prices.symbol.unique()):
        sid=db.upsert_symbol(dict(symbol_fa=symbol,ins_code=str(123+index),instrument_type_code=300))
        db.upsert_daily_prices_df(sid,prices.loc[prices.symbol.eq(symbol)].drop(columns='symbol'))
    return dict(capital_study=capital['folder'],decision_date='2024-02-18',previous_price_session='2024-02-19',
        record_date='2024-02-21',ex_date='2024-02-28',end_exclusive='2024-03-19')


def rehash(folder):
    path=Path(folder)/'manifest.json'
    payload=json.loads(path.read_bytes());payload['files']=case._all_files(Path(folder))
    path.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8')


def change_json(folder,name,transform):
    path=Path(folder)/name;payload=json.loads(path.read_bytes());transform(payload)
    path.write_text(json.dumps(payload,ensure_ascii=False,allow_nan=False),encoding='utf-8');rehash(folder)


def test_protocol_precedes_all_four_financial_replays_and_review_reads_no_database_or_network(db,bonus_case_inputs,monkeypatch):
    logs,log,compare=[],db.log_experiment,case._compare
    def record(**kwargs):
        logs.append(kwargs['strategy_name']);return log(**kwargs)
    def watched(*args):
        assert logs==['bonus-share-accounting-protocol'];return compare(*args)
    monkeypatch.setattr(db,'log_experiment',record);monkeypatch.setattr(case,'_compare',watched)
    result=case.run_bonus_share_case(db,**bonus_case_inputs)
    assert logs==['bonus-share-accounting-protocol','bonus-share-accounting-case']
    assert list(result['modes'])==list(case.MODES)
    for mode in ('uncredited_bonus_normal','uncredited_bonus_stress'):
        model=result['modes'][mode]
        assert model['sold_shares']==model['bought_shares'] and model['metrics']['total_return'] is None
    assert result['modes']['legacy_normal']['sold_shares']>result['modes']['uncredited_bonus_normal']['sold_shares']
    protocol=json.loads((Path(result['folder'])/'protocol.json').read_bytes())
    assert all(protocol[k] is False for k in case.FALSE_FLAGS) and protocol['releases']==[]
    monkeypatch.setattr(case,'_compare',compare)
    monkeypatch.setattr('database.db.DatabaseManager.__init__',lambda *_a,**_k:pytest.fail('database opened'))
    monkeypatch.setattr(socket,'create_connection',lambda *_a,**_k:pytest.fail('network used'))
    report=case.review_bonus_share_case(result['folder'])
    assert report['replay_verified'] and not report['independent_registration_verified']
    assert report['modes']==result['modes'] and not report['accepted_for_signals']


def test_failed_formal_registration_cannot_evaluate_or_publish_a_result(db,bonus_case_inputs,monkeypatch):
    monkeypatch.setattr(db,'log_experiment',lambda **_k:None)
    monkeypatch.setattr(case,'_compare',lambda *_a:pytest.fail('unregistered calculation'))
    with pytest.raises(RuntimeError,match='ثبت رسمی'):
        case.run_bonus_share_case(db,**bonus_case_inputs)
    folders=list((settings.DATA_DIR/'private'/'bonus-share-cases').iterdir())
    assert len(folders)==1 and (folders[0]/'protocol.json').is_file() and not (folders[0]/'result.json').exists()


def test_complete_source_copy_survives_removal_of_original_capital_study(db,bonus_case_inputs,tmp_path):
    result=case.run_bonus_share_case(db,**bonus_case_inputs)
    original=Path(bonus_case_inputs['capital_study']).resolve()
    assert original.is_relative_to(tmp_path.resolve())
    shutil.rmtree(original)
    assert case.review_bonus_share_case(result['folder'])['source_study_verified']


@pytest.mark.parametrize('flag',['actual_share_ratio_verified','holder_credit_verified','accepted_for_signals'])
def test_rehashed_scope_promotion_never_turns_capital_amounts_into_holder_entitlements(db,bonus_case_inputs,flag):
    result=case.run_bonus_share_case(db,**bonus_case_inputs)
    change_json(result['folder'],'protocol.json',lambda p:p.update({flag:True}))
    with pytest.raises(ValueError):case.review_bonus_share_case(result['folder'])


@pytest.mark.parametrize('kind',['result','ratio','phantom_credit','extra_file','early_completion','backdated_source','config',
    'extra_protocol','narrative','bool_config'])
def test_rehashed_forgery_cannot_pass_source_binding_or_replay(db,bonus_case_inputs,kind):
    result=case.run_bonus_share_case(db,**bonus_case_inputs);folder=Path(result['folder'])
    if kind=='result':
        change_json(folder,'result.json',lambda p:p['modes']['uncredited_bonus_normal']['metrics'].update(total_return=.9))
    elif kind=='ratio':
        change_json(folder,'protocol.json',lambda p:p['event'].update(bonus_numerator=100))
    elif kind=='phantom_credit':
        change_json(folder,'protocol.json',lambda p:p['releases'].append(dict(date='2024-03-16',source='issuer deposit is not holder credit')))
    elif kind=='extra_file':
        (folder/'not-an-input.json').write_text('{}',encoding='utf-8');rehash(folder)
    elif kind=='early_completion':
        change_json(folder,'result.json',lambda p:p.update(completed_at='2024-01-01T00:00:00+03:30'))
    elif kind=='backdated_source':
        change_json(folder,'protocol.json',lambda p:p.update(registered_at='2024-03-14T12:00:00+03:30'))
    elif kind=='config':
        change_json(folder,'protocol.json',lambda p:p['config'].update(max_loss_pct=.9))
    elif kind=='extra_protocol':
        change_json(folder,'protocol.json',lambda p:p.update(unchecked_metadata='not a registered input'))
    elif kind=='narrative':
        change_json(folder,'protocol.json',lambda p:p.update(share_ratio_source='actual shareholder credit verified'))
    else:
        change_json(folder,'protocol.json',lambda p:p['config'].update(max_positions=True))
    with pytest.raises(ValueError):case.review_bonus_share_case(folder)


def test_reader_rejects_sealed_price_date_before_requesting_numeric_columns(db,bonus_case_inputs,monkeypatch):
    result=case.run_bonus_share_case(db,**bonus_case_inputs);folder=Path(result['folder'])
    path=folder/'prices.parquet';frame=pd.read_parquet(path);frame.loc[0,'date']='2025-03-21';frame.to_parquet(path,index=False)
    change_json(folder,'protocol.json',lambda p:p['input_sha256'].update({'prices.parquet':hashlib.sha256(path.read_bytes()).hexdigest()}))
    calls,read=[],pd.read_parquet
    def watched(*args,**kwargs):
        calls.append(kwargs.get('columns'));return read(*args,**kwargs)
    monkeypatch.setattr(pd,'read_parquet',watched)
    with pytest.raises(ValueError,match='مرز'):case.review_bonus_share_case(folder)
    assert calls==[['date']]


def test_invalid_sealed_cli_dates_do_not_open_or_create_a_database(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DB_PATH',tmp_path/'absent.db')
    monkeypatch.setattr('database.db.DatabaseManager.__init__',lambda *_a,**_k:pytest.fail('database opened'))
    with pytest.raises(ValueError,match='مرز'):
        case.main(['run','--capital-study','unused','--decision-date','2024-02-18','--previous-price-session','2024-02-19',
                   '--record-date','2024-02-21','--ex-date','2025-03-21','--end-exclusive','2025-03-22'])
    assert not settings.DB_PATH.exists()


def test_future_source_availability_rejects_before_source_financial_interpretation_or_database_read(db,bonus_case_inputs,monkeypatch):
    capital=Path(bonus_case_inputs['capital_study'])
    path=capital/'result.json';payload=json.loads(path.read_bytes());payload['available_at']='2999-01-01T00:00:00+03:30'
    path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    monkeypatch.setattr(case,'review_capital_study',lambda *_a:pytest.fail('future source interpreted'))
    monkeypatch.setattr(db,'get_symbol',lambda *_a:pytest.fail('database read'))
    with pytest.raises(ValueError,match='دسترس'):
        case.run_bonus_share_case(db,**bonus_case_inputs)
