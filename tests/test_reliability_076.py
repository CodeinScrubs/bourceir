"""Acceptance tests for audited failures; all data are temporary synthetic fixtures."""

import ast
import hashlib
from datetime import timedelta
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from config import settings
from core.client_quality import snapshot_client_metrics, validated_client_data
from core.text import to_fa_digits
from core.timeutils import now_tehran_iso, to_jalali
from data_collector.monitor import DataFreshnessMonitor
from features.store import ALL_FEATURES, build_feature_panel, build_feature_store
from ops.environment import inspect_environment, record_environment
from tests.test_client_quality import coherent_history
from tests.test_paper import idea, portfolio, snapshot, trade
from tests.test_paper import paper as paper


def live_snapshot(**changes):
    values = dict(real_buy_volume=600., real_buy_count=10., real_sell_volume=400., real_sell_count=10.,
                  volume=1000., last_price=100., legal_buy_volume=400., legal_sell_volume=600.)
    values.update(changes)
    return values


@pytest.mark.parametrize('missing', [None, np.nan, pd.NA])
def test_missing_optional_legal_and_total_have_consistent_scalar_semantics(missing):
    result = snapshot_client_metrics(live_snapshot(legal_buy_volume=missing, legal_sell_volume=missing))
    assert result['buyer_power'] == 1.5
    assert result['real_flow_estimate_rial'] == 20_000.
    assert result['buy_volume_quality'] == 'verified_component'
    result = snapshot_client_metrics(live_snapshot(volume=missing))
    assert result['buyer_power'] == 1.5
    assert result['real_flow_estimate_rial'] is None


@pytest.mark.parametrize('changes', [dict(real_buy_volume=3000.), dict(legal_buy_volume=1.),
    dict(legal_buy_volume=float('inf')), dict(legal_sell_volume=True), dict(volume=False),
    dict(real_sell_count=0.), dict(real_buy_count=.5), dict(real_buy_volume=-1.)])
def test_incoherent_snapshot_cannot_create_buyer_power_or_flow(changes):
    result = snapshot_client_metrics(live_snapshot(**changes))
    assert result['buyer_power'] is None and result['real_flow_estimate_rial'] is None


def test_unknown_counts_do_not_erase_coherent_volume_estimate_and_zero_is_observed():
    result = snapshot_client_metrics(live_snapshot(real_buy_count=None, real_sell_count=pd.NA))
    assert result['buyer_power'] is None
    assert result['real_flow_estimate_rial'] == 20_000.
    result = snapshot_client_metrics(live_snapshot(real_buy_volume=0., real_buy_count=0., legal_buy_volume=1000.))
    assert result['buyer_power'] is None and result['real_flow_estimate_rial'] == -40_000.


@pytest.mark.parametrize('bad', [float('inf'), True, 'broken', -1.])
def test_daily_quality_distinguishes_invalid_legal_from_absent_without_mutating_raw(bad):
    prices, clients = coherent_history()
    clients['buy_n_value'] = clients['buy_n_value'].astype(object)
    clients.loc[23, 'buy_n_value'] = None
    clients.loc[24, 'buy_n_value'] = bad
    original = clients.copy(deep=True)
    clean = validated_client_data(prices, clients)
    assert clean.loc[23, 'buy_legal_value_quality'] == 'missing'
    assert clean.loc[24, 'buy_legal_value_quality'] == 'invalid'
    assert clean.loc[22, 'buy_legal_value_quality'] == 'observed'
    if bad == -1.:
        assert clean.loc[24, 'buy_value_quality'] == 'inconsistent_totals_or_implied_price'
        assert pd.isna(clean.loc[24, 'buy_i_value'])
    else:
        assert clean.loc[24, 'buy_value_quality'] == 'verified_component'
        assert clean.loc[24, 'buy_i_value'] == 60_000_000.
    pd.testing.assert_frame_equal(clients, original)


