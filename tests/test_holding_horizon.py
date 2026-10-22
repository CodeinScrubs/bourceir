"""A fixed long-horizon screen, including real replay, archive tampering and date guards."""

import json

import pandas as pd
import pytest

from research.holding_horizon import HOLDING_SESSIONS, _jobs, fold_picks, review_holding_study, run_holding_study
from research.run import _sha256

pytest_plugins = ['tests.test_flow_research']


def run(db, flow_baseline, tmp_path):
    source, root = flow_baseline
    output = tmp_path / 'holding'
    result = run_holding_study(db, source_root=root / 'research', source_id=source['experiment_id'],
        root=output, initial_capital_rial=100_000_000, prior_trials=32, min_train_sessions=20, test_sessions=70)
    return result, output / result['experiment_id']


def test_fixed_holding_replay_registers_first_reconciles_cash_and_reviews(db, flow_baseline, tmp_path, monkeypatch):
    from research import holding_horizon

    events = []
    register, select = db.log_experiment, holding_horizon.fold_picks

    def logged(**kwargs):
        events.append(kwargs['experiment_id'])
        return register(**kwargs)

    def picked(*args):
        assert events and events[0].endswith('-PROTOCOL')
        return select(*args)

    monkeypatch.setattr(db, 'log_experiment', logged)
    monkeypatch.setattr(holding_horizon, 'fold_picks', picked)
    select_past = holding_horizon.select_from_past

    def past_only(panel, fold, *args):
        assert panel.date.lt(fold.train_end_exclusive).all()
        return select_past(panel, fold, *args)

    monkeypatch.setattr(holding_horizon, 'select_from_past', past_only)
    result, folder = run(db, flow_baseline, tmp_path)
    assert events == [result['experiment_id']+'-PROTOCOL', result['experiment_id']]
    p = result['protocol']
    assert p['holding_sessions'] == [5, 20, 60] and p['engine_horizons'] == [4, 19, 59]
    assert p['minimum_disclosed_trials'] == 59 and p['modes_in_this_run'] == 27
    assert not result['accepted_for_signals'] and not result['profitability_proven'] and not result['holdout_eligible']
    assert len(result['results']) == 1 and len(result['results'][0]['modes']) == 27
    picks = [pd.read_parquet(folder / f'fold-00-hold{h}-momentum_20d-picks.parquet') for h in HOLDING_SESSIONS]
    assert len(picks[0]) > 0
    for other in picks[1:]:
        pd.testing.assert_frame_equal(picks[0], other)
    for mode in result['results'][0]['modes'].values():
        assert mode['cash_ledger']['recorded_fees_rial'] == pytest.approx(mode['metrics']['fees_rial'])
    reviewed = review_holding_study(db, folder)
    assert reviewed['modes_recomputed'] == 27 and reviewed['selection_and_financial_tables_recomputed']
    assert reviewed['current_code_matches_capture']


def test_changed_picks_are_rejected_even_when_their_hash_is_updated(db, flow_baseline, tmp_path):
    _, folder = run(db, flow_baseline, tmp_path)
    report = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    name = 'fold-00-hold20-momentum_20d-picks.parquet'
    frame = pd.read_parquet(folder / name)
    frame.loc[0, 'rank'] = 5
    frame.to_parquet(folder / name, index=False)
    report['artifacts_sha256'][name] = _sha256(folder / name)
    (folder / 'result.json').write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(AssertionError):
        review_holding_study(db, folder)


def test_changed_execution_is_recomputed_after_rehash(db, flow_baseline, tmp_path):
    _, folder = run(db, flow_baseline, tmp_path)
    report = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    name = 'fold-00-hold20-momentum_20d-executions.parquet'
    frame = pd.read_parquet(folder / name)
    assert len(frame) > 0
    frame.loc[0, 'price_rial'] += 1
    frame.to_parquet(folder / name, index=False)
    report['artifacts_sha256'][name] = _sha256(folder / name)
    (folder / 'result.json').write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(AssertionError):
        review_holding_study(db, folder)


