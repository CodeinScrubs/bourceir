"""Finite strategy coverage, causal decisions, independent accounting and archives."""

import json
import subprocess
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.evidence import audit_daily_replay
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from database.db import CLIENT_TYPE_COLUMNS
from research.strategy_catalog import catalogue, fixed_cases
from research.strategy_suite import (
    SCENARIOS,
    SuitePlan,
    _configuration,
    _decision_diagnostics,
    _freeze_code,
    _sha256,
    build_scores,
    fold_picks,
    review_suite,
    run_suite,
    snapshot_inputs,
    summarize_records,
    validate_inputs,
)
from tests.test_portfolio import config, history, picks


def inputs(n=100, symbols=('a', 'b')):
    prices, days = history(n, symbols)
    prices['trade_count'] = 10
    prices['low_raw'], prices['high_raw'] = 96., 105.
    clients = prices[['date', 'symbol']].copy()
    for side in ('buy', 'sell'):
        for participant in ('i', 'n'):
            clients[f'{side}_{participant}_volume'] = 600. if participant == 'i' else 400.
            clients[f'{side}_{participant}_value'] = 60000. if participant == 'i' else 40000.
            clients[f'{side}_{participant}_count'] = 10
    clients = clients[['symbol', 'date', *CLIENT_TYPE_COLUMNS]]
    registry = pd.DataFrame([dict(symbol_fa=s, ins_code=str(i+1), instrument_type_code=300, is_active=1)
                             for i, s in enumerate(symbols)])
    return prices, clients, registry, days.tolist()


@pytest.fixture(scope='module')
def fixed_market():
    prices, _, _, days = inputs(75)
    return prepare_portfolio_prices(prices), days


@pytest.mark.parametrize('case', fixed_cases(), ids=lambda c: c.name)
@pytest.mark.parametrize('scenario', SCENARIOS)
def test_every_registered_fixed_rule_and_scenario_has_audited_shared_cash_replay(case, scenario, fixed_market):
    prices, days = fixed_market
    candidates = {case.selector: picks(days, ('a', 'b'))}
    if case.selector == 'cash':
        candidates['cash'] = candidates['cash'].iloc[:0]
    selected, horizon, positions = fold_picks(case, candidates, days)
    plan = SuitePlan(start=days[0], end_exclusive='2024-04-01', initial_capital_rial=10000,
                     max_positions=2, min_daily_value_rial=0, min_train_sessions=3, test_sessions=75)
    out = replay_portfolio(prices, selected, days, _configuration(case, plan, scenario, horizon, positions),
        dip_rule=case.dip_rule, entry_allocation_fraction=case.allocation, loss_floor_exit=case.family == 'surge')
    evidence = audit_daily_replay(out, prices, selected, days)
    assert evidence['invariant_violations'] == []
    assert evidence['observed_path_model_return'] is not None
    assert evidence['economic_return'] is None and evidence['accepted_for_signals'] is False


def test_catalogue_preserves_all_existing_rules_and_exposes_separate_workflows():
    from research.baselines import STRATEGIES
    from research.dip_run import MAIN_RULES, SENSITIVITY_RULES

    cases = fixed_cases()
    assert len(cases) == 330 and len({c.name for c in cases}) == len(cases)
    assert {c.selector for c in cases if c.family == 'baseline'} == {s.name for s in STRATEGIES}
    assert {c.dip_rule for c in cases if c.family == 'dip'} == set(MAIN_RULES + SENSITIVITY_RULES)
    assert all(c.sensitivity_only == (c.dip_rule is not None and not c.dip_rule.conservative) for c in cases)
    inventory = catalogue()
    for item in inventory['separate_registered_workflows'] + inventory['descriptive_studies']:
        assert (settings.BASE_DIR / item['source']).is_file()
        assert (settings.BASE_DIR / item['tests']).is_file()
    assert not inventory['exhaustive_over_all_possible_algorithms']


