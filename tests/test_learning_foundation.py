"""Learning errors must be cheaper than training and cannot become valid rewards."""

import json
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

pytest.importorskip('gymnasium', reason='Install requirements-training.lock.txt for the optional learning suite')
pytest.importorskip('sklearn', reason='Install requirements-training.lock.txt for the optional learning suite')

from backtest.portfolio import replay_portfolio
from config import settings
from research.learning import (
    LearningBudget,
    evaluate,
    fit_q,
    fit_ridge,
    load_model,
    run_evaluation,
    run_training,
)
from research.rl_env import EntryTimingEnv, InvalidEpisode
from research.training_data import (
    LearningPlan,
    atomic_json,
    load_role,
    prepare_bundles,
    read_json,
    sha256,
    standardized,
)
from tests.test_portfolio import history


@pytest.fixture
def learning_bundle(tmp_path):
    prices, days = history(130, ('a', 'b'))
    registry = pd.DataFrame(dict(symbol=['a', 'b'], instrument_type_code=[300, 303]))
    plan = LearningPlan(('a', 'b'), str(days[30]), str(days[65]), str(days[68]), str(days[88]),
                        str(days[91]), str(days[115]), initial_capital_rial=10000)
    root = prepare_bundles(prices, days, registry, plan, tmp_path / 'bundles',
                           provenance=dict(source='tests only', historical_membership_verified=False))
    return root, prices, days, registry, plan


def test_role_files_exclude_future_rows_adjusted_prices_and_account_columns(learning_bundle):
    root, _, _, _, plan = learning_bundle
    train = load_role(root / 'train', 'train')
    assert train.prices.date.max() < plan.train_end
    assert train.features.date.max() < plan.train_end
    assert set(train.features) == {'date', 'symbol', *train.manifest['predictors']}
    with pytest.raises(ValueError, match='role'):
        load_role(root / 'test', 'train')


def test_test_price_changes_cannot_change_train_scaler_or_train_features(learning_bundle, tmp_path):
    root, prices, days, registry, plan = learning_bundle
    before = load_role(root / 'train', 'train')
    changed = prices.copy()
    fields = ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']
    changed.loc[changed.date.ge(plan.test_start), fields] *= 4
    other = prepare_bundles(changed, days, registry, plan, tmp_path / 'other', provenance=dict(source='tests only'))
    after = load_role(other / 'train', 'train')
    assert after.manifest['scaler'] == before.manifest['scaler']
    pd.testing.assert_frame_equal(before.features, after.features)


def test_feature_unknown_is_masked_and_cannot_be_an_entry(learning_bundle):
    root, *_ = learning_bundle
    data = load_role(root / 'train', 'train')
    features = data.features.copy()
    features.loc[features.date.eq(data.sessions[0]) & features.symbol.eq('a'), 'ret_1d'] = np.nan
    data = replace(data, features=features)
    values, valid = standardized(data, data.sessions[0])
    assert not valid[0] and valid[1] and (values[0] == 0).all()
    env = EntryTimingEnv(data)
    try:
        obs, info = env.reset(seed=7)
        assert np.isfinite(obs).all() and info['missing_feature_symbols'] == ['a']
        with pytest.raises(ValueError, match='masked'):
            env.step(1)
    finally:
        env.close()


def test_interactive_episode_matches_fixed_tape_fees_and_terminal_cash(learning_bundle):
    root, *_ = learning_bundle
    data = load_role(root / 'train', 'train')
    env = EntryTimingEnv(data, stress=True)
    rewards = []
    try:
        _, info = env.reset(seed=42)
        while True:
            action = next((i for i in (1, 2) if info['action_mask'][i]), 0)
            _, reward, done, truncated, info = env.step(action)
            rewards.append(reward)
            assert not truncated
            if done:
                break
        tape = pd.DataFrame(env.tape)
        fixed = replay_portfolio(data.prices, tape, data.sessions, env.config, start=data.sessions[0],
            no_trade_valuation_observations=[], require_explicit_corporate_actions=True)
        pd.testing.assert_frame_equal(fixed.equity, env.result.equity)
        pd.testing.assert_frame_equal(fixed.executions, env.result.executions)
        assert fixed.metrics == env.result.metrics
        assert sum(rewards) == pytest.approx(env.audit['modeled_cash_return'], abs=1e-14)
        assert env.audit['fees_rial'] > 0 and not env.audit['blockers']
        assert fixed.metrics['open_positions'] == 0
    finally:
        env.close()


