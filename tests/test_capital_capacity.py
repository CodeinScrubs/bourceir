"""داده ساختگی صرفاً در آزمون و SQLite/فایل موقت؛ هیچ سیگنالی پذیرفته نمی‌شود."""

from dataclasses import replace

import pandas as pd
import pytest

from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from core.capital_capacity import diagnose_cash_ledger, summarize_cases
from ops import capital_capacity as study
from tests.test_dashboard import DASHBOARD
from tests.test_dashboard import dashboard_db as dashboard_db
from tests.test_flow_research import flow_baseline as flow_baseline


@pytest.fixture
def capacity_source(db, tmp_path, monkeypatch):
    from research import labels

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(labels, 'CALENDAR_MIN_SYMBOLS', 1)
    dates = pd.bdate_range('2024-01-02', periods=20).strftime('%Y-%m-%d').tolist()
    prices = pd.DataFrame(dict(date=dates, symbol='آزمون', open_raw=100., high_raw=103., low_raw=97.,
        close_raw=100., close_final_raw=100., ref_price_raw=100., volume=10000., value=1000000.))
    picks = pd.DataFrame(dict(date=[dates[1], dates[6]], symbol=['آزمون'] * 2, rank=[0] * 2))
    key = 'EXP-2024-02-01-FLOW-' + 'a' * 32
    folder = settings.DATA_DIR / 'research' / key
    folder.mkdir(parents=True)
    prices.to_parquet(folder / 'prices.parquet', index=False)
    config = PortfolioConfig(initial_capital_rial=100000., horizon=1, max_positions=2)
    modes, outputs = {}, {}
    for suffix in ('', '-stress'):
        mode = 'post_surge_pullback-1d' + suffix
        result = replay_portfolio(prices, picks, dates, replace(config, stress=bool(suffix)), start=dates[0], end='2024-02-01')
        result.metrics['max_loss_from_initial'] = max(0., 1 - float(result.equity.equity_rial.min()) / config.initial_capital_rial)
        modes[mode] = dict(metrics=result.metrics, assumptions=result.assumptions)
        outputs[mode] = result
        for name, frame in dict(picks=picks, executions=result.executions, equity=result.equity, decisions=result.decisions).items():
            frame.to_parquet(folder / ('fold-00-' + mode + '-' + name + '.parquet'), index=False)
    boundary = dict(train_start='2023-01-01', train_end_exclusive='2024-01-01', test_start=dates[0], test_end_exclusive='2024-02-01')
    source = dict(experiment_id=key, protocol=dict(version='flow-portfolio-v4', portfolio_version='portfolio-v4', filter_version='flow-filters-v4',
        holdout_start='2024-02-01', source_files=study._current_code(study.ENGINE_FILES),
        inputs=dict(prices=study.sha256(folder / 'prices.parquet')), modes=list(modes), folds=[boundary]),
        results=[dict(fold=boundary, modes=modes)],
        artifacts_sha256={p.name: study.sha256(p) for p in folder.glob('*.parquet')})
    for name in ('default.yaml', 'production.yaml'):
        path = folder / 'code/config' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((settings.BASE_DIR / 'config' / name).read_bytes().replace(b'\r\n', b'\n'))
        source['protocol']['source_files']['config/' + name] = study.sha256(path)
    study._write(folder.with_suffix('.json'), source)
    # بررسی کامل منبع در آزمون یکپارچه جداگانه اجرا می‌شود؛ این fixture مرز ماژول را کوچک نگه می‌دارد.
    monkeypatch.setattr(study, 'review_research', lambda *_: dict(verified=dict(report_sha256=study.sha256(folder.with_suffix('.json')))))
    return key, source, prices, picks, outputs


def test_fixed_fill_fee_addback_is_reconciled_but_never_claims_a_zero_cost_backtest(capacity_source):
    _, source, _, picks, outputs = capacity_source
    result = next(iter(outputs.values()))
    row = diagnose_cash_ledger(result.equity, result.executions, result.decisions, result.metrics, len(picks))
    assert row['recorded_fees_rial'] == pytest.approx(result.executions.fee_rial.sum())
    assert row['ending_cash_rial'] == pytest.approx(row['initial_capital_rial'] - row['buy_notional_rial'] + row['sell_notional_rial'] - row['recorded_fees_rial'])
    assert row['fixed_fills_fee_addback_pnl_rial'] == pytest.approx(row['net_pnl_rial'] + row['recorded_fees_rial'])
    assert not row['fee_addback_is_counterfactual_backtest'] and not row['accepted_for_signals']
    assert len(source['results']) == 1


