"""Cut/resume/cold replay must preserve the exact frozen financial paths."""

import json
import zipfile
from pathlib import Path

import pytest

from config import settings
from research.search_ledger import summarize_ledger
from research.strategy_execution import review_execution_followup, run_execution_followup
from research.strategy_suite import SuitePlan, run_suite
from tests.test_strategy_execution import publication_payload
from tests.test_strategy_suite import inputs


@pytest.fixture
def frozen_inputs(db, tmp_path, monkeypatch):
    import research.labels as labels
    import research.strategy_suite as suite

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(labels, 'CALENDAR_MIN_SYMBOLS', 2)
    raw, clients, registry, days = inputs(150, ('a', 'b', 'c'))
    missing = days[63]
    raw = raw.loc[~(raw.symbol.eq('a') & raw.date.eq(missing))].reset_index(drop=True)
    clients = clients.merge(raw[['symbol', 'date']], on=['symbol', 'date'], validate='one_to_one')
    monkeypatch.setattr(suite, 'snapshot_inputs', lambda *_: (raw, clients, registry))
    parent = run_suite(db, plan=SuitePlan(start=days[0], end_exclusive='2024-07-01', initial_capital_rial=10000,
        min_daily_value_rial=0, max_positions=2, min_train_sessions=60, test_sessions=30),
        case_names=['base:buyer_power:h1'])
    source = settings.DATA_DIR / 'private' / 'observations.json'
    source.write_text(json.dumps(publication_payload('1', 'a', missing)), encoding='utf-8')
    return Path(parent['folder']), source


def replay_bytes(folder):
    with zipfile.ZipFile(Path(folder) / 'replays.zip') as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_controlled_interruption_then_resume_reuses_only_completed_folds_and_matches_cold(frozen_inputs):
    parent, source = frozen_inputs
    def stop(event):
        if event.get('stage') == 'native_execution_fold_complete':
            raise KeyboardInterrupt('test controlled interruption after one committed fold')
    with pytest.raises(KeyboardInterrupt):
        run_execution_followup(parent, source, progress=stop)
    assert summarize_ledger()['status_counts'].get('interrupted') == 1
    resumed = run_execution_followup(parent, source, allow_repeat=True)
    count = len(json.loads((parent / 'protocol.json').read_bytes())['folds'])
    assert count >= 2
    assert resumed['computation'] == dict(cache_enabled=True, reused_folds=1, recomputed_folds=count-1,
                                        new_independent_evidence=False)
    cold = run_execution_followup(parent, source, use_cache=False, allow_repeat=True)
    assert replay_bytes(resumed['folder']) == replay_bytes(cold['folder'])
    assert resumed['records'] == cold['records']
    assert review_execution_followup(resumed['folder'], reexecute=True)['financial_reexecution_performed']
    assert summarize_ledger()['status_counts'] == {'complete': 3, 'interrupted': 1}


def test_parallel_cache_misses_then_warm_serial_reuse_are_identical_and_do_no_replay(frozen_inputs, monkeypatch):
    import research.strategy_execution as module

    parent, source = frozen_inputs
    parallel = run_execution_followup(parent, source, workers=2)
    assert parallel['computation']['reused_folds'] == 0
    assert parallel['computation']['recomputed_folds'] >= 2
    def forbidden(*args, **kwargs):
        pytest.fail('Warm checkpoint unexpectedly recomputed a financial path')
    monkeypatch.setattr(module, '_fold_job', forbidden)
    warm = run_execution_followup(parent, source, allow_repeat=True)
    assert warm['computation']['recomputed_folds'] == 0
    assert warm['computation']['reused_folds'] == parallel['computation']['recomputed_folds']
    assert replay_bytes(warm['folder']) == replay_bytes(parallel['folder'])
    assert warm['records'] == parallel['records']


def test_source_corruption_is_rejected_even_if_a_valid_checkpoint_already_exists(frozen_inputs):
    parent, source = frozen_inputs
    first = run_execution_followup(parent, source)
    assert first['computation']['recomputed_folds'] > 0
    (parent / 'prices.parquet').write_bytes(b'corrupted after checkpoint publication')
    with pytest.raises(ValueError, match='hash'):
        run_execution_followup(parent, source, allow_repeat=True)


