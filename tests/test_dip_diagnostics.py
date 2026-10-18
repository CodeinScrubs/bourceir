"""آشتی زیان حساب، عدم حذف خروج قفل و تمایز شاهد از اجرا."""

import copy

import pandas as pd
import pytest

from core.dip_diagnostics import diagnose_dip_fold, witness_summary
from tests.test_dip_portfolio import dip_history, replay
from tests.test_portfolio import picks


def diagnose(prices, calendar, result):
    return diagnose_dip_fold(prices, list(calendar), result.executions, result.decisions,
                             picks(calendar[:1], tuple(prices.symbol.unique())), result.metrics)


def test_postmortem_reconciles_net_pnl_without_subtracting_fees_twice():
    prices, calendar = dip_history()
    result = replay(prices, calendar, max_positions=1)
    output = diagnose(prices, calendar, result)
    assert output['filled_entries'] == 1
    assert output['realized_net_rial'] == pytest.approx(result.metrics['realized_net_rial'])
    assert output['total_fold_proxy_pnl_rial'] == pytest.approx(
        output['realized_net_rial'] + output['nonrealized_proxy_residual_rial'])
    assert output['causal_distribution_claim_verified'] is False


def test_sell_queue_rolls_to_actual_exit_instead_of_dropping_loss():
    prices, calendar = dip_history()
    prices.loc[2, ['low_raw', 'high_raw', 'open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] = [
        97, 97, 97, 97, 97, 100, 9700000]
    prices.loc[3, 'ref_price_raw'] = 97
    result = replay(prices, calendar, max_positions=1)
    output = diagnose(prices, calendar, result)
    assert output['delayed_sell_executions'] == 1
    assert output['exits'][0]['exit_kind'] == 'time_late'
    assert output['exits'][0]['locked_down_on_planned_exit'] is True
    assert output['realized_net_rial'] == pytest.approx(result.metrics['realized_net_rial'])


def test_unknown_total_fold_return_stays_unknown_despite_known_partial_realized_cash():
    prices, calendar = dip_history()
    result = replay(prices, calendar)
    result.metrics = copy.deepcopy(result.metrics)
    result.metrics['total_return'] = None
    output = diagnose(prices, calendar, result)
    assert output['total_fold_proxy_pnl_rial'] is None and output['nonrealized_proxy_residual_rial'] is None


def test_empty_execution_ledger_is_known_no_fill_not_missing_outcome():
    prices, calendar = dip_history()
    prices.loc[1, 'low_raw'] = 99
    result = replay(prices, calendar)
    output = diagnose(prices, calendar, result)
    assert output['filled_entries'] == 0 and output['realized_net_rial'] == 0
    assert output['selected_opportunities'] == 1


def test_partial_or_uninspected_path_never_inflates_complete_witness_fraction():
    report = witness_summary([dict(complete=True, event_confirmed=True),
                              dict(complete=False, event_confirmed=True),
                              dict(complete=True, event_confirmed=False)], all_possible_days=10)
    assert report['confirmed_complete_witnesses'] == 1 and report['unknown_days'] == 8
    assert report['confirmed_fraction_of_all'] == .1 and report['possible_fraction_upper'] == .9
    assert not report['own_order_fill_verified'] and not report['confidence_interval']


def test_mismatched_cash_ledger_fails_closed():
    prices, calendar = dip_history()
    result = replay(prices, calendar)
    result.metrics['realized_net_rial'] += 1
    with pytest.raises(ValueError, match='آشتی'):
        diagnose(prices, calendar, result)


def test_known_sales_are_not_silently_inferred_from_no_buy():
    prices, calendar = dip_history()
    result = replay(prices, calendar)
    with pytest.raises(ValueError, match='بدون خرید'):
        diagnose_dip_fold(prices, list(calendar), result.executions.loc[result.executions.side == 'sell'],
                          pd.DataFrame(), pd.DataFrame(), result.metrics)