@pytest.mark.parametrize('kwargs', [dict(end_exclusive='2025-03-22'), dict(start='2024-2-01'),
    dict(initial_capital_rial=True), dict(initial_capital_rial=float('inf')), dict(test_sessions=0),
    dict(participation=.051), dict(stress_participation=.06), dict(stress_slippage_bps=1)])
def test_plan_rejects_unsealed_or_inconsistent_configuration(kwargs):
    with pytest.raises(ValueError):
        SuitePlan(**kwargs)


@pytest.mark.parametrize('defect', ['future', 'duplicate', 'adjusted', 'bad_symbol', 'missing_client', 'fund'])
def test_market_snapshot_cannot_mix_future_keys_adjusted_prices_or_other_instruments(defect):
    prices, clients, registry, _ = inputs()
    if defect == 'future':
        prices.loc[0, 'date'] = '2025-03-21'
    elif defect == 'duplicate':
        prices = pd.concat([prices, prices.iloc[:1]])
    elif defect == 'adjusted':
        prices['adj_close'] = 999.
    elif defect == 'bad_symbol':
        prices.loc[0, 'symbol'] = 'ك'
    elif defect == 'missing_client':
        clients = clients.iloc[1:]
    else:
        registry.loc[0, 'instrument_type_code'] = 305
    with pytest.raises(ValueError):
        validate_inputs(prices, clients, registry, SuitePlan())


def test_missing_client_values_are_retained_and_never_fake_buyer_power():
    prices, clients, registry, days = inputs()
    clients[CLIENT_TYPE_COLUMNS] = np.nan
    plan = SuitePlan(start=days[0], min_daily_value_rial=0)
    validate_inputs(prices, clients, registry, plan)
    _, candidates, coverage = build_scores(prices, clients, np.array(days), plan)
    assert candidates['buyer_power'].empty and candidates['real_inflow_5d'].empty
    assert coverage['buyer_power']['unknown_score_rows'] == len(prices)


def test_scores_and_random_controls_are_prefix_invariant_and_order_independent():
    prices, clients, _, days = inputs(110, tuple(f's{i}' for i in range(20)))
    plan = SuitePlan(start=days[0], min_daily_value_rial=0)
    _, full, _ = build_scores(prices, clients, np.array(days), plan)
    before = days[85]
    _, prefix, _ = build_scores(prices.loc[prices.date.lt(before)].sample(frac=1, random_state=7),
        clients.loc[clients.date.lt(before)].sample(frac=1, random_state=5), np.array(days[:85]), plan)
    assert set(full) == set(prefix)
    for selector in full:
        if selector == 'buy_hold':
            continue
        expected = full[selector].loc[full[selector].date.lt(before)].reset_index(drop=True)
        pd.testing.assert_frame_equal(expected, prefix[selector].reset_index(drop=True))


def test_tail_uses_entry_plus_holding_sessions_and_never_expost_outcomes():
    _, _, _, days = inputs(12)
    case = next(c for c in fixed_cases() if c.name == 'base:momentum_5d:h2')
    selected, horizon, _ = fold_picks(case, {case.selector: picks(days)}, days)
    assert horizon == 2 and selected.date.max() == days[-4]
    assert days.index(selected.date.max()) + 1 + horizon == len(days)-1


def test_independent_auditor_detects_fees_range_capacity_lookahead_and_cash_corruption():
    prices, days = history(6)
    selected = picks(days)
    out = replay_portfolio(prices, selected, days, config())
    assert audit_daily_replay(out, prices, selected, days)['invariant_violations'] == []
    out.executions.loc[0, 'price_rial'] = 500.
    out.executions.loc[0, 'quantity'] = 9000
    out.equity.loc[2, 'cash_rial'] += 1
    observed = audit_daily_replay(out, prices, selected.iloc[:0], days)
    assert {'execution_outside_observed_range', 'execution_fee_mismatch', 'shared_daily_participation_exceeded',
            'shared_cash_not_reconciled', 'entry_without_previous_session_decision'} <= set(observed['invariant_violations'])
    assert observed['observed_path_model_return'] is None