@pytest.mark.parametrize('change', ['fees', 'cutoff'])
def test_worker_cannot_compute_under_settings_different_from_its_checkpoint_contract(tmp_path, change):
    from research.strategy_execution import _cached_fold_job

    fees = dict(settings.FEES)
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    protocol = dict(fees=dict(fees), holdout_end_exclusive=cutoff)
    if change == 'fees':
        protocol['fees']['buy_commission'] = 999
    else:
        protocol['holdout_end_exclusive'] = '1900-01-01'
    with pytest.raises(ValueError, match='financial settings'):
        _cached_fold_job(tmp_path, protocol, b'', 0, tmp_path / 'out.zip', fees, cutoff,
                         tmp_path / 'private/cache', tmp_path / 'private')
    assert not (tmp_path / 'out.zip').exists()


def test_nonzero_fold_subset_preserves_source_decisions_and_its_exact_denominator(frozen_inputs):
    parent, source = frozen_inputs
    subset = run_execution_followup(parent, source, fold_indices=[1])
    assert subset['expected_replays'] == 2
    assert {row['fold'] for row in subset['records']} == {1}
    assert subset['computation']['recomputed_folds'] == 1
    assert review_execution_followup(subset['folder'], reexecute=True)['financial_reexecution_performed']
    all_folds = run_execution_followup(parent, source)
    # A larger scope is a different contract; do not silently reuse the narrower checkpoint.
    assert all_folds['computation']['reused_folds'] == 0
    for row in subset['records']:
        match = next(r for r in all_folds['records'] if (r['case'],r['fold'],r['scenario']) ==
                     (row['case'],row['fold'],row['scenario']))
        assert row['sha256'] == match['sha256']


@pytest.mark.parametrize('indices', [[], [True], [-1], [999], [1, 0], [0, 0], ['0'], [None]])
def test_invalid_or_changed_scope_is_rejected_before_execution(tmp_path, indices):
    from dataclasses import asdict

    from research.strategy_catalog import fixed_cases
    from research.strategy_execution import _scope

    protocol = dict(cases=[asdict(fixed_cases()[0])], folds=[{}, {}])
    with pytest.raises(ValueError):
        _scope(protocol, dict(evaluation_fold_indices=indices))


@pytest.mark.parametrize('reverse', [False, True])
def test_requested_case_order_is_canonicalized_before_any_calculation(tmp_path, monkeypatch, reverse):
    from dataclasses import asdict

    import research.strategy_execution as module
    from research.strategy_catalog import fixed_cases

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    parent = settings.DATA_DIR / 'private' / 'parent'
    parent.mkdir(parents=True)
    selected = [case for case in fixed_cases() if case.name in ('cash', 'base:momentum_5d:h1')]
    names = [case.name for case in selected]
    (parent / 'protocol.json').write_text(json.dumps(dict(cases=[asdict(c) for c in selected], folds=[{}])))
    source = parent / 'publications.json'
    source.write_text('{}')
    monkeypatch.setattr(module, 'review_suite', lambda *_: None)
    def inspect(protocol, scope):
        assert scope['evaluation_case_names'] == names
        raise RuntimeError('scope inspection stopped before computation')
    monkeypatch.setattr(module, '_scope', inspect)
    with pytest.raises(RuntimeError, match='scope inspection'):
        run_execution_followup(parent, source, case_names=names[::-1] if reverse else names)
    assert not (settings.DATA_DIR / 'private' / 'research-search').exists()


def test_unknown_diagnostic_values_have_one_json_safe_type_in_cold_and_cached_api(frozen_inputs, monkeypatch):
    import research.strategy_execution as module

    parent, source = frozen_inputs
    monkeypatch.setattr(module, '_decision_diagnostics', lambda *_: dict(first_unknown={'quantity': float('nan')}))
    cold = run_execution_followup(parent, source, use_cache=False)
    cached = run_execution_followup(parent, source, allow_repeat=True)
    assert cold['records'] == cached['records']
    assert all(row['decision_diagnostics']['first_unknown']['quantity'] is None for row in cold['records'])
    assert replay_bytes(cold['folder']) == replay_bytes(cached['folder'])