@pytest.mark.parametrize('damage', ['missing_bar', 'reference_change', 'terminal_queue'])
def test_unusable_episode_raises_instead_of_rewarding_unknowns(learning_bundle, damage):
    root, *_ = learning_bundle
    data = load_role(root / 'train', 'train')
    frame = data.prices.copy()
    day = data.sessions[2]
    row = frame.date.eq(day) & frame.symbol.eq('a')
    if damage == 'missing_bar':
        frame = frame.loc[~row]
    elif damage == 'reference_change':
        frame.loc[row, 'ref_price_raw'] = 101
    else:
        after = frame.date.ge(day) & frame.symbol.eq('a')
        frame.loc[after, ['open_raw','high_raw','low_raw','close_raw','close_final_raw']] = 90
        frame.loc[after, 'value'] = frame.loc[after, 'volume'] * 90
    env = EntryTimingEnv(replace(data, prices=frame))
    try:
        _, info = env.reset()
        env.step(1)
        with pytest.raises(InvalidEpisode, match='discard|Unusable'):
            for _ in data.sessions:
                _, _, done, _, info = env.step(0)
                if done:
                    break
    finally:
        env.close()


def test_gymnasium_contract_and_reset_close_have_no_live_worker(learning_bundle):
    from gymnasium.utils.env_checker import check_env
    root, *_ = learning_bundle
    env = EntryTimingEnv(load_role(root / 'train', 'train'))
    try:
        check_env(env, skip_render_check=True, skip_close_check=True)
        _, a = env.reset(seed=1)
        _, b = env.reset(seed=1)
        assert a['date'] == b['date']
    finally:
        env.close()
    assert env._worker is None


def test_manifest_tampering_and_invalid_registry_or_embargo_are_rejected(learning_bundle, tmp_path):
    root, prices, days, registry, plan = learning_bundle
    path = root / 'train' / 'manifest.json'
    meta = json.loads(path.read_text(encoding='utf-8'))
    meta['fees']['buy'] = 0
    path.write_text(json.dumps(meta), encoding='utf-8')
    with pytest.raises(ValueError, match='contract'):
        load_role(root / 'train', 'train')
    registry.loc[0, 'instrument_type_code'] = 305
    with pytest.raises(ValueError, match='ordinary'):
        prepare_bundles(prices, days, registry, plan, tmp_path / 'bad', provenance=dict(source='tests only'))
    with pytest.raises(ValueError, match='embargo'):
        prepare_bundles(prices, days, registry.assign(instrument_type_code=300),
            replace(plan, validation_start=plan.train_end), tmp_path / 'bad2', provenance=dict(source='tests only'))


def test_q_cold_and_episode_boundary_resume_are_identical(learning_bundle, tmp_path):
    root, *_ = learning_bundle
    data = load_role(root / 'train', 'train')
    budget = LearningBudget(episodes=4)
    cold = fit_q(data, budget, seed=7, checkpoint=tmp_path / 'cold.json')
    partial = fit_q(data, budget, seed=7, checkpoint=tmp_path / 'partial.json', stop_after=2)
    assert partial['status'] == 'partial' and partial['completed_episodes'] == 2
    resumed = fit_q(data, budget, seed=7, checkpoint=tmp_path / 'resumed.json', resume=tmp_path / 'partial.json')
    assert cold == resumed
    assert cold['steps'] == 4 * (len(data.sessions) - 1)
    with pytest.raises(ValueError, match='contract'):
        fit_q(data, budget, seed=8, checkpoint=tmp_path / 'bad.json', resume=tmp_path / 'partial.json')


def test_unknown_episode_does_not_commit_partial_weight_updates(learning_bundle, tmp_path, monkeypatch):
    root, *_ = learning_bundle
    data = load_role(root / 'train', 'train')
    budget = LearningBudget(episodes=2)
    original = EntryTimingEnv.step
    calls = 0
    def broken(self, action):
        nonlocal calls
        calls += 1
        if calls == len(data.sessions) + 2:
            raise InvalidEpisode('test missing mark in episode two')
        return original(self, action)
    monkeypatch.setattr(EntryTimingEnv, 'step', broken)
    path = tmp_path / 'checkpoint.json'
    with pytest.raises(InvalidEpisode, match='missing mark'):
        fit_q(data, budget, seed=4, checkpoint=path)
    saved = load_model(path)
    assert saved['completed_episodes'] == 1
    assert saved['steps'] == len(data.sessions) - 1
    assert len(saved['episodes']) == 1


def test_ridge_targets_and_normalizer_are_training_only(learning_bundle, tmp_path):
    root, *_, plan = learning_bundle
    train = load_role(root / 'train', 'train')
    model = fit_ridge(train, LearningBudget(), seed=7, checkpoint=tmp_path / 'ridge.json')
    assert all(row['resolved_at'] < plan.train_end for row in model['labels'])
    validation = load_role(root / 'validation', 'validation')
    checked = evaluate(validation, model=model)
    assert checked['audit']['modeled_cash_return'] is not None
    with pytest.raises(ValueError, match='train role'):
        fit_ridge(validation, LearningBudget(), seed=7, checkpoint=tmp_path / 'leak.json')


