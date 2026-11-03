"""Prepared prices preserve independent financial results and cannot leak mutable state."""

from dataclasses import FrozenInstanceError, replace

import pandas as pd
import pytest

from backtest.cash_dividends import CashDividend, CashDividendBook, DividendPayment
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from research.dip import DipRule
from tests.test_bonus_share_compatibility import FROZEN
from tests.test_portfolio import config, history, picks


@pytest.mark.parametrize('case',FROZEN['cases'],ids=lambda c:c['name'])
def test_prepared_and_raw_replays_have_identical_financial_frames_metrics_and_assumptions(case):
    from backtest.portfolio import PortfolioConfig

    prices,selected = pd.DataFrame(case['prices']),pd.DataFrame(case['selections'])
    prepared = prepare_portfolio_prices(prices)
    def run(source):
        kwargs = {}
        if case['cash_events']:
            kwargs['cash_dividends'] = CashDividendBook([CashDividend(**e) for e in case['cash_events']],
                [DividendPayment(**p) for p in case['cash_payments']])
        return replay_portfolio(source,selected,case['calendar'],PortfolioConfig(**case['config']),
            dip_rule=None if case['dip_rule'] is None else DipRule(**case['dip_rule']),**kwargs)
    raw,cached = run(prices),run(prepared)
    for kind in ('equity','executions','decisions'):
        pd.testing.assert_frame_equal(getattr(raw,kind),getattr(cached,kind),check_exact=True)
    assert raw.metrics==cached.metrics and raw.assumptions==cached.assumptions


def test_original_frame_and_each_cached_mapping_are_independent_and_immutable():
    prices,days = history(6,('a',))
    prepared = prepare_portfolio_prices(prices)
    original_mark = prepared.sessions[days[2]]['a']['mark']
    prices.loc[prices.date.eq(days[2]),'close_final_raw'] = 99999.
    assert prepared.sessions[days[2]]['a']['mark']==original_mark
    with pytest.raises(TypeError):
        prepared.sessions[days[2]] = {}
    with pytest.raises(TypeError):
        prepared.sessions[days[2]]['a'] = {}
    with pytest.raises(TypeError):
        prepared.sessions[days[2]]['a']['mark'] = 99999.
    with pytest.raises(FrozenInstanceError):
        prepared.row_count = 0


def test_frame_fingerprint_is_for_normalized_values_not_row_order_or_external_origin():
    prices,days = history()
    first = prepare_portfolio_prices(prices)
    shuffled = prepare_portfolio_prices(prices.sample(frac=1,random_state=17))
    assert first.frame_fingerprint==shuffled.frame_fingerprint and first.row_count==len(prices)
    changed = prices.copy()
    changed.loc[0,'volume'] += 1
    assert prepare_portfolio_prices(changed).frame_fingerprint!=first.frame_fingerprint


@pytest.mark.parametrize('field',['ref_price_raw','close_final_raw','volume','value'])
def test_boolean_numbers_never_turn_into_usable_one_rial_observations(field):
    prices,days = history(6,('a',))
    prices[field] = prices[field].astype(object)
    prices.loc[prices.date.eq(days[2]),field] = True
    prepared = prepare_portfolio_prices(prices)
    row = prepared.sessions[days[2]]['a']
    assert not row['valid']
    if field in ('ref_price_raw','close_final_raw'):
        assert not row['mark_valid']
    else:
        assert row['mark_valid']


def test_future_prices_and_future_cached_decisions_cannot_change_prior_financial_results():
    prices,days = history()
    selected = picks(days)
    full = replay_portfolio(prepare_portfolio_prices(prices),selected,days,config())
    end = days[6]
    short_prices = prices.loc[prices.date.le(end)]
    short = replay_portfolio(prepare_portfolio_prices(short_prices),selected.loc[selected.date.le(end)],days[:7],config())
    pd.testing.assert_frame_equal(full.equity.loc[full.equity.date.le(end)].reset_index(drop=True),short.equity,check_exact=True)
    pd.testing.assert_frame_equal(full.executions.loc[full.executions.date.le(end)].reset_index(drop=True),short.executions,check_exact=True)


def test_cache_reuse_in_a_different_scenario_does_not_mutate_prior_or_later_results():
    prices,days = history()
    selected = picks(days)
    prepared = prepare_portfolio_prices(prices)
    before = replay_portfolio(prepared,selected,days,config())
    replay_portfolio(prepared,selected,days,config(stress=True))
    after = replay_portfolio(prepared,selected,days,config())
    for kind in ('equity','executions','decisions'):
        pd.testing.assert_frame_equal(getattr(before,kind),getattr(after,kind),check_exact=True)
    assert before.metrics==after.metrics and before.assumptions==after.assumptions


def test_unknown_cache_contract_and_duplicate_source_keys_fail_closed():
    prices,days = history()
    prepared = prepare_portfolio_prices(prices)
    with pytest.raises(ValueError,match='Unsupported prepared'):
        replay_portfolio(replace(prepared,version='unknown'),picks(days),days,config())
    with pytest.raises(ValueError):
        prepare_portfolio_prices(pd.concat([prices,prices.iloc[:1]],ignore_index=True))
    with pytest.raises(ValueError):
        prepare_portfolio_prices(prices.rename(columns={'open_raw':'close_raw'}))


def test_prepared_data_without_open_cannot_be_used_for_a_dip_execution():
    prices,days = history()
    prepared = prepare_portfolio_prices(prices.drop(columns='open_raw'))
    with pytest.raises(ValueError):
        replay_portfolio(prepared,picks(days),days,config(horizon=2),dip_rule=DipRule(.01,.02,2))


def test_full_history_and_pruned_selected_history_have_identical_financial_results():
    from research.surge_portfolio import relevant_prices
    prices,days = history(30,('a','b','unselected'))
    prices.loc[prices.symbol.eq('unselected'),'value'] = float('nan')
    selected = picks(days[5:6],('a','b'))
    pruned = relevant_prices(prices,selected,start=days[5],end=days[22])
    original = replay_portfolio(pruned,selected,days,config(),start=days[5],end=days[22])
    cached = replay_portfolio(prepare_portfolio_prices(prices),selected,days,config(),
        start=days[5],end=days[22])
    for kind in ('equity','executions','decisions'):
        pd.testing.assert_frame_equal(getattr(original,kind),getattr(cached,kind),check_exact=True)
    assert original.metrics==cached.metrics and original.assumptions==cached.assumptions
