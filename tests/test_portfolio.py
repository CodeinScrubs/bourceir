"""Shared cash, no-lookahead replay, liquidity and chronological selection contracts."""

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from backtest.bonus_shares import BonusCapitalization, BonusRelease, BonusShareBook
from backtest.cash_dividends import CashDividend, DividendPayment
from backtest.corporate_actions import BonusDividendBook, DatedCashDividendBook
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from research.portfolio_run import select_from_past
from research.walkforward import TimeFold, purged_training, time_folds
from tests.test_daily_publications import observation


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


def test_dated_zero_activity_resolves_only_the_exact_entry_and_keeps_cash():
    code = '12345'
    prices, calendar = history(5, (code,))
    missing = prices.loc[prices.date.ne(calendar[1])]
    selections = picks(calendar[:2], (code,)).iloc[:1]
    native = dict(insCode=code, dEven=20240102, qTotTran5J=0, qTotCap=0, zTotTran=0)
    proof = observation(code=code, day=calendar[1], native=native) | dict(http_status=200)
    unknown = replay_portfolio(missing, selections, calendar, config())
    assert unknown.metrics['total_return'] is None
    verified = replay_portfolio(missing, selections, calendar, config(), no_trade_entry_observations=[proof])
    assert verified.metrics['total_return'] == 0 and verified.executions.empty
    assert verified.metrics['observed_no_trade_entries'] == 1
    assert verified.decisions.loc[verified.decisions.status.eq('observed_no_trade_entry'), 'evidence_sha256'].tolist() == [proof['body_sha256']]
    assert len(verified.equity) == len(calendar)

    conflict = replay_portfolio(prices, selections, calendar, config(), no_trade_entry_observations=[proof])
    assert conflict.metrics['total_return'] is None
    assert conflict.decisions.iloc[-1].status == 'conflicting_no_trade_entry'
    wrong_day = observation(code=code, day=calendar[2], native=native) | dict(http_status=200)
    with pytest.raises(ValueError, match='هویت یا تاریخ'):
        replay_portfolio(missing, selections, calendar, config(), no_trade_entry_observations=[wrong_day])


def flat_publication(code, day, *, mark=100, reference=100):
    native = dict(insCode=code, dEven=int(day.replace('-', '')), qTotTran5J=0, qTotCap=0, zTotTran=0,
                  pClosing=mark, priceYesterday=reference)
    stamp = '2024-02-01T14:00:00+03:30'
    return observation(code=code, day=day, native=native) | dict(http_status=200, requested_at=stamp, received_at=stamp)


def test_continuous_flat_marks_defer_exit_preserve_cash_and_apply_action_on_reopening():
    code = '12345'
    prices, calendar = history(6, (code,))
    missing = prices.loc[~prices.date.isin(calendar[2:4])].copy()
    after = missing.date.ge(calendar[4])
    fields = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']
    missing.loc[after, fields] *= .5
    missing.loc[after, 'volume'] *= 4
    missing.loc[after, 'value'] *= 2
    before = missing.copy(deep=True)
    selections = picks(calendar[:1], (code,))
    proofs = [flat_publication(code, day) for day in calendar[2:4]]
    assert replay_portfolio(missing, selections, calendar, config()).metrics['total_return'] is None

    result = replay_portfolio(missing, selections, calendar, config(), no_trade_valuation_observations=proofs)
    buy, sell = result.executions.to_dict('records')
    assert (buy['date'], sell['date'], sell['quantity']) == (calendar[1], calendar[4], 2 * buy['quantity'])
    assert result.metrics['status'] == 'complete' and result.metrics['corporate_action_events'] == 1
    assert result.metrics['observed_no_trade_marks'] == result.metrics['observed_no_trade_exit_delays'] == 2
    assert result.equity.iloc[1:4].cash_rial.nunique() == result.equity.iloc[1:4].equity_rial.nunique() == 1
    assert result.equity.iloc[1:4].stale_positions.eq(0).all()
    assert result.decisions.loc[result.decisions.status.eq('observed_no_trade_exit_delayed'), 'evidence_sha256'].tolist() == [
        proof['body_sha256'] for proof in proofs]
    pd.testing.assert_frame_equal(missing, before)
    economic = replay_portfolio(missing, selections, calendar, config(), no_trade_valuation_observations=proofs,
                                require_explicit_corporate_actions=True)
    assert economic.executions.side.tolist() == ['buy'] and economic.metrics['total_return'] is None
    assert economic.equity.iloc[-1].cash_rial == result.equity.iloc[1].cash_rial
    blocked = economic.decisions.iloc[-1]
    assert blocked.status == 'unaccounted_reference_change' and blocked.date == calendar[4]
    assert blocked.previous_official_rial == 100 and blocked.reference_rial == 50


