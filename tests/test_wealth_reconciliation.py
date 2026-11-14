"""Synthetic table fixtures only; no broker/account or real-market inputs."""

from copy import deepcopy

import pytest

from research.wealth_reconciliation import review_wealth_rows


def row(**changes):
    result = dict(symbol='آزمون', initial_toman='100', subscriptions_toman='20',
        dps_received_toman='5', dps_receivable_toman='3', rights_sale_proceeds_toman='2',
        buy_fee_toman='1', sell_fee_toman='1', final_holdings_toman='110',
        final_net_toman='99', total_contributed_toman='120', shares_bought='10',
        ret_net_pct='-1', ret_on_contributed_pct='-0.83', basis='TOTAL')
    result.update(changes)
    return result


def review(rows=None, **kwargs):
    return review_wealth_rows([row()] if rows is None else rows,
        rights_policy=kwargs.pop('rights_policy', 'sell_rights_no_external_cash'), **kwargs)


def test_reconciles_funding_and_face_receivable_without_certifying_profit():
    source = [row()]
    before = deepcopy(source)
    result = review(source, expected_initial_rial=1000)
    assert source == before
    assert result['computed_end_assets_rial'] == 1190
    assert result['computed_value_net_subscriptions_rial'] == 990
    assert result['total_contributed_rial'] == 1200
    assert result['end_assets_excluding_reported_receivables_rial'] == 1160
    assert result['numeric_result_reconciled']
    assert not result['owner_funding_policy_matches'] and result['policy_conflict_rows'] == 1
    assert result['computed_model_return'] == pytest.approx(-.01)
    assert result['return_on_total_contributed'] == pytest.approx(119/120-1)
    assert result['economic_return'] is None and not result['actual_fills_verified']
    assert result['rows'][0]['economic_return'] is None


def test_funding_permission_changes_policy_assessment_never_evidence_status():
    result = review(rights_policy='exercise_with_external_cash')
    assert result['owner_funding_policy_matches'] and result['economic_return'] is None
    assert not result['accepted_for_signals']


def test_fractional_quantity_and_imported_total_label_do_not_certify_execution():
    result = review([row(shares_bought='10.125')])
    assert result['fractional_purchase_rows'] == 1
    assert 'external_total_label_is_not_independent_verification' in result['rows'][0]['issues']


def test_weighted_portfolio_return_is_not_the_mean_of_unequal_sleeve_returns():
    first = row(subscriptions_toman=0, final_net_toman=119, total_contributed_toman=100,
        ret_net_pct=19, ret_on_contributed_pct=19)
    second = row(symbol='دوم', initial_toman=200, subscriptions_toman=0,
        final_net_toman=119, total_contributed_toman=200, ret_net_pct=-40.5, ret_on_contributed_pct=-40.5)
    result = review([first, second])
    assert result['numeric_result_reconciled']
    assert result['computed_model_return'] == pytest.approx(238/300-1)
    assert result['computed_model_return'] != pytest.approx((.19-.405)/2)


@pytest.mark.parametrize('column', ['final_net_toman', 'total_contributed_toman', 'ret_net_pct', 'ret_on_contributed_pct'])
def test_stale_or_inconsistent_columns_are_not_silently_recomputed_as_valid(column):
    result = review([row(**{column: '999'})])
    assert result['arithmetic_error_rows'] == 1 and not result['numeric_result_reconciled']


@pytest.mark.parametrize('value', [None, True, float('nan'), float('inf'), 'NaN', 'invalid', '1e100000'])
def test_unknown_and_boolean_money_never_becomes_zero(value):
    with pytest.raises(ValueError):
        review([row(dps_received_toman=value)])


@pytest.mark.parametrize('value', [None, True, -1, 'NaN'])
def test_invalid_shares_never_enter_the_total(value):
    with pytest.raises(ValueError):
        review([row(shares_bought=value)])


def test_empty_table_duplicate_normalized_symbol_wrong_capital_and_missing_column_fail_closed():
    with pytest.raises(ValueError):
        review([])
    with pytest.raises(ValueError):
        review([row(symbol='كيان'), row(symbol='کیان')])
    with pytest.raises(ValueError):
        review(expected_initial_rial=999)
    broken = row()
    del broken['dps_received_toman']
    with pytest.raises(ValueError):
        review([broken])
    with pytest.raises(ValueError):
        review(rights_policy='assume_free_shares')


def test_negative_net_value_is_a_possible_accounting_result_not_missing_data():
    result = review([row(subscriptions_toman=200, final_net_toman=-81, total_contributed_toman=300,
        ret_net_pct=-181, ret_on_contributed_pct='-60.33')])
    assert result['numeric_result_reconciled']
    assert result['computed_model_return'] == pytest.approx(-1.81)


def test_display_rounding_residual_has_explicit_small_tolerance():
    result = review([row(final_net_toman='99.03', ret_net_pct='-1')])
    assert result['numeric_result_reconciled']
    result = review([row(final_net_toman='99.06')])
    assert not result['numeric_result_reconciled']


def test_convert_to_rial_before_enforcing_the_money_bound():
    with pytest.raises(ValueError, match='Rial'):
        review([row(initial_toman=str(2**62))])