def test_stale_terminal_model_return_is_not_audited_as_known_profit():
    prices, days = history(8, ('a',))
    # No required exit before the end. The engine retains its historical mark;
    # the evidence layer must not interpret it as a known current value.
    prices = prices.loc[prices.date.ne(days[-1])]
    selected = picks(days[:1], ('a',))
    out = replay_portfolio(prices, selected, days, config(max_positions=1, horizon=20))
    assert out.metrics['total_return'] is not None
    audit = audit_daily_replay(out, prices, selected, days)
    assert audit['observed_path_model_return'] is None and audit['modeled_cash_return'] is None
    assert 'stale_valuation' in audit['blockers']


def test_stale_fraction_is_unknown_even_after_all_whole_shares_have_been_sold():
    prices, days = history(8, ('a',))
    fields = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']
    prices.loc[prices.date.ge(days[2]), fields] *= .8
    prices = prices.loc[prices.date.ne(days[-1])]
    selected = picks(days[:1], ('a',))
    out = replay_portfolio(prices, selected, days, config(max_positions=1))
    assert out.metrics['open_positions'] == 0 and out.metrics['noncash_receivable_rial'] > 0
    audit = audit_daily_replay(out, prices, selected, days)
    assert audit['observed_path_model_return'] is None and audit['maximum_observed_drawdown'] is None
    assert audit['stale_sessions'] > 0


def test_missing_required_exit_is_present_in_first_unknown_diagnostic():
    prices, days = history(8, ('a',))
    prices = prices.loc[prices.date.ne(days[2])]
    out = replay_portfolio(prices, picks(days[:1], ('a',)), days, config(max_positions=1))
    assert out.metrics['total_return'] is None
    first = _decision_diagnostics(out)['first_unknown']
    assert first['status'] == 'missing_exit_bar' and first['date'] == days[2]