def test_unknown_symbol_is_a_typed_empty_result_with_explicit_coverage(db, tmp_path):
    result = build_feature_panel(db, symbols=['ناشناخته'])
    assert result.empty and list(result) == ['date', 'symbol'] + ALL_FEATURES
    assert result.attrs['selection']['unavailable_symbols'] == [dict(symbol='ناشناخته', reason='unknown_or_no_daily_prices')]
    prices, clients = coherent_history()
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    result = build_feature_panel(db, symbols=['ناشناخته', 'آزمون'])
    assert set(result.symbol) == {'آزمون'} and len(result) == len(prices)
    manifest = build_feature_store(db, symbols=['ناشناخته', 'آزمون'], root=tmp_path/'features')
    assert manifest['symbol_selection']['included_symbols'] == ['آزمون']
    assert len(manifest['symbol_selection']['unavailable_symbols']) == 1


def test_explicit_feature_target_cannot_add_a_fund_to_ordinary_equities(db):
    prices, _ = coherent_history()
    for symbol, kind in [('سهمآزمون', 300), ('صندوقآزمون', 305)]:
        sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=symbol, instrument_type_code=kind))
        db.upsert_daily_prices_df(sid, prices)
    result = build_feature_panel(db, symbols=['سهمآزمون', 'صندوقآزمون'])
    assert set(result.symbol) == {'سهمآزمون'}
    assert result.attrs['selection']['unavailable_symbols'] == [
        dict(symbol='صندوقآزمون', reason='outside_ordinary_equity_scope')]


@pytest.mark.parametrize('all_unknown', [True, False])
@pytest.mark.parametrize('monitor_present', [True, False])
def test_live_page_all_null_and_mixed_rows_render_without_invalid_flow(monkeypatch, all_unknown, monitor_present,
                                                                     isolated_board_monitor):
    if monitor_present:
        isolated_board_monitor(dict(state='current', url='http://127.0.0.1:1', status=dict(
            boards=[dict(key='TEST00000001', symbol='تابلوآزمون', ok=True, last_price=100, volume=1000)], alerts=[])))
    bad = live_snapshot(symbol_fa='آزمون', real_buy_volume=3000., value=100_000., ref_price=100.,
                        close_price=100., captured_at=now_tehran_iso(), provider_timestamp=now_tehran_iso(),
                        buy_queue_volume=0., sell_queue_volume=0.)
    good = dict(bad, symbol_fa='سالم', real_buy_volume=600.)
    source = pd.DataFrame([bad] if all_unknown else [bad, good])
    tree = ast.parse((settings.BASE_DIR/'dashboard/app.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'page_live')
    namespace = dict(st=st, pd=pd, jobs=SimpleNamespace(job_status=lambda _: dict(running=False)),
                     latest_snapshots=lambda: source, DataFreshnessMonitor=DataFreshnessMonitor,
                     to_jalali=to_jalali, to_fa_digits=to_fa_digits, interval_panel=lambda _: None)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])), 'live_acceptance', 'exec'), namespace)
    monkeypatch.setattr(st, '_brs_live_acceptance', namespace['page_live'], raising=False)
    app = AppTest.from_string('import streamlit as st\nst._brs_live_acceptance()').run()
    assert not app.exception
    metric = 'برآورد پول حقیقی (میلیارد تومان)'
    tables = [frame.value for frame in app.dataframe if metric in frame.value.columns]
    assert len(tables) == 1
    table = tables[0]
    assert pd.isna(table.loc[table['نماد'].eq('آزمون'), 'برآورد پول حقیقی (میلیارد تومان)']).all()
    if all_unknown:
        app.radio[0].set_value('قدرت خریدار > ۲').run()
        assert not app.exception
        assert not any(metric in frame.value.columns for frame in app.dataframe)
        assert len(app.dataframe) == int(monitor_present)