@pytest.mark.parametrize('kind', ['fees', 'cash', 'realized', 'count', 'return', 'invalid_status', 'unknown_numeric'])
def test_inconsistent_ledger_is_rejected(capacity_source, kind):
    result = next(iter(capacity_source[-1].values()))
    metrics, equity = dict(result.metrics), result.equity.copy()
    if kind == 'fees': metrics['fees_rial'] += 100
    if kind == 'cash': equity.loc[equity.index[-1], 'cash_rial'] += 100
    if kind == 'realized': metrics['realized_net_rial'] += 100
    if kind == 'count': metrics['executions'] += 1
    if kind == 'return': metrics['total_return'] += .1
    if kind == 'invalid_status': metrics['status'] = 'invented'
    if kind == 'unknown_numeric': metrics.update(status='invalid_data')
    with pytest.raises(ValueError):
        diagnose_cash_ledger(equity, result.executions, result.decisions, metrics, 2)


def test_unknown_fold_never_becomes_zero_return_or_complete_entry_rate(capacity_source):
    result = next(iter(capacity_source[-1].values()))
    metrics = dict(result.metrics, status='invalid_data', total_return=None, final_equity_rial=None)
    equity = result.equity.copy()
    equity.loc[equity.index[-1], 'equity_rial'] = float('nan')
    row = diagnose_cash_ledger(equity, result.executions, result.decisions, metrics, 2)
    assert row['net_pnl_rial'] is None and row['entry_fill_fraction'] is None and row['diagnosis'] == 'unknown'
    summary = summarize_cases([row])
    assert summary['unknown_folds'] == 1 and summary['mean_known_fold_return'] is None
    assert summary['all_fold_mean'] is None and summary['pooled_model_entry_fill_fraction'] is None


def test_capacity_run_registers_first_preserves_controls_and_replays_without_live_db(db, capacity_source, monkeypatch):
    key = capacity_source[0]
    events, original_log, original_replay = [], db.log_experiment, study.replay_portfolio
    def log(**kw):
        events.append(kw['experiment_id'])
        return original_log(**kw)
    def replay(*args, **kw):
        assert events and events[0].endswith('-PROTOCOL')
        return original_replay(*args, **kw)
    monkeypatch.setattr(db, 'log_experiment', log)
    monkeypatch.setattr(study, 'replay_portfolio', replay)
    result = study.run_capacity_study(db, key, multipliers=(1, 100))
    assert len(result['cases']) == 4 and len(events) == 2
    assert set(result['summary']) == {'x1', 'x100'}
    assert result['plan_sha256'] and not result['accepted_for_signals']
    mode = 'post_surge_pullback-1d'
    assert result['summary']['x100'][mode]['mean_recorded_fee_fraction'] < result['summary']['x1'][mode]['mean_recorded_fee_fraction']
    assert result['summary']['x100'][mode]['mean_known_fold_return'] > result['summary']['x1'][mode]['mean_known_fold_return']
    assert result['summary']['x100'][mode]['mean_known_noncash_equity_share'] < result['summary']['x1'][mode]['mean_known_noncash_equity_share']
    # سهم بیشتر نقد در این مثال زیان را کم می‌کند؛ به معنای مزیت انتخاب سهم نیست.
    db.close()
    reviewed = study.read_capacity_study(result['folder'], deep=True)
    assert reviewed['artifacts_verified'] and not reviewed['replay_recomputed']
    assert not reviewed['profitability_proven']


@pytest.mark.parametrize('kind', ['duplicate_case', 'summary', 'artifact', 'config'])
def test_rehashed_tampering_does_not_bypass_full_capacity_review(db, capacity_source, kind):
    result = study.run_capacity_study(db, capacity_source[0], multipliers=(1,))
    folder = study.Path(result['folder'])
    saved = study._load_json(folder / 'result.json')
    if kind == 'duplicate_case': saved['cases'][1] = saved['cases'][0]
    if kind == 'summary': saved['summary']['x1']['post_surge_pullback-1d']['known_folds'] = 999
    if kind == 'artifact':
        path = next((folder / 'cases').glob('*executions.parquet'))
        with path.open('ab') as file: file.write(b'changed')
    if kind == 'config': saved['cases'][0]['assumptions']['config']['initial_capital_rial'] *= 2
    study._write(folder / 'result.json', saved)
    study._write(folder / 'result-digest.json', dict(sha256=study.sha256(folder / 'result.json')))
    with pytest.raises((ValueError, AssertionError)):
        study.read_capacity_study(folder, deep=True)


def test_failed_registration_never_runs_replay(db, capacity_source, monkeypatch):
    monkeypatch.setattr(db, 'log_experiment', lambda **_: None)
    monkeypatch.setattr(study, 'replay_portfolio', lambda *_, **__: pytest.fail('Must register first'))
    with pytest.raises(RuntimeError, match='ثبت مطالعه'):
        study.run_capacity_study(db, capacity_source[0], multipliers=(1,))


