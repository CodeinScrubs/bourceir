"""Exact funding ratios for a sourced capital event; never holder credits.

The caller links issuer/event/source explicitly. Rounded reported percentages
and price gaps are intentionally absent from this arithmetic interface.
"""

from fractions import Fraction

VERSION = 'exact-capital-claim-terms-v1'


def _integer(value, name, *, positive=False):
    if type(value) is not int or value < int(positive) or value > 2**63 - 1:
        raise ValueError(f'{name} must be an exact bounded integer in Rial')
    return value


def _ratio(value):
    return dict(numerator=value.numerator, denominator=value.denominator)


def capital_claim_terms(*, capital_before_rial, capital_after_rial,
                        retained_earnings_rial=None, revaluation_surplus_rial=None,
                        preemptive_cash_and_claims_rial=None, waived_preemption_cash_rial=None):
    """Separate conditional bonus claims and subscription rights by funding.

    Missing funding fields remain unknown. If stated components exactly exhaust
    the increase, the *aggregate* unassigned balance is zero, without claiming
    that each omitted source field was explicitly reported as zero. Waived
    preemption never grants rights or shares to existing holders here.
    """
    before = _integer(capital_before_rial, 'capital_before_rial', positive=True)
    after = _integer(capital_after_rial, 'capital_after_rial', positive=True)
    if after <= before:
        raise ValueError('Capital must increase')
    fields = dict(retained_earnings_rial=retained_earnings_rial,
                  revaluation_surplus_rial=revaluation_surplus_rial,
                  preemptive_cash_and_claims_rial=preemptive_cash_and_claims_rial,
                  waived_preemption_cash_rial=waived_preemption_cash_rial)
    stated = {name: _integer(value, name) for name, value in fields.items() if value is not None}
    increase = after - before
    known = sum(stated.values())
    if known > increase:
        raise ValueError('Stated funding exceeds the increase')
    unassigned = increase - known if stated else None
    complete = unassigned == 0
    bonus_fields = (retained_earnings_rial, revaluation_surplus_rial)
    known_bonus = sum(value for value in bonus_fields if value is not None)
    # An unassigned balance could include another free-share source (reserves,
    # premium, ...). Even explicitly zero retained/revaluation fields cannot
    # establish the *total* bonus quantity until funding is exhausted.
    bonus = Fraction(known_bonus, before) if complete else None
    rights = (Fraction(preemptive_cash_and_claims_rial or 0, before)
              if preemptive_cash_and_claims_rial is not None or complete else None)
    return dict(version=VERSION, capital_before_rial=before, capital_after_rial=after,
                capital_multiplier=_ratio(Fraction(after, before)),
                funding_fields_rial=fields, stated_funding_exhausts_increase=complete,
                unassigned_after_stated_funding_rial=unassigned,
                supported_bonus_ratio=(_ratio(Fraction(known_bonus, before))
                    if complete or any(value is not None for value in bonus_fields) else None),
                conditional_total_bonus_ratio=None if bonus is None else _ratio(bonus),
                conditional_rights_ratio=None if rights is None else _ratio(rights),
                existing_holder_entitlement_verified=False, shares_credited_verified=False,
                tradability_verified=False, feature_eligible=False, accepted_for_signals=False)