@pytest.mark.parametrize('failure', ['gap_before_due', 'different_mark', 'raw_collision'])
def test_unproven_held_sessions_remain_unknown(failure):
    code = '12345'
    prices, calendar = history(6, (code,))
    missing = prices.loc[~prices.date.isin(calendar[2:4])]
    proofs = [flat_publication(code, calendar[3])]
    status = 'missing_held_mark'
    if failure == 'different_mark':
        proofs.append(flat_publication(code, calendar[2], mark=101, reference=101))
        status = 'discontinuous_no_trade_valuation'
    elif failure == 'raw_collision':
        missing = prices
        proofs.append(flat_publication(code, calendar[2]))
        status = 'conflicting_no_trade_valuation'
    result = replay_portfolio(missing, picks(calendar[:1], (code,)), calendar, config(horizon=2),
                              no_trade_valuation_observations=proofs)
    assert result.metrics['total_return'] is None and result.metrics['max_drawdown'] is None
    assert result.decisions.iloc[-1].status == status and result.decisions.iloc[-1].date == calendar[2]
    assert result.executions.side.tolist() == ['buy']


def test_flat_receipts_support_unfilled_entry_and_open_terminal_mark_separately():
    code = '12345'
    prices, calendar = history(4, (code,))
    proofs = [flat_publication(code, day) for day in calendar[2:]]
    missing = prices.loc[prices.date.lt(calendar[2])]
    entry = replay_portfolio(missing, picks(calendar[1:2], (code,)), calendar, config(),
                             no_trade_valuation_observations=proofs)
    assert entry.metrics['observed_no_trade_entries'] == 1 and entry.executions.empty
    assert entry.metrics['total_return'] == 0

    held = replay_portfolio(missing, picks(calendar[:1], (code,)), calendar, config(),
                            no_trade_valuation_observations=proofs)
    assert held.executions.side.tolist() == ['buy']
    assert held.metrics['open_positions'] == 1 and held.metrics['observed_no_trade_exit_delays'] == 2
    assert held.metrics['final_equity_rial'] > held.equity.iloc[-1].cash_rial
    assert held.assumptions['terminal_value_is_marked_nav']


def test_flat_mark_also_delays_a_loss_floor_sale():
    code, other = '12345', '67890'
    prices, calendar = history(7, (code, other))
    loss = prices.symbol.eq(other) & prices.date.ge(calendar[2])
    fields = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw']
    prices.loc[loss, fields + ['value']] *= .6
    prices.loc[loss & prices.date.gt(calendar[2]), 'ref_price_raw'] = 60
    gaps = prices.symbol.eq(code) & prices.date.isin(calendar[3:5])
    result = replay_portfolio(prices.loc[~gaps], picks(calendar[:1], (code, other)), calendar, config(horizon=5),
        loss_floor_exit=True, no_trade_valuation_observations=[flat_publication(code, day) for day in calendar[3:5]])
    assert result.metrics['risk_exit_request_date'] == calendar[2]
    assert result.metrics['observed_no_trade_exit_delays'] == 2
    sells = result.executions.loc[result.executions.side.eq('sell')]
    assert dict(zip(sells.symbol, sells.date)) == {other: calendar[3], code: calendar[5]}
    assert sells.exit_kind.eq('loss_floor').all() and result.metrics['risk_exit_fully_liquidated']