def test_step_budget_is_checked_before_fitting_and_no_model_is_created(learning_bundle, tmp_path):
    root, *_ = learning_bundle
    path = tmp_path / 'unstarted.json'
    with pytest.raises(ValueError, match='admission'):
        fit_q(load_role(root / 'train', 'train'), LearningBudget(max_steps=1), seed=1, checkpoint=path)
    assert not path.exists()


def test_registration_records_repetitions_and_validation_does_not_open_test(learning_bundle, tmp_path, monkeypatch):
    from research.search_ledger import summarize_ledger
    root, *_ = learning_bundle
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'private-data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'unused-account.db')
    budget = LearningBudget(episodes=2)
    run_training(root / 'train', tmp_path / 'run1', method='q', budget=budget, seed=11)
    with pytest.raises(ValueError, match='repeat authorization'):
        run_training(root / 'train', tmp_path / 'blocked', method='q', budget=budget, seed=11)
    run_training(root / 'train', tmp_path / 'run2', method='q', budget=budget, seed=11, allow_repeat=True)
    # The runner receives only validation; even deleting test cannot affect fitting/evaluation.
    (root / 'test' / 'prices.parquet').unlink()
    report = run_evaluation(root / 'validation', [tmp_path / 'run1' / 'model.json'], tmp_path / 'evaluation')
    assert report['status'] == 'complete_development_evaluation'
    assert report['expected_paths'] == len(report['paths']) == 8
    assert not report['test_opened'] and not report['holdout_opened']
    assert summarize_ledger()['registered_attempts'] == 6
    assert not settings.DB_PATH.exists()


def test_failed_training_is_terminally_logged_not_removed(learning_bundle, tmp_path, monkeypatch):
    from research.search_ledger import summarize_ledger
    root, *_ = learning_bundle
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'private-data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'unused-account.db')
    from research import learning
    def broken(*args, **kwargs):
        raise ValueError('test unusable market input')
    monkeypatch.setattr(learning, 'fit_q', broken)
    with pytest.raises(ValueError, match='unusable'):
        run_training(root / 'train', tmp_path / 'failed', method='q', budget=LearningBudget())
    assert summarize_ledger()['status_counts'] == {'failed': 1}
    assert json.loads((tmp_path / 'failed' / 'result.json').read_text())['status'] == 'failed'


@pytest.mark.parametrize('action', [True, False, 1.0, -1, 3])
def test_ambiguous_rl_actions_are_rejected(learning_bundle, action):
    root, *_ = learning_bundle
    env = EntryTimingEnv(load_role(root / 'train', 'train'))
    try:
        env.reset()
        with pytest.raises(ValueError, match='integer action'):
            env.step(action)
    finally:
        env.close()


def test_supervised_unknown_target_is_not_dropped_or_imputed(learning_bundle, tmp_path):
    root, *_ = learning_bundle
    data = load_role(root / 'train', 'train')
    damaged = data.prices.loc[~(data.prices.date.eq(data.sessions[1]) & data.prices.symbol.eq('a'))].copy()
    checkpoint = tmp_path / 'invalid.json'
    with pytest.raises(ValueError, match='Missing training bars'):
        fit_ridge(replace(data, prices=damaged), LearningBudget(), seed=7, checkpoint=checkpoint)
    assert not checkpoint.exists()


def test_frozen_model_rejects_predictor_permutation_and_nonfinite_weights(learning_bundle, tmp_path):
    from copy import deepcopy

    from research.learning import check_model_for_role
    root, *_ = learning_bundle
    model = fit_ridge(load_role(root / 'train', 'train'), LearningBudget(), seed=7, checkpoint=tmp_path/'ridge.json')
    validation = load_role(root / 'validation', 'validation')
    order = deepcopy(model)
    order['spec']['predictors'] = list(reversed(order['spec']['predictors']))
    with pytest.raises(ValueError, match='contract'):
        check_model_for_role(order, validation)
    model['coefficients'][0] = np.nan
    with pytest.raises(ValueError, match='weights'):
        check_model_for_role(model, validation)


def test_training_cannot_unseal_the_general_holdout(learning_bundle):
    _, *_, plan = learning_bundle
    with pytest.raises(ValueError, match='pre-holdout'):
        replace(plan, test_end='2025-03-22')


def _rehash_role(root):
    from research.search_ledger import identity
    path = root/'manifest.json'
    manifest=read_json(path)
    manifest['files']={name:sha256(root/name) for name in manifest['files']}
    manifest['bundle_id']=identity({k:v for k,v in manifest.items() if k!='bundle_id'})
    atomic_json(path,manifest)