def test_runtime_mismatch_masks_all_current_profit_paths_in_export(paper, db, monkeypatch):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, iid)
    before = repo.report(key)
    identity = before['protocol']['code']
    monkeypatch.setattr('database.paper.code_identity', lambda: dict(identity, sha256='changed'))
    result = repo.export(key)['report']
    assert result['runtime_matches'] is False
    for field in ('equity_rial', 'total_net_rial', 'loss_limit_reached'):
        assert result[field] is None and result['ledger'][field] is None
    assert result['ledger']['realized_net_rial'] is None
    assert result['ledger']['cash_rial'] == before['ledger']['cash_rial']
    assert result['ledger']['fees_rial'] == before['ledger']['fees_rial']
    assert all(p['unrealized_net_rial'] is None for p in result['ledger']['positions'].values())
    assert all(p['realized_net_rial'] is None for p in result['attribution'])


def versions(name):
    if name == 'absent':
        raise PackageNotFoundError(name)
    return dict(numpy='1.0', colorama='2.0')[name]


@pytest.mark.parametrize('platform,count', [('linux', 1), ('win32', 2)])
def test_environment_receipt_and_markers_are_platform_aware(tmp_path, platform, count):
    lock, marker = tmp_path/'lock.txt', tmp_path/'receipt'
    lock.write_text('numpy==1.0\ncolorama==2.0 ; sys_platform == "win32"\n', newline='\n')
    options = dict(installed_version=versions, python_minor=(3, 11), marker_environment={'sys_platform': platform})
    assert inspect_environment(lock, marker, **options)['ready'] is False
    report = record_environment(lock, marker, **options)
    assert report['ready'] and report['checked_packages'] == count
    lock.write_bytes(lock.read_bytes().replace(b'\n', b'\r\n'))
    assert inspect_environment(lock, marker, **options)['ready']  # ZIP/git line endings are equivalent.
    marker.write_text('old')
    assert not inspect_environment(lock, marker, **options)['ready']
    record_environment(lock, marker, **options)
    lock.write_text('numpy==2.0\n')
    old = marker.read_bytes()
    with pytest.raises(ValueError, match='numpy'):
        record_environment(lock, marker, **options)
    assert marker.read_bytes() == old


@pytest.mark.parametrize('requirement', ['absent==1.0', 'numpy>=1.0', 'numpy @ https://invalid.example/a.whl', 'bad >>>'])
def test_environment_never_records_missing_or_unpinned_packages(tmp_path, requirement):
    lock, marker = tmp_path/'lock.txt', tmp_path/'receipt'
    lock.write_text(requirement)
    with pytest.raises(ValueError):
        record_environment(lock, marker, installed_version=versions, python_minor=(3, 11))
    assert not marker.exists()


def test_environment_rejects_wrong_python_and_launcher_wires_guard_before_streamlit(tmp_path):
    lock, marker = tmp_path/'lock.txt', tmp_path/'receipt'
    lock.write_text('numpy==1.0\n')
    result = inspect_environment(lock, marker, installed_version=versions, python_minor=(3, 12))
    assert not result['dependencies_match']
    launch = (settings.BASE_DIR/'START.bat').read_text(encoding='utf-8')
    setup = (settings.BASE_DIR/'SETUP.bat').read_text(encoding='utf-8')
    assert launch.index('-m ops.environment --check') < launch.index('-m streamlit run')
    assert launch.index('setlocal') < launch.index('set "PYTHONUTF8=1"') < launch.index('-m ops.environment --check')
    assert '-m ops.environment --record' in setup


def test_corrupt_environment_receipt_is_repaired_only_when_packages_match(tmp_path):
    lock, marker = tmp_path/'lock.txt', tmp_path/'receipt'
    lock.write_text('numpy==1.0\n')
    marker.write_bytes(b'\xff\xfe')
    assert not inspect_environment(lock, marker, installed_version=versions)['ready']
    assert record_environment(lock, marker, installed_version=versions)['ready']


def test_study_hash_keeps_content_identity_without_loading_entire_file(tmp_path, monkeypatch):
    from ops.stock_study import _sha

    body = b'public-audit-content\n' * 200000
    target = tmp_path/'public-evidence.bin'
    target.write_bytes(body)
    expected = hashlib.sha256(body).hexdigest()
    monkeypatch.setattr(Path, 'read_bytes', lambda _: pytest.fail('Hash must stream large artifacts'))
    assert _sha(target) == expected
