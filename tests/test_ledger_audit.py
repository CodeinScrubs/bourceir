"""Adversarial ledger checks, including cash-consistent accounting corruption."""

import copy
from dataclasses import replace
from types import MappingProxyType

import numpy as np
import pytest

from backtest.evidence import audit_daily_replay
from backtest.portfolio import replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from research.dip import DipRule
from tests.test_portfolio import config, flat_publication, history, picks


def market(*, horizon=1):
    prices, days = history(7, ('12345',))
    selected = picks(days[:1], ('12345',))
    result = replay_portfolio(prices, selected, days, config(max_positions=1, horizon=horizon))
    return prices, days, selected, result


def test_independent_inventory_reconciles_cash_whole_shares_and_daily_nav():
    prices, days, selected, result = market()
    audit = audit_daily_replay(result, prices, selected, days)
    ledger = audit['inventory_audit']
    assert not audit['invariant_violations']
    assert ledger['reconstructed_nav_sessions'] == len(days)
    assert ledger['terminal_inventory_checked'] and ledger['independently_reconstructed_open_positions'] == 0
    assert len(ledger['ledger_sha256']) == 64 and not ledger['actual_entitlements_verified']


def test_cash_consistent_extra_sale_is_rejected_without_relying_on_engine_nav():
    prices, days, selected, result = market()
    sell = result.executions.index[result.executions.side.eq('sell')][0]
    event = result.executions.loc[sell].copy()
    result.executions.loc[sell, 'quantity'] += 1
    extra_fee = event.price_rial * result.assumptions['fees']['sell']
    result.executions.loc[sell, 'fee_rial'] += extra_fee
    result.metrics['fees_rial'] += extra_fee
    increment = event.price_rial - extra_fee
    mask = result.equity.date.ge(event.date)
    result.equity.loc[mask, ['cash_rial', 'equity_rial']] += increment
    result.metrics['total_return'] = result.equity.iloc[-1].equity_rial / result.metrics['initial_capital_rial'] - 1
    audit = audit_daily_replay(result, prices, selected, days)
    assert 'sale_exceeds_independent_inventory' in audit['invariant_violations']
    assert 'shared_cash_not_reconciled' not in audit['invariant_violations']
    assert audit['observed_path_model_return'] is None


@pytest.mark.parametrize('field', ['equity_rial', 'positions', 'noncash_receivable_rial'])
def test_invented_nav_or_inventory_is_not_accepted(field):
    prices, days, selected, result = market(horizon=20)
    result.equity.loc[2, field] += 1
    audit = audit_daily_replay(result, prices, selected, days)
    expected = dict(equity_rial='inventory_nav_not_reconciled', positions='independent_position_count_mismatch',
                    noncash_receivable_rial='fractional_nav_not_reconciled')[field]
    assert expected in audit['invariant_violations']
    assert audit['observed_path_model_return'] is None


def test_inside_daily_range_is_insufficient_for_the_declared_vwap_price():
    prices, days, selected, result = market(horizon=20)
    event = result.executions.iloc[0].copy()
    # Keep cash, fees and NAV coherent under the tampered price.
    result.executions.loc[0, 'price_rial'] += .1
    extra_fee = event.quantity * .1 * result.assumptions['fees']['buy']
    result.executions.loc[0, 'fee_rial'] += extra_fee
    result.metrics['fees_rial'] += extra_fee
    result.equity.loc[result.equity.date.ge(event.date), ['cash_rial', 'equity_rial']] -= event.quantity * .1 + extra_fee
    result.metrics['total_return'] = result.equity.iloc[-1].equity_rial / result.metrics['initial_capital_rial'] - 1
    audit = audit_daily_replay(result, prices, selected, days)
    assert 'execution_price_policy_mismatch' in audit['invariant_violations']
    assert 'execution_outside_observed_range' not in audit['invariant_violations']
    assert 'shared_cash_not_reconciled' not in audit['invariant_violations']


