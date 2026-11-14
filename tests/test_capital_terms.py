"""Synthetic arithmetic only; market-source evidence is not a unit test fixture."""

from copy import deepcopy
from fractions import Fraction

import pytest

from research.capital_terms import capital_claim_terms


def fraction(ratio):
    return Fraction(ratio['numerator'], ratio['denominator'])


def test_exact_amounts_preserve_bonus_rejected_by_rounded_percentage_matching():
    result = capital_claim_terms(capital_before_rial=7_000, capital_after_rial=20_000,
                                 retained_earnings_rial=13_000)
    assert fraction(result['capital_multiplier']) == Fraction(20, 7)
    assert abs(float(fraction(result['capital_multiplier'])) - 2.86) > .00011
    assert fraction(result['conditional_total_bonus_ratio']) == Fraction(13, 7)
    assert fraction(result['conditional_rights_ratio']) == 0
    assert result['funding_fields_rial']['preemptive_cash_and_claims_rial'] is None
    assert result['stated_funding_exhausts_increase']
    assert not result['shares_credited_verified'] and not result['tradability_verified']


def test_mixed_funding_never_turns_paid_rights_into_free_shares():
    result = capital_claim_terms(capital_before_rial=4_700, capital_after_rial=7_000,
                                 retained_earnings_rial=900, preemptive_cash_and_claims_rial=1_400)
    assert fraction(result['conditional_total_bonus_ratio']) == Fraction(9, 47)
    assert fraction(result['conditional_rights_ratio']) == Fraction(14, 47)


def test_waived_preemption_grants_no_rights_to_existing_holders():
    result = capital_claim_terms(capital_before_rial=100, capital_after_rial=140,
                                 waived_preemption_cash_rial=40)
    assert fraction(result['conditional_total_bonus_ratio']) == 0
    assert fraction(result['conditional_rights_ratio']) == 0


def test_partial_funding_keeps_unknown_total_and_known_component():
    result = capital_claim_terms(capital_before_rial=100, capital_after_rial=140,
                                 retained_earnings_rial=10)
    assert result['unassigned_after_stated_funding_rial'] == 30
    assert fraction(result['supported_bonus_ratio']) == Fraction(1, 10)
    assert result['conditional_total_bonus_ratio'] is None
    assert result['conditional_rights_ratio'] is None


def test_no_funding_fields_do_not_become_zero_claims():
    result = capital_claim_terms(capital_before_rial=100, capital_after_rial=140)
    assert result['unassigned_after_stated_funding_rial'] is None
    assert result['supported_bonus_ratio'] is None
    assert result['conditional_total_bonus_ratio'] is None
    assert result['conditional_rights_ratio'] is None


def test_partial_cash_funding_does_not_impute_zero_bonus():
    result = capital_claim_terms(capital_before_rial=100, capital_after_rial=140,
                                 preemptive_cash_and_claims_rial=20)
    assert result['supported_bonus_ratio'] is None
    assert result['conditional_total_bonus_ratio'] is None
    assert fraction(result['conditional_rights_ratio']) == Fraction(1, 5)


def test_stated_zero_is_preserved_without_implying_complete_funding():
    result = capital_claim_terms(capital_before_rial=100, capital_after_rial=140,
                                 retained_earnings_rial=0, revaluation_surplus_rial=0)
    assert fraction(result['supported_bonus_ratio']) == 0
    assert result['conditional_total_bonus_ratio'] is None
    assert result['conditional_rights_ratio'] is None
    assert not result['stated_funding_exhausts_increase']


def test_inputs_are_not_mutated():
    values = dict(capital_before_rial=100, capital_after_rial=140, revaluation_surplus_rial=40)
    before = deepcopy(values)
    result = capital_claim_terms(**values)
    assert values == before and fraction(result['conditional_total_bonus_ratio']) == Fraction(2, 5)


@pytest.mark.parametrize('value', [True, 1.0, '100', -1, 2**63])
@pytest.mark.parametrize('field', ['capital_before_rial', 'retained_earnings_rial'])
def test_inexact_or_invalid_integer_funding_is_rejected(value, field):
    values = dict(capital_before_rial=100, capital_after_rial=140, retained_earnings_rial=40)
    values[field] = value
    with pytest.raises(ValueError):
        capital_claim_terms(**values)


@pytest.mark.parametrize('before,after,funding', [(0, 10, 10), (100, 100, 0), (100, 99, 0), (100, 140, 41)])
def test_nonincreasing_or_overfunded_capital_is_rejected(before, after, funding):
    with pytest.raises(ValueError):
        capital_claim_terms(capital_before_rial=before, capital_after_rial=after,
                            retained_earnings_rial=funding)