def test_failed_registration_never_selects_or_publishes(db, flow_baseline, tmp_path, monkeypatch):
    from research import holding_horizon

    monkeypatch.setattr(db, 'log_experiment', lambda **kwargs: None)
    monkeypatch.setattr(holding_horizon, 'fold_picks', lambda *a: pytest.fail('selection before registration'))
    with pytest.raises(RuntimeError, match='ثبت پروتکل'):
        run(db, flow_baseline, tmp_path)
    assert not list((tmp_path / 'holding').rglob('result.json'))
    assert len(list((tmp_path / 'holding').rglob('protocol.json'))) == 1


def test_all_long_and_short_controls_share_a_predefined_intake_window():
    from backtest.portfolio import PortfolioConfig

    dates = pd.date_range('2024-01-01', periods=100).strftime('%Y-%m-%d').to_numpy()
    panel = pd.DataFrame([dict(date=d, symbol=s, value=100., ret_20d=float(i))
                          for d in dates for i, s in enumerate(('الف', 'ب', 'ج'))])
    picks = fold_picks(panel, dates, 2, 0)
    for frame in picks.values():
        assert frame.date.max() < dates[-61]
        assert frame.groupby('date').size().eq(2).all()
    jobs = _jobs(picks, PortfolioConfig(initial_capital_rial=1_000_000),
                 (pd.DataFrame(columns=['date', 'symbol', 'rank']), 2))
    for hold in HOLDING_SESSIONS:
        for suffix in ('', '-stress'):
            selected, config = jobs[f'hold{hold}-momentum_20d'+suffix]
            pd.testing.assert_frame_equal(selected, picks['momentum_20d'])
            assert config.horizon == hold-1
    with pytest.raises(ValueError, match='روز تصمیم'):
        fold_picks(panel.assign(net_ret_20d=10.), dates, 2, 0)


def test_short_folds_are_not_silently_removed(db, flow_baseline, tmp_path):
    source, root = flow_baseline
    with pytest.raises(ValueError, match='بیش از شصت'):
        run_holding_study(db, source_root=root/'research', source_id=source['experiment_id'], root=tmp_path/'short',
                          initial_capital_rial=100_000_000, prior_trials=32, min_train_sessions=20, test_sessions=50)
    assert not (tmp_path / 'short').exists()


def test_missing_outcomes_never_become_a_positive_known_average():
    from research.holding_horizon import _modes, _summarize

    known = dict(selected_opportunities=1, buy_executions=1, sell_executions=1, net_return=.2,
        recorded_fees_fraction_initial=.01, entry_outcomes_complete=True, diagnosis='net_positive',
        mean_noncash_equity_share=.5, max_drawdown=.02, corporate_action_events=0)
    unknown = dict(known, net_return=None, entry_outcomes_complete=False, diagnosis='unknown')
    records = [dict(modes={m: dict(cash_ledger=known) for m in _modes()}),
               dict(modes={m: dict(cash_ledger=unknown) for m in _modes()})]
    summary, comparisons = _summarize(records)
    assert summary['hold60-momentum_20d']['all_fold_mean'] is None
    assert summary['hold60-momentum_20d']['unknown_folds'] == 1
    assert comparisons['60']['stress']['mean_edge_to_past_selected_baseline'] is None


def test_reviewer_checks_dates_before_reading_sealed_equity(db, flow_baseline, tmp_path, monkeypatch):
    from config import settings

    _, folder = run(db, flow_baseline, tmp_path)
    report = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    name = 'fold-00-idle_cash-equity.parquet'
    frame = pd.read_parquet(folder / name)
    frame.loc[0, 'date'] = settings.RESEARCH_CONFIG['holdout_start']
    frame.to_parquet(folder / name, index=False)
    report['artifacts_sha256'][name] = _sha256(folder / name)
    (folder / 'result.json').write_text(json.dumps(report), encoding='utf-8')
    actual = pd.read_parquet
    columns_seen = []

    def observed(path, *args, **kwargs):
        if hasattr(path, 'read'):
            columns_seen.append(kwargs.get('columns'))
        return actual(path, *args, **kwargs)

    monkeypatch.setattr(pd, 'read_parquet', observed)
    with pytest.raises(ValueError, match='مهروموم'):
        review_holding_study(db, folder)
    assert columns_seen[-1] == ['date']
