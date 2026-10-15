"""Shared cash, no-lookahead replay, liquidity and chronological selection contracts."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from research.portfolio_run import select_from_past
from research.walkforward import TimeFold, purged_training, time_folds


def history(n=12, symbols=('a', 'b', 'c')):
    dates = pd.date_range('2024-01-01', periods=n).strftime('%Y-%m-%d').to_numpy()
    frames = [pd.DataFrame(dict(symbol=symbol, date=dates, low_raw=99., high_raw=101., open_raw=100.,
                                close_raw=100., close_final_raw=100., ref_price_raw=100., volume=1000., value=100000.))
              for symbol in symbols]
    return pd.concat(frames, ignore_index=True), dates


def picks(calendar, symbols=('a', 'b', 'c')):
    return pd.DataFrame([(day, symbol, rank) for day in calendar for rank, symbol in enumerate(symbols)],
                        columns=['date', 'symbol', 'rank'])


def config(**changes):
    return replace(PortfolioConfig(initial_capital_rial=10000, horizon=1, max_positions=2), **changes)


def test_shared_cash_and_slots_prevent_overallocation():
    prices, calendar = history()
    result = replay_portfolio(prices, picks(calendar), calendar, config())
    assert result.equity.cash_rial.ge(-1e-6).all() and result.equity.positions.le(2).all()
    assert result.executions.iloc[0]['date'] == calendar[1]
    assert result.equity.iloc[0]['equity_rial'] == 10000
    for (_, symbol), events in result.executions.groupby(['date', 'symbol']):
        assert events.quantity.sum() <= 50 and (events.quantity * events.price_rial).sum() <= 5000
    assert result.metrics['total_return'] < 0 and result.metrics['fees_rial'] > 0


def test_cash_from_sales_cannot_fund_buys_on_same_day():
    prices, calendar = history(5, ('a', 'b'))
    selected = pd.DataFrame([(calendar[0], 'a', 0), (calendar[1], 'b', 0)], columns=['date', 'symbol', 'rank'])
    result = replay_portfolio(prices, selected, calendar, config(max_positions=1))
    assert set(result.executions.symbol) == {'a'}
    assert 'slots_full' in set(result.decisions.status)


def test_exit_lock_and_partial_fills_keep_capital_tied_up():
    prices, calendar = history(6, ('a',))
    prices.loc[2, ['low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] = [97, 97, 97, 97, 100, 97000]
    prices.loc[3, ['low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value']] = [96, 98, 97, 97, 97, 100, 9700]
    prices.loc[4, 'ref_price_raw'] = 97
    selected = picks(calendar[:1], ('a',))
    result = replay_portfolio(prices, selected, calendar, config(max_positions=1))
    assert not ((result.executions.side == 'sell') & (result.executions.date == calendar[2])).any()
    sold = result.executions.loc[result.executions.side == 'sell']
    assert sold.iloc[0].quantity <= 5 and len(sold) >= 2
    assert result.equity.loc[result.equity.date == calendar[3], 'positions'].iloc[0] == 1


def test_future_prices_and_selections_do_not_change_past_fills():
    prices, calendar = history()
    selected = picks(calendar)
    full = replay_portfolio(prices, selected, calendar, config())
    cut = calendar[6]
    short = replay_portfolio(prices.loc[prices.date <= cut], selected.loc[selected.date <= cut], calendar[:7], config())
    pd.testing.assert_frame_equal(full.equity.loc[full.equity.date <= cut].reset_index(drop=True), short.equity)
    pd.testing.assert_frame_equal(full.executions.loc[full.executions.date <= cut].reset_index(drop=True), short.executions)


def test_synthetic_action_preserves_value_and_does_not_hide_assumption():
    prices, calendar = history(5, ('a',))
    mask = prices.date >= calendar[2]
    for col in ('low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw'):
        prices.loc[mask, col] *= .5
    prices.loc[mask, 'volume'] *= 2
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config(max_positions=1))
    assert result.metrics['corporate_action_events'] == 1
    assert -.03 < result.metrics['total_return'] < 0
    assert result.assumptions['corporate_action_model'] == 'synthetic_whole_shares_plus_noncash_fraction'


def test_unknown_reference_on_open_holding_invalidates_financial_return():
    prices, calendar = history(5, ('a',))
    prices.loc[2, 'ref_price_raw'] = np.nan
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config())
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.equity.iloc[-1].equity_rial != result.equity.iloc[-1].equity_rial


def test_unknown_entry_execution_cannot_be_credited_as_idle_cash():
    prices, calendar = history(5, ('a',))
    prices.loc[1, 'value'] = np.nan
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config())
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert not result.executions.shape[0]


def test_unknown_vwap_during_held_position_does_not_invent_delayed_fill():
    prices, calendar = history(5, ('a',))
    prices.loc[2, 'value'] = np.nan
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config())
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert len(result.executions) == 1


def test_mark_and_drawdown_include_starting_capital_and_loss_circuit():
    prices, calendar = history(6, ('a',))
    prices.loc[2:, ['low_raw', 'high_raw', 'close_raw', 'close_final_raw']] = [79, 81, 80, 80]
    prices.loc[2, 'value'] = 80000
    prices.loc[3:, ['ref_price_raw', 'value']] = [80, 80000]
    result = replay_portfolio(prices, picks(calendar, ('a',)), calendar, config(max_positions=1, horizon=4))
    assert result.metrics['stopped_new_entries']
    assert result.metrics['max_drawdown'] >= .10
    assert len(result.executions.loc[result.executions.side == 'buy']) == 1


def test_stress_fills_are_within_bounds_and_at_adverse_prices():
    prices, calendar = history(5, ('a',))
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config(stress=True))
    assert set(result.executions.loc[result.executions.side == 'buy', 'price_rial']) == {101}
    assert set(result.executions.loc[result.executions.side == 'sell', 'price_rial']) == {99}


def test_calendar_and_duplicate_input_rejected():
    prices, calendar = history()
    with pytest.raises(ValueError):
        replay_portfolio(prices, picks(calendar), calendar[::-1], config())
    with pytest.raises(ValueError):
        replay_portfolio(pd.concat([prices, prices.iloc[:1]]), picks(calendar), calendar, config())
    with pytest.raises(ValueError):
        config(participation=.051)


def test_walkforward_embargo_and_actual_exit_purge():
    _, calendar = history(20)
    fold = TimeFold(calendar[0], calendar[8], calendar[10], calendar[18], 2)
    panel = pd.DataFrame(dict(date=[calendar[5], calendar[6], calendar[7], calendar[9]],
        label_status_2d=['complete', 'complete', 'entry_blocked', 'complete'],
        exit_date_2d=[calendar[7], calendar[13], '', calendar[12]], entry_date=[calendar[6], calendar[7], calendar[8], calendar[10]]))
    assert list(purged_training(panel, fold, 2).date) == [calendar[5]]
    _, long = history(140)
    folds = time_folds(long, min_train_sessions=60, test_sessions=30, embargo_sessions=4, end_exclusive='2025-01-01')
    assert folds[0].train_end_exclusive == long[60] and folds[0].test_start == long[64]
    assert folds[0].test_end_exclusive <= folds[1].test_start


def test_selection_uses_past_only_and_preserves_unknown_candidate_ranks(monkeypatch):
    from research import portfolio_run
    from research.baselines import Strategy
    dates = pd.date_range('2023-01-01', periods=160).strftime('%Y-%m-%d')
    panel = pd.DataFrame(dict(date=dates, symbol='a', value=1000., score=1., entry_date=dates,
        label_status_2d='complete', exit_date_2d=dates, net_ret_2d=.01))
    fold = TimeFold(dates[0], dates[120], dates[125], dates[150], 5)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('fixed', 'fixed', lambda p: p.score)])
    selected, _ = select_from_past(panel, fold, (2,), 1)
    assert selected['strategy'] == 'fixed'
    panel.loc[panel.date >= dates[125], 'net_ret_2d'] = -100
    assert select_from_past(panel, fold, (2,), 1)[0] == selected
    panel.loc[:119, 'exit_date_2d'] = dates[145]  # no training label is known yet
    assert select_from_past(panel, fold, (2,), 1)[0] is None


def test_negative_past_returns_choose_idle_cash(monkeypatch):
    from research import portfolio_run
    from research.baselines import Strategy
    dates = pd.date_range('2023-01-01', periods=160).strftime('%Y-%m-%d')
    panel = pd.DataFrame(dict(date=dates, symbol='a', value=1000., score=1., entry_date=dates,
        label_status_2d='complete', exit_date_2d=dates, net_ret_2d=-.01))
    fold = TimeFold(dates[0], dates[120], dates[125], dates[150], 5)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('fixed', 'fixed', lambda p: p.score)])
    assert select_from_past(panel, fold, (2,), 1)[0] is None


def test_idle_cash_and_halted_mark_are_explicit():
    prices, calendar = history(5, ('a',))
    empty = pd.DataFrame(columns=['date', 'symbol', 'rank'])
    idle = replay_portfolio(prices, empty, calendar, config())
    assert idle.metrics['total_return'] == 0 and idle.metrics['max_drawdown'] == 0
    prices = prices.loc[prices.date != calendar[2]]
    held = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config(horizon=3))
    assert held.metrics['stale_mark_days'] == 1
    assert held.equity.loc[held.equity.date == calendar[2], 'positions'].iloc[0] == 1


@pytest.fixture
def pinned_baseline(db, tmp_path, monkeypatch):
    from features import store
    from research import labels as label_module
    from research import run
    from tests.test_features import synthetic_history
    for i, symbol in enumerate(('a', 'b', 'c')):
        frame, client = synthetic_history(seed=i)
        # Queue candidates must exist in the *test* folds, not only in training.
        for index in (112, 145):
            price = frame.loc[index, 'ref_price_raw'] * 1.03
            frame.loc[index, ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw']] = price
            frame.loc[index, 'value'] = frame.loc[index, 'volume'] * price
            frame.loc[index + 1, 'ref_price_raw'] = price
        sid = db.upsert_symbol(dict(symbol_fa=symbol, ins_code=str(i), instrument_type_code=300))
        db.upsert_daily_prices_df(sid, frame)
        db.upsert_client_types_df(sid, client)
    root = tmp_path / 'data'
    monkeypatch.setattr(settings, 'DATA_DIR', root)
    monkeypatch.setattr(store, 'FEATURES_ROOT', root / 'features')
    monkeypatch.setattr(run, 'RESEARCH_ROOT', root / 'research')
    monkeypatch.setattr(run, 'LABELS_ROOT', root / 'labels')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'dev_start', '2025-01-01')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2025-10-01')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'min_daily_value_rial', 0)
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [2, 3])
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'top_k', 2)
    monkeypatch.setattr(label_module, 'CALENDAR_MIN_SYMBOLS', 2)
    store.build_feature_store(db)
    source = run.run_research(db, random_trials=1)
    return source, root


@pytest.mark.parametrize('selector', ['buyer_power', 'buy_queue'])
def test_registered_walkforward_end_to_end_stays_development_only(db, pinned_baseline, selector):
    import json

    from research.portfolio_run import run_portfolio_research
    from research.run import _sha256
    source, root = pinned_baseline
    result = run_portfolio_research(db, min_train_sessions=104, test_sessions=30, dip_selector=selector)
    assert result['protocol']['source_id'] == source['experiment_id']
    assert not result['accepted_for_signals'] and not (root/'research'/'holdout-unsealed.json').exists()
    assert len(result['results']) == 2
    assert {'dip', 'dip_stress', 'dip_random', 'dip_vwap'} <= set(result['summary'])
    assert result['protocol']['dip']['rule']['dip'] == .02
    assert result['protocol']['dip']['selector'] == selector
    experiments = {e['experiment_id'] for e in db.get_experiments()}
    assert result['experiment_id'] in experiments and result['experiment_id']+'-PROTOCOL' in experiments
    folder = root/'research'/result['experiment_id']
    assert (folder/'code'/'backtest'/'portfolio.py').exists()
    observed_candidates = 0
    for picks_file in folder.glob('fold-*-dip-picks.parquet'):
        primary = pd.read_parquet(picks_file)
        observed_candidates += len(primary)
        control = pd.read_parquet(picks_file.with_name(picks_file.name.replace('-dip-picks', '-dip_vwap-picks')))
        random = pd.read_parquet(picks_file.with_name(picks_file.name.replace('-dip-picks', '-dip_random-picks')))
        pd.testing.assert_frame_equal(primary, control)
        pd.testing.assert_series_equal(primary.groupby('date').size(), random.groupby('date').size())
        if selector == 'buy_queue' and not primary.empty:
            source_features = pd.read_parquet(folder/'features.parquet')
            assert primary.merge(source_features, on=['date', 'symbol'])['locked_up'].eq(1).all()
    assert observed_candidates > 0
    for name, digest in result['artifacts_sha256'].items():
        assert _sha256(folder/name) == digest
    assert json.loads((root/'research'/'latest-portfolio.json').read_text())['experiment_id'] == result['experiment_id']
    assert json.loads((root/'research'/f'latest-portfolio-{selector}.json').read_text())['experiment_id'] == result['experiment_id']


def test_tampered_or_holdout_source_is_rejected(pinned_baseline):
    from research.portfolio_run import verified_baseline_inputs
    source, root = pinned_baseline
    folder = root/'research'/source['experiment_id']
    path = folder/'features.parquet'
    frame = pd.read_parquet(path)
    frame.loc[0, 'ret_1d'] = .99
    frame.to_parquet(path, index=False)
    with pytest.raises(ValueError, match='اثر انگشت'):
        verified_baseline_inputs(root/'research')
    with pytest.raises(ValueError, match='شناسه'):
        verified_baseline_inputs(root/'research', '../other')


def test_previous_price_contract_cannot_feed_a_new_portfolio_evaluation(pinned_baseline):
    import json

    from research.portfolio_run import verified_baseline_inputs
    source, root = pinned_baseline
    path = root/'research'/f"{source['experiment_id']}.json"
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['params']['research_provenance']['feature_manifest']['version'] = 'daily-v4'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='قرارداد'):
        verified_baseline_inputs(root/'research')
