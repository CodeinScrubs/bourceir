"""Hypothetical accounting inputs; no live data or broker activity."""

from decimal import Decimal

import pytest

from core.rights_mark import hypothetical_rights_mark


def spec():
    return dict(symbol='الف', rights_symbol='الفح', budget_rial=10000,
                purchase_date='2024-01-01', purchase_price_rial=100, purchase_fee_rate='0.01',
                old_shares=4, new_rights=3, valuation_date='2024-02-01',
                share_quote_date='2024-02-01', share_quote_rial=80,
                rights_quote_date='2024-02-01', rights_quote_rial=30,
                assumed_sale_fee_rate='0.02')


def test_budget_rounding_rights_and_assumed_liquidation_are_separate():
    result = hypothetical_rights_mark(spec())
    assert result['shares'] == 99 and result['assumed_rights'] == 74
    assert Decimal(result['purchase_cost_rial']) == Decimal('9999')
    assert Decimal(result['residual_cash_rial']) == Decimal('1')
    assert Decimal(result['total_mark_rial']) == Decimal('10141')
    assert Decimal(result['assumed_liquidation_rial']) == Decimal('9938.2')
    assert Decimal(result['marked_profit_rial']) == Decimal('141')
    assert not result['entitlement_verified'] and not result['execution_verified']
    assert not result['realized_profit'] and not result['signals_enabled']


@pytest.mark.parametrize('field,value', [
    ('share_quote_date', '2024-01-31'), ('rights_quote_date', '2024-01-31'),
    ('purchase_date', '2024-02-02'), ('valuation_date', '2024-2-1'),
    ('share_quote_rial', 0), ('rights_quote_rial', None), ('old_shares', 0),
    ('new_rights', True), ('budget_rial', 0), ('purchase_fee_rate', 'NaN'),
    ('purchase_fee_rate', -1), ('purchase_fee_rate', 0.1), ('assumed_sale_fee_rate', '1'),
    ('rights_symbol', 'الف'),
])
def test_invalid_or_incompatible_inputs_do_not_create_a_result(field, value):
    inputs = spec()
    inputs[field] = value
    with pytest.raises(ValueError):
        hypothetical_rights_mark(inputs)


def test_missing_sale_fee_never_implies_zero_and_insufficient_cash_fails():
    inputs = spec()
    inputs['assumed_sale_fee_rate'] = None
    assert hypothetical_rights_mark(inputs)['assumed_liquidation_rial'] is None
    inputs['budget_rial'] = 100
    with pytest.raises(ValueError, match='insufficient'):
        hypothetical_rights_mark(inputs)
