"""بازتولید مستقل گزارش‌های بیرونی؛ تمام داده‌ها در fixture و DB موقت‌اند."""

import ast
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from config import settings
from core.client_quality import snapshot_buyer_power, validated_client_data
from core.text import to_fa_digits
from core.timeutils import now_tehran_iso, to_jalali
from data_collector.monitor import DataFreshnessMonitor
from features.store import ALL_FEATURES, build_feature_panel
from tests.test_client_quality import coherent_history
from tests.test_paper import idea, portfolio, snapshot, trade
from tests.test_paper import paper as paper


@pytest.mark.parametrize('missing', [None, np.nan, pd.NA])
def test_same_missing_optional_value_has_same_buyer_power(missing):
    row=dict(real_buy_volume=600., real_buy_count=10., real_sell_volume=400.,
             real_sell_count=10., volume=1000., legal_buy_volume=missing)
    assert snapshot_buyer_power(row)==1.5


def test_unknown_symbol_does_not_destroy_valid_feature_work(db):
    result=build_feature_panel(db,symbols=['ناشناخته'])
    assert result.empty and list(result)==['date','symbol']+ALL_FEATURES


def test_explicit_feature_scope_cannot_include_a_fund(db):
    prices,_=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='صندوقآزمون',ins_code='123',instrument_type_code=305))
    db.upsert_daily_prices_df(sid,prices)
    assert build_feature_panel(db,symbols=['صندوقآزمون']).empty


def test_invalid_legal_value_is_distinguishable_from_absence():
    prices,clients=coherent_history()
    clients['buy_n_value']=clients['buy_n_value'].astype(object)
    clients.loc[23,'buy_n_value']=None
    clients.loc[24,'buy_n_value']='corrupt'
    result=validated_client_data(prices,clients)
    assert result.loc[23,'buy_legal_value_quality']=='missing'
    assert result.loc[24,'buy_legal_value_quality']=='invalid'


def test_all_unknown_live_metrics_do_not_crash_or_display_invalid_flow(monkeypatch, isolated_board_monitor):
    bad=dict(symbol_fa='آزمون',real_buy_volume=3000.,real_sell_volume=400.,real_buy_count=10.,
             real_sell_count=10.,legal_buy_volume=400.,legal_sell_volume=600.,volume=1000.,value=100_000.,
             last_price=100.,ref_price=100.,close_price=100.,buy_queue_volume=0.,sell_queue_volume=0.,
             captured_at=now_tehran_iso(),provider_timestamp=now_tehran_iso())
    module=ast.parse((settings.BASE_DIR/'dashboard/app.py').read_text(encoding='utf-8'))
    node=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='page_live')
    namespace=dict(st=st,pd=pd,jobs=SimpleNamespace(job_status=lambda _:dict(running=False)),
                   latest_snapshots=lambda:pd.DataFrame([bad]),DataFreshnessMonitor=DataFreshnessMonitor,
                   to_jalali=to_jalali,to_fa_digits=to_fa_digits,interval_panel=lambda _:None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),'local_live_regression','exec'),namespace)
    monkeypatch.setattr(st,'_brs_independent_live_regression',namespace['page_live'],raising=False)
    app=AppTest.from_string('import streamlit as st\nst._brs_independent_live_regression()').run()
    assert not app.exception
    assert pd.isna(app.dataframe[0].value['برآورد پول حقیقی (میلیارد تومان)']).all()


def test_runtime_change_cannot_leak_nested_portfolio_profit(paper,db,monkeypatch):
    repo,clock=paper
    key=portfolio(paper)
    iid=idea(repo,key)
    clock[0]+=timedelta(seconds=10)
    snapshot(db,clock[0]);trade(repo,key,iid)
    original=repo.report(key)
    monkeypatch.setattr('database.paper.code_identity',lambda:dict(original['protocol']['code'],sha256='changed'))
    report=repo.export(key)['report']
    assert report['runtime_matches'] is False and report['equity_rial'] is None
    assert report['ledger']['equity_rial'] is None
    assert report['ledger']['total_net_rial'] is None
    assert report['ledger']['cash_rial']==original['ledger']['cash_rial']


def test_fractional_daily_volume_rejects_the_whole_batch(db):
    prices,_=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    prices=prices.head(2).copy();prices['volume']=prices['volume'].astype(float)
    prices.loc[1,'volume']=1.5
    with pytest.raises(ValueError):db.upsert_daily_prices_df(sid,prices)
    assert not db.conn.execute('SELECT 1 FROM daily_prices').fetchone()