def test_flat_mark_preserves_non_cash_fraction_after_whole_shares_exit():
    code = '12345'
    prices, calendar = history(5, (code,))
    after = prices.date.ge(calendar[2])
    fields = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']
    prices.loc[after, fields] *= .8
    prices.loc[after, 'volume'] *= 2
    prices.loc[after, 'value'] *= 1.6
    result = replay_portfolio(prices.loc[prices.date.ne(calendar[3])], picks(calendar[:1], (code,)), calendar, config(),
        no_trade_valuation_observations=[flat_publication(code, calendar[3], mark=80, reference=80)])
    assert result.executions.side.tolist() == ['buy', 'sell'] and result.metrics['open_positions'] == 0
    assert result.metrics['observed_no_trade_marks'] == 1 and result.metrics['observed_no_trade_exit_delays'] == 0
    assert result.metrics['noncash_receivable_rial'] == pytest.approx(.25 * 80 * (1 - settings.FEES['sell']))
    assert result.equity.loc[result.equity.date.eq(calendar[3]), 'stale_fractional_exposures'].item() == 0


@pytest.mark.parametrize('combined', [False, True])
def test_cash_ex_on_zero_activity_day_is_booked_once_and_never_funds_a_trade(combined):
    code = '12345'
    prices, dates = history(7, (code,))
    cols = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']
    prices.loc[prices.date.ge(dates[4]), cols] *= .9
    missing = prices.loc[~prices.date.isin(dates[2:4])]
    proofs = [flat_publication(code, dates[2]), flat_publication(code, dates[3], mark=90, reference=90)]
    event = CashDividend('dps', code, dates[2], dates[3], '10', 'a' * 64)
    payment = DividendPayment('receipt', event.event_id, dates[5], '10', 'b' * 64)
    cash = DatedCashDividendBook([event], [payment], previous_price_sessions={event.event_id: dates[1]})
    books = {'bonus_shares': BonusDividendBook([], dividends=cash)} if combined else {'cash_dividends': cash}
    result = replay_portfolio(missing, picks(dates[:1], (code,)), dates, config(),
                              no_trade_valuation_observations=proofs, **books)
    buy, sell = result.executions.to_dict('records')
    assert (buy['date'], sell['date'], sell['quantity']) == (dates[1], dates[4], buy['quantity'])
    assert result.metrics['corporate_action_events'] == result.metrics['observed_no_trade_cash_ex_marks'] == 1
    assert result.metrics['observed_no_trade_exit_delays'] == 2
    assert result.equity.iloc[3].cash_rial == result.equity.iloc[2].cash_rial
    assert result.equity.iloc[3].cash_dividend_receivable_face_rial == buy['quantity'] * 10
    assert np.isnan(result.equity.iloc[3].equity_rial)
    assert result.equity.iloc[5].cash_rial - result.equity.iloc[4].cash_rial == pytest.approx(buy['quantity'] * 10)
    assert [row['kind'] for row in result.metrics['cash_dividend_ledger']] == ['receivable_created', 'cash_received']
    assert result.metrics['total_return'] is not None and result.metrics['max_drawdown'] is None


@pytest.mark.parametrize('bad_source', ['amount', 'previous_price_session'])
def test_cash_ex_with_nonmatching_source_leaves_claim_and_sale_unknown(bad_source):
    code = '12345'
    prices, dates = history(6, (code,))
    event = CashDividend('dps', code, dates[2], dates[3], '11' if bad_source == 'amount' else '10', 'a' * 64)
    predecessor = dates[0] if bad_source == 'previous_price_session' else dates[1]
    cash = DatedCashDividendBook([event], previous_price_sessions={event.event_id: predecessor})
    missing = prices.loc[~prices.date.isin(dates[2:4])]
    proofs = [flat_publication(code, dates[2]), flat_publication(code, dates[3], mark=90, reference=90)]
    result = replay_portfolio(missing, picks(dates[:1], (code,)), dates, config(),
                              no_trade_valuation_observations=proofs, cash_dividends=cash)
    assert result.metrics['status'] == 'invalid_data' and result.metrics['total_return'] is None
    assert result.executions.side.tolist() == ['buy'] and not result.metrics['cash_dividend_claims']
    decision = result.decisions.iloc[-1]
    assert decision.status == 'unknown_corporate_entitlement' and decision.date == dates[3]
    assert decision.evidence_sha256 == proofs[-1]['body_sha256']