@pytest.mark.parametrize('defect', ['unknown_activity', 'buy_queue'])
def test_independent_auditor_rejects_unusable_bars_and_assumed_queue_fills(defect):
    prices, days = history(8, ('a',))
    chosen = picks(days[:1], ('a',))
    out = replay_portfolio(prices, chosen, days, config(max_positions=1))
    selected = prices.date.eq(days[1])
    if defect == 'unknown_activity':
        prices.loc[selected, 'value'] = np.nan
        expected = 'execution_from_unusable_raw_bar'
    else:
        prices.loc[selected, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw']] = 103.
        prices.loc[selected, 'value'] = 103000.
        expected = 'execution_in_assumed_locked_queue'
    audit = audit_daily_replay(out, prices, chosen, days)
    assert expected in audit['invariant_violations'] and audit['observed_path_model_return'] is None


def test_frozen_source_includes_import_dependencies_and_imports_outside_checkout(tmp_path):
    bundle = tmp_path / 'source.zip'
    hashes = _freeze_code(bundle)
    assert {'ops/report_files.py', 'data_collector/tsetmc.py', 'research/strategy_suite.py'} <= set(hashes)
    assert not any('.env' in Path(name).parts or name.startswith(('data/', 'logs/')) for name in hashes)
    target = tmp_path / 'isolated'
    target.mkdir()
    with zipfile.ZipFile(bundle) as archive:
        for name in archive.namelist():
            path = (target / name).resolve()
            assert path.is_relative_to(target.resolve())
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(archive.read(name))
    code = ('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
            'import research.strategy_suite as suite; import ops.report_files as reports; '
            'assert Path(suite.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()); '
            'assert Path(reports.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()); print("isolated-import-ok")')
    out = subprocess.run([sys.executable, '-I', '-B', '-c', code, str(target)], cwd=target,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == 'isolated-import-ok'


def test_snapshot_reads_only_market_tables_and_does_not_open_holdout_or_change_database(db):
    import sqlite3

    db.init_db()
    prices, clients, _, _ = inputs(8, ('a',))
    symbol_id = db.upsert_symbol(dict(symbol_fa='a', ins_code='1', instrument_type_code=300))
    future = prices.iloc[:1].assign(date=settings.RESEARCH_CONFIG['holdout_start'])
    db.upsert_daily_prices_df(symbol_id, pd.concat([prices, future]), recompute=False)
    db.upsert_client_types_df(symbol_id, clients.iloc[:2])
    read_tables = set()
    def authorize(action, table, *rest):
        if action == sqlite3.SQLITE_READ:
            read_tables.add(table)
        return sqlite3.SQLITE_OK
    before = db.conn.total_changes
    db.conn.set_authorizer(authorize)
    try:
        raw, flow, registry = snapshot_inputs(db, SuitePlan())
    finally:
        db.conn.set_authorizer(None)
    assert set(read_tables) <= {'daily_prices', 'client_types_daily', 'symbols', 'scoped'}
    assert len(raw) == len(flow) == 8 and len(registry) == 1
    assert flow.buy_i_value.isna().sum() == 6
    assert raw.date.max() < settings.RESEARCH_CONFIG['holdout_start']
    assert db.conn.total_changes == before and not db.conn.in_transaction
    with db.conn:
        db.conn.execute("UPDATE daily_prices SET price_unit='Toman' WHERE date='2024-01-01'")
    with pytest.raises(ValueError, match='units'):
        snapshot_inputs(db, SuitePlan())


def test_partial_folds_remain_unknown_in_summary_denominator():
    case = fixed_cases()[0]
    record = dict(case=case.name, fold=0, scenario='normal', audit=dict(observed_path_model_return=.2,
        modeled_cash_return=.2, fees_rial=10, blockers=[], invariant_violations=[]))
    result = summarize_records([record], [case], 2)[0]['scenarios']
    assert result['normal']['known_folds'] == 1 and result['normal']['unknown_folds'] == 1
    assert result['normal']['mean_fold_model_return'] is None
    assert result['stress']['recorded_folds'] == 0 and result['stress']['unknown_folds'] == 2


@pytest.fixture
def small_suite(db, tmp_path, monkeypatch):
    import research.strategy_suite as module

    root = tmp_path / 'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    raw, clients, registry, days = inputs(110, tuple(f's{i}' for i in range(20)))
    monkeypatch.setattr(module, 'snapshot_inputs', lambda *_: (raw, clients, registry))
    plan = SuitePlan(start=days[0], end_exclusive='2024-05-01', initial_capital_rial=10000,
        min_daily_value_rial=0, max_positions=2, min_train_sessions=60, test_sessions=30)
    cases = ['cash', 'buy_hold', 'base:momentum_5d:h1', 'base:buyer_power:h2',
             'dip:dip2%-tp3%-h1-cons:buyer_power']
    result = run_suite(db, plan=plan, case_names=cases)
    return result, Path(result['folder'])


def test_registered_integration_preserves_inputs_denominator_and_replay_evidence(small_suite):
    result, folder = small_suite
    assert result['invariant_failures'] == 0
    review = review_suite(folder)
    assert review['status'] == 'verified_archive' and review['replays'] == result['expected_replays']
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    assert not protocol['complete_fixed_catalogue'] and not protocol['holdout_loaded']
    assert protocol['raw_last'] < settings.RESEARCH_CONFIG['holdout_start']
    from research.search_ledger import summarize_ledger
    registration = protocol['local_search_registration']
    assert len(registration['registrations']) == len(protocol['cases'])
    assert summarize_ledger()['status_counts'] == {'complete': len(protocol['cases'])}
    with zipfile.ZipFile(folder / 'replays.zip') as archive:
        for member in archive.namelist():
            replay = json.loads(archive.read(member))
            assert not replay['audit']['invariant_violations']
    assert not (folder.parent / 'latest.json').exists()
    assert review_suite(folder, reexecute=True)['financial_reexecution_performed']


def test_shared_feature_failure_is_registered_before_compute_and_retained(db, tmp_path, monkeypatch):
    import research.strategy_suite as module
    from research.search_ledger import summarize_ledger

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    raw, clients, registry, days = inputs(110, tuple(f's{i}' for i in range(20)))
    monkeypatch.setattr(module, 'snapshot_inputs', lambda *_: (raw, clients, registry))
    plan = SuitePlan(start=days[0], end_exclusive='2024-05-01', min_train_sessions=60, test_sessions=30)
    def fail(*_):
        assert summarize_ledger()['status_counts'] == {'unfinished': 2}
        raise RuntimeError('test feature failure')
    monkeypatch.setattr(module, 'build_scores', fail)
    with pytest.raises(RuntimeError, match='test feature failure'):
        run_suite(db, plan=plan, case_names=['cash', 'buy_hold'])
    assert summarize_ledger()['status_counts'] == {'failed': 2}
    with pytest.raises(ValueError, match='explicit repeat'):
        run_suite(db, plan=plan, case_names=['cash', 'buy_hold'])
    assert summarize_ledger()['registered_attempts'] == 2


def test_preflight_receipt_cannot_understate_matrix_even_if_file_hashes_are_updated(small_suite):
    _, folder = small_suite
    admission = json.loads((folder / 'compute-preflight.json').read_bytes())
    admission['expected_replays'] = 0
    (folder / 'compute-preflight.json').write_text(json.dumps(admission), encoding='utf-8')
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    protocol['compute_preflight_sha256'] = _sha256(folder / 'compute-preflight.json')
    (folder / 'protocol.json').write_text(json.dumps(protocol), encoding='utf-8')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    for name in ('compute-preflight.json', 'protocol.json'):
        manifest[name] = _sha256(folder / name)
    (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='preflight'):
        review_suite(folder)


def test_reexecution_checks_source_again_before_issuing_verification(small_suite, monkeypatch):
    import research.strategy_suite as module

    _, folder = small_suite
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    calls = []
    def fingerprint():
        calls.append(True)
        return protocol['code_sha256'] if len(calls) == 1 else 'f' * 64
    monkeypatch.setattr(module, 'research_code_fingerprint', fingerprint)
    with pytest.raises(ValueError, match='Source changed during'):
        review_suite(folder, reexecute=True)


@pytest.mark.parametrize('defect', ['raw', 'extra', 'missing_record', 'duplicate_member'])
def test_review_rejects_tampering_missing_rows_and_duplicate_archive_members(small_suite, defect):
    _, folder = small_suite
    if defect == 'raw':
        (folder / 'prices.parquet').write_bytes(b'changed')
    elif defect == 'extra':
        (folder / 'extra.txt').write_text('unexpected', encoding='utf-8')
    elif defect == 'missing_record':
        path = folder / 'results.json'
        value = json.loads(path.read_bytes())
        value['records'].pop()
        path.write_text(json.dumps(value), encoding='utf-8')
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest[path.name] = _sha256(path)
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    else:
        with zipfile.ZipFile(folder / 'replays.zip', 'a') as archive:
            name = archive.namelist()[0]
            with pytest.warns(UserWarning, match='Duplicate name'):
                archive.writestr(name, archive.read(name))
        manifest = json.loads((folder / 'manifest.json').read_bytes())
        manifest['replays.zip'] = _sha256(folder / 'replays.zip')
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        review_suite(folder)


def test_invalid_subset_fails_before_snapshot_or_file_writes(db, tmp_path, monkeypatch):
    import research.strategy_suite as module

    monkeypatch.setattr(module, 'snapshot_inputs', lambda *_: pytest.fail('must not read data'))
    for names in ([], ['cash', 'cash'], ['new-unregistered-strategy']):
        with pytest.raises(ValueError):
            run_suite(db, case_names=names)


def test_cli_catalog_and_invalid_temporal_boundary(capsys):
    import brs

    assert brs.main(['strategy-suite']) == 0
    assert len(json.loads(capsys.readouterr().out)['cases']) == 330
    assert brs.main(['strategy-suite', '--run', '--before', '2030-01-01']) != 0