def test_foreign_client_source_cannot_enter_default_features(db):
    _,clients=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    db.upsert_client_types_df(sid,clients.tail(1))
    with db.conn:db.conn.execute("UPDATE client_types_daily SET source='other-source'")
    assert db.get_client_types_df('آزمون').empty


def test_normalized_aliases_do_not_duplicate_features_or_lose_raw_identity(db,tmp_path):
    from features.provenance import assert_compatible_inputs, raw_input_snapshot
    from features.store import build_feature_store
    prices,clients=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='کیان',ins_code='123',instrument_type_code=300))
    db.upsert_daily_prices_df(sid,prices);db.upsert_client_types_df(sid,clients)
    result=build_feature_panel(db,symbols=['كيان','کیان'])
    assert len(result)==len(prices) and set(result.symbol)=={'کیان'}
    assert result.attrs['selection']['unavailable_symbols']==[
        dict(symbol='کیان',reason='duplicate_normalized_request')]
    manifest=build_feature_store(db,symbols=['كيان'],root=tmp_path/'features')
    assert manifest['provenance']['raw_inputs']==raw_input_snapshot(db,symbols=['کیان'])
    assert_compatible_inputs(db, manifest)


@pytest.mark.parametrize('change', ['new_empty_member', 'instrument_identity', 'market_identity', 'type_identity'])
def test_feature_research_rejects_registry_changes_even_without_raw_price_changes(db, tmp_path, change):
    from features.provenance import assert_compatible_inputs, raw_input_snapshot
    from features.store import build_feature_store

    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    manifest = build_feature_store(db, root=tmp_path / 'features')
    before = raw_input_snapshot(db)
    if change == 'new_empty_member':
        db.upsert_symbol(dict(symbol_fa='نمادجدید', ins_code='456', instrument_type_code=300))
    elif change == 'instrument_identity':
        with db.conn:
            db.conn.execute('UPDATE symbols SET ins_code=? WHERE id=?', ('456', sid))
    elif change == 'market_identity':
        with db.conn:
            db.conn.execute('UPDATE symbols SET flow=? WHERE id=?', (2, sid))
    else:
        with db.conn:
            db.conn.execute('UPDATE symbols SET instrument_type_code=? WHERE id=?', (303, sid))
    assert raw_input_snapshot(db) == before
    with pytest.raises(ValueError, match='جامعه|هویت'):
        assert_compatible_inputs(db, manifest)


def test_registry_snapshot_preserves_known_empty_members_and_ignores_unrelated_metadata(db, tmp_path):
    from features.provenance import assert_compatible_inputs, registry_snapshot
    from features.store import build_feature_store

    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    db.upsert_symbol(dict(symbol_fa='بدونسابقه', ins_code='456', instrument_type_code=300))
    manifest = build_feature_store(db, root=tmp_path / 'features')
    stored = manifest['provenance']['registry_snapshot']
    assert {row['symbol_fa'] for row in stored['members']} == {'آزمون', 'بدونسابقه'}
    assert not stored['historical_membership_verified']
    with db.conn:
        db.conn.execute('UPDATE symbols SET is_active=0,name_fa=? WHERE id=?', ('نام تازه', sid))
    db.upsert_symbol(dict(symbol_fa='صندوقآزمون', ins_code='789', instrument_type_code=305))
    assert registry_snapshot(db) == stored
    assert not db.conn.in_transaction
    assert_compatible_inputs(db, manifest)
    assert not db.conn.in_transaction
    db.conn.execute('BEGIN')
    assert_compatible_inputs(db, manifest)
    assert db.conn.in_transaction
    db.conn.rollback()


def test_missing_registry_snapshot_cannot_authorize_new_research(db, tmp_path):
    from features.provenance import assert_compatible_inputs
    from features.store import build_feature_store

    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    manifest = build_feature_store(db, root=tmp_path / 'features')
    manifest['provenance'].pop('registry_snapshot')
    with pytest.raises(ValueError, match='جامعه|هویت'):
        assert_compatible_inputs(db, manifest)