def test_bonus_credits_on_nontrading_days_remain_separate_from_original_lot_and_cash():
    code = '12345'
    prices, dates = history(7, (code,))
    cols = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']
    prices.loc[prices.date.ge(dates[3]), cols] *= .5
    missing = prices.loc[~prices.date.isin(dates[[2, 4]])]
    proofs = [flat_publication(code, dates[2]), flat_publication(code, dates[4], mark=50, reference=50)]
    event = BonusCapitalization('bonus', code, dates[2], dates[3], 1, 1, dates[1],
                                '100', '50', 'a' * 64, '2024-02-01T14:00:00+03:30')
    release = BonusRelease('credit', event.event_id, dates[4], 1, 1, 'b' * 64, '2024-02-01T14:00:00+03:30')
    result = replay_portfolio(missing, picks(dates[:1], (code,)), dates, config(),
                              no_trade_valuation_observations=proofs, bonus_shares=BonusShareBook([event], [release]))
    buy, original_sale, bonus_sale = result.executions.to_dict('records')
    assert original_sale['date'] == dates[3] and bonus_sale['date'] == dates[5]
    assert original_sale['quantity'] == bonus_sale['quantity'] == buy['quantity']
    assert not original_sale['credit_disposal'] and bonus_sale['credit_disposal']
    assert result.equity.iloc[4].cash_rial == result.equity.iloc[3].cash_rial
    assert result.equity.iloc[4].stale_positions == 0
    assert result.metrics['observed_no_trade_marks'] == 2 and result.metrics['corporate_action_events'] == 1


def test_observed_mark_does_not_overwrite_a_conflicting_previous_raw_price():
    code = '12345'
    prices, dates = history(5, (code,))
    prepared = prepare_portfolio_prices(prices.loc[prices.date.ne(dates[2])])
    day = dates[3]
    incorrect = dict(prepared.sessions[day][code], previous_official=99)
    prepared = replace(prepared, sessions=dict(prepared.sessions) | {day: {code: incorrect}})
    result = replay_portfolio(prepared, picks(dates[:1], (code,)), dates, config(),
                              no_trade_valuation_observations=[flat_publication(code, dates[2])])
    assert result.metrics['total_return'] is None and result.executions.side.tolist() == ['buy']
    assert result.decisions.iloc[-1].status == 'conflicting_valuation_predecessor'


def test_flat_valuation_requires_native_flat_mark_full_day_and_development_window(monkeypatch):
    code = '12345'
    prices, calendar = history(5, (code,))
    selections = picks(calendar[:1], (code,))
    with pytest.raises(ValueError, match='شاهد روز کامل'):
        replay_portfolio(prices, selections, calendar, config(),
            no_trade_valuation_observations=[flat_publication(code, calendar[2], mark=101)])
    proof = flat_publication(code, calendar[2])
    same_day = proof | dict(requested_at='2024-01-03T14:00:00+03:30', received_at='2024-01-03T14:00:00+03:30')
    with pytest.raises(ValueError, match='شاهد روز کامل'):
        replay_portfolio(prices, selections, calendar, config(), no_trade_valuation_observations=[same_day])
    with pytest.raises(ValueError, match='حق‌تقدم'):
        replay_portfolio(prices, selections, calendar, config(), subscription_rights=object(),
                         no_trade_valuation_observations=[proof])
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', calendar[-1])
    with pytest.raises(ValueError, match='پیش از مهروموم'):
        replay_portfolio(prices, selections, calendar, config(), no_trade_valuation_observations=[proof])


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


@pytest.mark.parametrize('field',['ref_price_raw','close_final_raw'])
@pytest.mark.parametrize('whole_column',[True,False])
def test_boolean_numeric_values_never_become_prices_or_synthetic_share_multipliers(field,whole_column):
    prices,calendar=history(5,('a',))
    if whole_column:
        prices[field]=True
    else:
        prices[field]=prices[field].astype(object)
        prices.loc[2,field]=True
    result=replay_portfolio(prices,picks(calendar[:1],('a',)),calendar,config(max_positions=1))
    assert result.metrics['status']=='invalid_data'
    assert result.metrics['total_return'] is None


