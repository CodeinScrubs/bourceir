"""A known model mark is neither terminal cash nor evidence of actual execution."""

import pytest

from research.strategy_outcomes import outcome_status


def inputs():
    days = ['2024-01-01', '2024-01-02']
    item = dict(equity=[dict(date=d, equity_rial=110., cash_rial=10.) for d in days],
        metrics=dict(open_positions=1, noncash_receivable_rial=0.))
    audit = dict(observed_path_model_return=.10, modeled_cash_return=None,
        invariant_violations=[], blockers=['terminal_assets_not_cash'])
    return item, audit, days


def test_marked_nav_can_be_known_with_unsold_assets_without_becoming_cash():
    item, audit, days = inputs()
    row = outcome_status(item, audit, days)
    assert row['model_nav_known'] and not row['modeled_terminal_cash_known']
    assert row['model_nav_rial'] == 110 and row['modeled_terminal_cash_rial'] is None
    assert row['recorded_assets_context'] == 'planned_endpoint'
    assert not row['economic_accounting_verified'] and not row['actual_execution_verified']


def test_truncated_inventory_is_not_inventory_at_the_planned_endpoint():
    item, audit, days = inputs()
    item['equity'].pop()
    audit['observed_path_model_return'] = None
    row = outcome_status(item, audit, days)
    assert row['recorded_asof_date'] == days[0] and row['planned_endpoint_date'] == days[1]
    assert row['recorded_assets_context'] == 'engine_stop_before_planned_endpoint'
    assert row['model_nav_rial'] is None and row['economic_return'] is None


@pytest.mark.parametrize('defect', ['short_known_nav', 'cash_with_position', 'cash_without_nav', 'cash_with_claim'])
def test_inconsistent_claims_fail_closed(defect):
    item, audit, days = inputs()
    if defect == 'short_known_nav':
        item['equity'].pop()
    else:
        audit['modeled_cash_return'] = .1
        if defect == 'cash_without_nav':
            audit['observed_path_model_return'] = None
        if defect == 'cash_with_claim':
            item['metrics'].update(open_positions=0, noncash_receivable_rial=1.)
    with pytest.raises(ValueError):
        outcome_status(item, audit, days)


def test_even_known_modeled_cash_and_injected_flags_do_not_certify_economics():
    item, audit, days = inputs()
    item['metrics']['open_positions'] = 0
    item['equity'][-1]['cash_rial'] = 110.
    audit.update(modeled_cash_return=.10, blockers=[], economic_profit_verified=True, actual_fills_verified=True)
    row = outcome_status(item, audit, days)
    assert row['modeled_terminal_cash_known']
    assert not row['economic_accounting_verified'] and not row['actual_execution_verified']