@pytest.mark.parametrize('damage', ['numeric_false', 'boolean_flow'])
def test_registry_snapshot_rejects_type_confusion_even_if_python_values_compare_equal(db, tmp_path, damage):
    from features.provenance import assert_compatible_inputs
    from features.store import build_feature_store

    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300, flow=1))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    manifest = build_feature_store(db, root=tmp_path / 'features')
    registry = manifest['provenance']['registry_snapshot']
    if damage == 'numeric_false':
        registry['historical_membership_verified'] = 0
    else:
        registry['members'][0]['flow'] = True
    with pytest.raises(ValueError, match='جامعه|هویت'):
        assert_compatible_inputs(db, manifest)


def test_registry_snapshot_rejects_non_normalized_stored_identity(db):
    from features.provenance import registry_snapshot

    sid = db.upsert_symbol(dict(symbol_fa='کیان', ins_code='123', instrument_type_code=300))
    with db.conn:
        db.conn.execute('UPDATE symbols SET symbol_fa=? WHERE id=?', ('كيان', sid))
    with pytest.raises(ValueError, match='نام|هویت'):
        registry_snapshot(db)


def test_feature_compatibility_fails_if_code_changes_during_verification(db, tmp_path, monkeypatch):
    from features import provenance
    from features.store import build_feature_store

    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    manifest = build_feature_store(db, root=tmp_path / 'features')
    hashes = iter([manifest['provenance']['feature_logic_sha256'], 'changed'])
    monkeypatch.setattr(provenance, 'feature_logic_fingerprint', lambda: next(hashes))
    with pytest.raises(ValueError, match='هنگام بررسی'):
        provenance.assert_compatible_inputs(db, manifest)
    assert not db.conn.in_transaction


def test_explicit_block_board_is_not_an_ordinary_feature_target(db):
    prices,_=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='آزمون2',ins_code='123',instrument_type_code=300))
    db.upsert_daily_prices_df(sid,prices)
    result=build_feature_panel(db,symbols=['آزمون2'])
    assert result.empty and result.attrs['selection']['unavailable_symbols'][0]['reason']=='outside_ordinary_equity_scope'


@pytest.mark.parametrize('field',['buy_i_volume','buy_n_volume','buy_i_count','sell_n_count'])
@pytest.mark.parametrize('bad',[1.5,True,-1,float('inf')])
def test_invalid_participant_quantity_cannot_partially_commit(db,field,bad):
    _,clients=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    clients=clients.head(2).copy();clients[field]=clients[field].astype(object)
    clients.loc[1,field]=bad
    with pytest.raises(ValueError):db.upsert_client_types_df(sid,clients)
    assert not db.conn.execute('SELECT 1 FROM client_types_daily').fetchone()


@pytest.mark.parametrize('change',[dict(real_buy_volume=600.5,legal_buy_volume=399.5),
                                 dict(volume=1000.5),dict(legal_sell_volume=600.5)])
def test_fractional_live_share_volume_cannot_create_valid_flow(change):
    from core.client_quality import snapshot_client_metrics
    row=dict(real_buy_volume=600.,real_sell_volume=400.,real_buy_count=10.,real_sell_count=10.,
             legal_buy_volume=400.,legal_sell_volume=600.,volume=1000.,last_price=100.)
    result=snapshot_client_metrics(row|change)
    assert result['buyer_power'] is None and result['real_flow_estimate_rial'] is None
    assert result['clock_coupling_verified'] is False


def test_missing_calendar_is_unknown_instead_of_zero_missing_sessions():
    from research.uncertainty import compare_equity_curves
    from tests.test_uncertainty import curves
    result=compare_equity_curves(*curves(),initial_capital_rial=100000.,replications=1000)
    assert result['missing_expected_sessions'] is None and result['unexpected_sessions'] is None


def test_paper_protocol_captures_participant_quality_dependency():
    from database.paper import code_identity
    assert 'core/client_quality.py' in code_identity()['sources']


def test_global_feature_identity_retains_clients_without_price_history(db,tmp_path):
    from features.provenance import assert_compatible_inputs, raw_input_snapshot
    from features.store import build_feature_store
    prices,clients=coherent_history()
    sid=db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    db.upsert_daily_prices_df(sid,prices);db.upsert_client_types_df(sid,clients)
    empty_sid=db.upsert_symbol(dict(symbol_fa='بدونقیمت',ins_code='456',instrument_type_code=303))
    db.upsert_client_types_df(empty_sid,clients.head(1))
    manifest=build_feature_store(db,root=tmp_path/'features')
    assert 'including inactive symbols' in manifest['provenance']['known_limitations'][0]
    assert manifest['provenance']['raw_inputs']==raw_input_snapshot(db)
    assert_compatible_inputs(db,manifest)