def test_unknown_selected_decision_does_not_turn_into_a_zero_return_cash_account():
    prices,calendar=history(5,('a',))
    prices.loc[0,'ref_price_raw']=np.nan
    result=replay_portfolio(prices,picks(calendar[:1],('a',)),calendar,config(max_positions=1))
    assert 'invalid_decision' in set(result.decisions.status)
    assert result.metrics['total_return'] is None and result.metrics['status']=='invalid_data'
    assert result.metrics['max_drawdown'] is None


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
    clock = pd.to_datetime(dates)
    panel = pd.DataFrame(dict(date=dates, symbol='a', value=1000., score=1.,
        entry_date=(clock + pd.Timedelta(days=1)).strftime('%Y-%m-%d'), entry_executable=True,
        label_status_2d='complete', exit_date_2d=(clock + pd.Timedelta(days=2)).strftime('%Y-%m-%d'), net_ret_2d=.01))
    fold = TimeFold(dates[0], dates[120], dates[125], dates[150], 5)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('fixed', 'fixed', lambda p: p.score)])
    selected, _ = select_from_past(panel, fold, (2,), 1, calendar=dates)
    assert selected['strategy'] == 'fixed'
    panel.loc[panel.date >= dates[125], 'net_ret_2d'] = -100
    assert select_from_past(panel, fold, (2,), 1, calendar=dates)[0] == selected
    panel.loc[:119, 'exit_date_2d'] = dates[145]  # no training label is known yet
    assert select_from_past(panel, fold, (2,), 1, calendar=dates)[0] is None


def test_negative_past_returns_choose_idle_cash(monkeypatch):
    from research import portfolio_run
    from research.baselines import Strategy
    dates = pd.date_range('2023-01-01', periods=160).strftime('%Y-%m-%d')
    clock = pd.to_datetime(dates)
    panel = pd.DataFrame(dict(date=dates, symbol='a', value=1000., score=1.,
        entry_date=(clock + pd.Timedelta(days=1)).strftime('%Y-%m-%d'), entry_executable=True,
        label_status_2d='complete', exit_date_2d=(clock + pd.Timedelta(days=2)).strftime('%Y-%m-%d'), net_ret_2d=-.01))
    fold = TimeFold(dates[0], dates[120], dates[125], dates[150], 5)
    monkeypatch.setattr(portfolio_run, 'STRATEGIES', [Strategy('fixed', 'fixed', lambda p: p.score)])
    assert select_from_past(panel, fold, (2,), 1, calendar=dates)[0] is None


def test_idle_cash_and_halted_mark_are_explicit():
    prices, calendar = history(5, ('a',))
    empty = pd.DataFrame(columns=['date', 'symbol', 'rank'])
    idle = replay_portfolio(prices, empty, calendar, config())
    assert idle.metrics['total_return'] == 0 and idle.metrics['max_drawdown'] == 0
    prices = prices.loc[prices.date != calendar[2]]
    held = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config(horizon=3))
    assert held.metrics['stale_mark_days'] == 1
    assert held.equity.loc[held.equity.date == calendar[2], 'positions'].iloc[0] == 1


def test_zero_observed_reference_changes_do_not_verify_real_corporate_entitlements():
    prices, calendar = history(5, ('a',))
    result = replay_portfolio(prices, picks(calendar[:1], ('a',)), calendar, config())
    assert not result.executions.empty
    assert result.metrics['corporate_action_events'] == 0
    assert result.metrics['actual_corporate_entitlements_verified'] is False
    assert result.assumptions['version'] == 'portfolio-v8'


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
    assert json.loads((root/'research'/'latest-portfolio.json').read_text(encoding='utf-8'))['experiment_id'] == result['experiment_id']
    assert json.loads((root/'research'/f'latest-portfolio-{selector}.json').read_text(encoding='utf-8'))['experiment_id'] == result['experiment_id']


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


def test_baseline_without_recovery_provenance_cannot_feed_new_research(pinned_baseline):
    import json

    from research.portfolio_run import verified_baseline_inputs
    source, root = pinned_baseline
    path = root / 'research' / f"{source['experiment_id']}.json"
    payload = json.loads(path.read_text(encoding='utf-8'))
    payload['params']['research_provenance']['feature_manifest']['provenance']['raw_inputs']['version'] = 'raw-inputs-v1'
    path.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='قرارداد'):
        verified_baseline_inputs(root / 'research')