def test_source_generation_mutation_prevents_publication(db, capacity_source, monkeypatch):
    original = study.replay_portfolio
    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        path = settings.DATA_DIR / 'research' / capacity_source[0] / 'prices.parquet'
        with path.open('ab') as file: file.write(b'changed')
        return result
    monkeypatch.setattr(study, 'replay_portfolio', mutate)
    with pytest.raises(ValueError, match='اثر انگشت'):
        study.run_capacity_study(db, capacity_source[0], multipliers=(1,))
    folder = next((settings.DATA_DIR / 'private/capital-capacity').glob('CAPACITY-*'))
    assert not (folder / 'result.json').exists()
    assert study._load_json(folder / 'status.json')['status'] == 'failed_or_interrupted'


def test_owner_size_reads_only_explicit_profile_capital(db, capacity_source, monkeypatch):
    class Profile:
        def __init__(self, db): pass
        def profile(self): return dict(capital_rial=250000., updated_at='2024-01-01T08:00:00+03:30')
    monkeypatch.setattr(study, 'WorkbenchRepository', Profile)
    result = study.run_capacity_study(db, capacity_source[0], multipliers=(1,), owner_capital=True)
    reviewed = study.read_capacity_study(result['folder'], deep=True)
    assert reviewed['plan']['owner_capital_read'] and not reviewed['plan']['current_holdings_and_available_cash_read']
    assert {c['metrics']['initial_capital_rial'] for c in reviewed['cases'] if c['scenario'] == 'owner'} == {250000.}


def test_capital_dashboard_empty_state_has_no_fabricated_results(dashboard_db):
    from streamlit.testing.v1 import AppTest
    app = AppTest.from_file(DASHBOARD, default_timeout=60).run()
    app.sidebar.radio[0].set_value('💸 هزینه و ظرفیت سرمایه').run()
    assert not app.exception and any('هنوز مطالعه' in message.value for message in app.info)


def test_sealed_price_dates_are_rejected_before_any_replay(db, capacity_source, monkeypatch):
    key, source, prices, _, _ = capacity_source
    folder = settings.DATA_DIR / 'research' / key
    prices.loc[prices.index[-1], 'date'] = source['protocol']['holdout_start']
    prices.to_parquet(folder / 'prices.parquet', index=False)
    source['protocol']['inputs']['prices'] = study.sha256(folder / 'prices.parquet')
    study._write(folder.with_suffix('.json'), source)
    monkeypatch.setattr(study, 'replay_portfolio', lambda *_, **__: pytest.fail('Sealed value reached replay'))
    with pytest.raises(ValueError, match='مهر و موم'):
        study.run_capacity_study(db, key, multipliers=(1,))


def test_source_base_mismatch_stops_all_scaled_cases(db, capacity_source, monkeypatch):
    original = study.replay_portfolio
    calls = []
    def replay(*args, **kwargs):
        calls.append(kwargs)
        result = original(*args, **kwargs)
        result.metrics['fees_rial'] += 100
        return result
    monkeypatch.setattr(study, 'replay_portfolio', replay)
    with pytest.raises(ValueError, match='آمار بازاجرا'):
        study.run_capacity_study(db, capacity_source[0], multipliers=(1, 100))
    assert len(calls) == 1


def test_changed_market_lock_setting_cannot_be_mixed_with_original_choices(db, capacity_source, monkeypatch):
    monkeypatch.setitem(settings.BACKTEST_CONFIG, 'lock_min_move_pct', .019)
    with pytest.raises(ValueError, match='آستانه قفل'):
        study.run_capacity_study(db, capacity_source[0], multipliers=(1,))


def test_saved_capital_dashboard_displays_cash_and_unknown_denominators(db, capacity_source):
    from streamlit.testing.v1 import AppTest
    def render():
        from dashboard.capital_capacity import page_capital_capacity
        page_capital_capacity()
    study.run_capacity_study(db, capacity_source[0], multipliers=(1, 100))
    app = AppTest.from_function(render, default_timeout=60).run()
    assert not app.exception
    data = app.dataframe[0].value
    assert len(data) == 4 and set(data['برش معلوم/کل']) == {'1/1'}
    assert data['میانگین سهم غیرنقد برش معلوم (%)'].notna().all()
    assert any('سودآوری هنوز' in w.value for w in app.warning)


def test_real_flow_source_verification_and_all_modes_survive_capacity_pipeline(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research.flow_v4_run import run_flow_v4
    from research.run import run_research

    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    build_feature_store(db)
    baseline = run_research(db, random_trials=1)
    flow = run_flow_v4(db, source_id=baseline['experiment_id'], min_train_sessions=104, test_sessions=30, replications=1000)
    result = study.run_capacity_study(db, flow['experiment_id'], multipliers=(1, 10))
    reviewed = study.read_capacity_study(result['folder'], deep=True)
    assert len(reviewed['cases']) == len(flow['results']) * 34 * 2
    assert reviewed['source_base_replay_parity_verified']
    assert set(reviewed['summary']['x1']) == set(flow['summary'])
    assert reviewed['plan']['cutoff'] == settings.RESEARCH_CONFIG['holdout_start']
    assert not (settings.DATA_DIR / 'research/holdout-unsealed.json').exists()