def test_successive_synthetic_actions_keep_fractional_units_separate_after_sale():
    prices, days, selected, _ = market()
    fields = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']
    prices.loc[prices.date.ge(days[2]), fields] *= .8
    prices.loc[prices.date.ge(days[4]), fields] *= .6
    result = replay_portfolio(prices, selected, days, config(max_positions=1))
    audit = audit_daily_replay(result, prices, selected, days)
    assert not audit['invariant_violations']
    assert result.metrics['open_positions'] == 0 and result.metrics['noncash_receivable_rial'] > 0
    assert 'synthetic_corporate_entitlements' in audit['blockers']
    assert audit['modeled_cash_return'] is None
    result.metrics['fractional_exposures'][0]['quantity'] += 1
    assert 'terminal_fractional_inventory_not_reconciled' in audit_daily_replay(
        result, prices, selected, days)['invariant_violations']


def test_no_trade_valuation_requires_publisher_evidence_outside_the_engines_own_log():
    prices, days, selected, _ = market(horizon=20)
    prices = prices.loc[prices.date.ne(days[2])]
    proof = flat_publication('12345', days[2])
    result = replay_portfolio(prices, selected, days, config(max_positions=1, horizon=20),
                              no_trade_valuation_observations=[proof])
    without = audit_daily_replay(result, prices, selected, days)
    assert 'independent_valuation_evidence_missing' in without['blockers']
    assert without['observed_path_model_return'] is None
    with_proof = audit_daily_replay(result, prices, selected, days, no_trade_valuation_observations=[proof])
    assert not with_proof['invariant_violations'] and with_proof['observed_path_model_return'] is not None
    bad = copy.deepcopy(proof)
    bad['body_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        audit_daily_replay(result, prices, selected, days, no_trade_valuation_observations=[bad])


def test_stale_counter_cannot_be_forged_to_certify_known_model_return():
    prices, days, selected, _ = market(horizon=20)
    prices = prices.loc[prices.date.ne(days[-1])]
    result = replay_portfolio(prices, selected, days, config(max_positions=1, horizon=20))
    result.equity['stale_positions'] = 0
    audit = audit_daily_replay(result, prices, selected, days)
    assert 'independent_stale_count_mismatch' in audit['invariant_violations']
    assert audit['inventory_audit']['independently_stale_sessions'] == 1
    assert audit['observed_path_model_return'] is None


def test_unknown_terminal_nav_does_not_certify_the_remaining_inventory():
    prices, days, selected, _ = market()
    prices = prices.loc[prices.date.ne(days[2])]
    result = replay_portfolio(prices, selected, days, config(max_positions=1))
    assert np.isnan(result.equity.iloc[-1].equity_rial)
    audit = audit_daily_replay(result, prices, selected, days)
    assert not audit['inventory_audit']['terminal_inventory_checked']
    assert audit['observed_path_model_return'] is None


@pytest.mark.parametrize('opening', [float('nan'), 200.])
def test_dip_auditor_rejects_unknown_or_outside_range_open_even_when_limit_was_crossed(opening):
    prices, days, selected, _ = market()
    prices[['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw']] = [98., 96., 102., 100., 100.]
    rule = DipRule(dip=.01, take_profit=.02, hold=1, conservative=True)
    result = replay_portfolio(prices, selected, days, config(max_positions=1), dip_rule=rule)
    assert not result.executions.empty
    prices.loc[prices.date.eq(days[1]), 'open_raw'] = opening
    audit = audit_daily_replay(result, prices, selected, days)
    assert 'execution_price_policy_mismatch' in audit['invariant_violations']
    assert audit['observed_path_model_return'] is None


def test_inventory_nav_uses_raw_official_close_even_if_prepared_entry_mark_is_wrong():
    prices, days, selected, _ = market(horizon=20)
    prepared = prepare_portfolio_prices(prices)
    sessions = dict(prepared.sessions)
    session = dict(sessions[days[1]])
    session['12345'] = MappingProxyType(dict(session['12345'], mark=110.))
    sessions[days[1]] = MappingProxyType(session)
    forged = replace(prepared, sessions=MappingProxyType(sessions))
    result = replay_portfolio(forged, selected, days, config(max_positions=1, horizon=20))
    audit = audit_daily_replay(result, forged, selected, days)
    assert 'inventory_nav_not_reconciled' in audit['invariant_violations']
    assert audit['observed_path_model_return'] is None