def test_cached_feature_forgery_is_detected_even_with_updated_file_hashes(learning_bundle):
    root,*_=learning_bundle
    path=root/'train/features.parquet'
    frame=pd.read_parquet(path)
    frame.loc[0,'ret_5d']=.714
    frame.to_parquet(path,index=False)
    _rehash_role(root/'train')
    with pytest.raises(ValueError,match='features differ'):
        load_role(root/'train','train')


def test_validation_data_cannot_replace_the_training_scaler(learning_bundle):
    from research.search_ledger import identity
    root,*_=learning_bundle
    path=root/'validation/manifest.json'
    manifest=read_json(path)
    manifest['scaler']['mean'][0]+=.1
    manifest['bundle_id']=identity({k:v for k,v in manifest.items() if k!='bundle_id'})
    atomic_json(path,manifest)
    with pytest.raises(ValueError,match='Normalizer does not reproduce'):
        load_role(root/'validation','validation')


def test_warmup_is_carried_and_all_feature_lookups_match_the_direct_calculation(learning_bundle):
    root,*_,plan=learning_bundle
    data=load_role(root/'train','train')
    assert data.prices.date.lt(plan.train_start).sum()>2
    for day in data.sessions:
        values,valid=standardized(data,day)
        raw=data.features.loc[data.features.date.eq(day)].set_index('symbol').reindex(plan.symbols)
        raw=raw.loc[:,data.manifest['predictors']].to_numpy(dtype=float)
        expected=(raw-np.asarray(data.manifest['scaler']['mean']))/np.asarray(data.manifest['scaler']['scale'])
        known=np.isfinite(raw).all(axis=1)
        expected[~known]=0.
        np.testing.assert_array_equal(values,expected)
        np.testing.assert_array_equal(valid,known)
    assert not data.feature_lookup[1].flags.writeable
    assert data.feature_lookup is data.feature_lookup


def test_unaccounted_reference_changes_block_training_before_episode_one(learning_bundle,tmp_path):
    root,*_=learning_bundle
    data=load_role(root/'train','train')
    prices=data.prices.copy()
    prices.loc[prices.date.eq(data.sessions[4]) & prices.symbol.eq('a'),'ref_price_raw']=101.
    checkpoint=tmp_path/'not-started.json'
    with pytest.raises(ValueError,match='Unsupported training reference'):
        fit_q(replace(data,prices=prices),LearningBudget(),seed=17,checkpoint=checkpoint)
    assert not checkpoint.exists()


def test_registered_filter_query_counts_aliases_once_and_preserves_denominators(learning_bundle, tmp_path, monkeypatch):
    from research.rule_space import run_filter_review
    from research.search_ledger import summarize_ledger
    root, *_ = learning_bundle
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'private-data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path/'unused-account.db')
    rule_file = tmp_path/'rules.json'
    stronger = dict(feature='value_ratio_20d', operator='>=', threshold=1.6)
    weaker = dict(feature='value_ratio_20d', operator='>=', threshold=1.3)
    atomic_json(rule_file, [dict(name='stronger', predicates=[stronger]),
                           dict(name='redundant-alias', predicates=[weaker, stronger])])
    report = run_filter_review(root/'train', rule_file, tmp_path/'query')
    assert report['admission']['nominal_named_rules'] == 2
    assert report['admission']['logical_unique_rules'] == 1
    row, = report['rules']
    assert row['true_rows'] + row['false_rows'] + row['unknown_rows'] == row['rows']
    assert not report['performance_evaluated'] and not report['accepted_for_signals']
    assert summarize_ledger()['registered_attempts'] == 1
    with pytest.raises(ValueError, match='repeat authorization'):
        run_filter_review(root/'train', rule_file, tmp_path/'blocked')
    assert not settings.DB_PATH.exists()


def test_filter_computation_failure_retains_registered_attempt(learning_bundle, tmp_path, monkeypatch):
    from research import rule_space
    from research.search_ledger import summarize_ledger
    root, *_ = learning_bundle
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'private-data')
    rule_file = tmp_path/'rules.json'
    atomic_json(rule_file, [dict(name='one', predicates=[dict(feature='ret_1d', operator='>', threshold=0.)])])
    def broken(*args, **kwargs):
        raise ValueError('test interrupted filter calculation')
    monkeypatch.setattr(rule_space, 'RuleMatrix', broken)
    with pytest.raises(ValueError, match='interrupted filter'):
        rule_space.run_filter_review(root/'train', rule_file, tmp_path/'failed')
    assert summarize_ledger()['status_counts'] == {'failed': 1}
