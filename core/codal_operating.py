"""Exact quantity/unit-price arithmetic from reconciled public monthly sales cells.

This does not certify issuer identity, historical availability, a formula's
meaning, economic truth, comparable product mix, feature eligibility or alpha.
"""

import json
from collections import Counter
from decimal import DecimalException
from fractions import Fraction

from core.codal_revenue import _decimal, _number, extract_monthly_revenue
from core.text import normalize_symbol

VERSION = 'codal-operating-metrics-v1'
BASE_UNITS = frozenset(('عدد', 'تیوپ', 'شیشه', 'آمپول', 'ویال', 'قرص', 'کپسول', 'بسته', 'قطعه',
    'دستگاه', 'بطری', 'جفت', 'تن', 'کیلوگرم', 'گرم', 'لیتر', 'متر', 'متر مربع', 'متر مکعب', 'ساعت'))
FALSE_FLAGS = ('formulas_executed', 'financial_facts_independently_verified', 'document_identity_verified',
    'historical_publication_verified', 'product_identity_across_periods_verified',
    'tariff_change_verified', 'product_mix_adjusted', 'source_rounding_uncertainty_quantified',
    'aggregate_quantity_computed', 'aggregate_rate_computed', 'accepted_for_features',
    'accepted_for_signals', 'profitability_proven')


def exact(value):
    """Reduced fraction: no invented precision or rounding in financial storage."""
    value = Fraction(value)
    return dict(numerator=str(value.numerator), denominator=str(value.denominator))


def unit_scale(label):
    """Only declared count prefixes; no tonne/kg or implicit packaging conversion."""
    if not isinstance(label, str):
        return None
    label = normalize_symbol(label)
    multiplier = 1
    for prefix, scale in (('هزار ', 1000), ('میلیون ', 1000000)):
        if label.startswith(prefix):
            multiplier, label = scale, label[len(prefix):]
            break
    if label not in BASE_UNITS:
        return None
    return dict(base_unit=label, multiplier_to_base=multiplier)


def _operating_row(row, cells):
    output = dict(row, status='not_applicable', reasons=[], quantity_is_native_primitive=False,
        reported_quantity=None, base_quantity=None, base_unit=None, quantity_scale=None,
        implied_rate_rial_per_reported_unit=None, implied_rate_rial_per_base_unit=None,
        cached_rate_rial_per_reported_unit=None, cached_minus_implied_rial_per_reported_unit=None,
        cached_rate_equals_implied=None, nearest_rounding_interval_contains_implied=None,
        publisher_rounding_rule_verified=False, **dict.fromkeys(FALSE_FLAGS, False))
    if row['section'] not in ('domestic', 'export', 'services'):
        output['reasons'] = ['signed_returns_or_discount_not_used_for_unit_price']
        return output
    quantity_cell = cells[row['quantity_address']]
    output.update(quantity_data_type=quantity_cell.get('data_type'),
        quantity_formula_present=quantity_cell.get('formula_present'),
        cached_rate_formula_present=cells[row['rate_address']].get('formula_present'))
    try:
        quantity = Fraction(_number(quantity_cell, primitive=True))
    except (ValueError, TypeError, KeyError, DecimalException):
        output.update(status='quantity_unavailable', reasons=['quantity_is_not_an_observed_numeric_primitive'])
        return output
    output.update(quantity_is_native_primitive=True, reported_quantity=exact(quantity))
    if quantity < 0:
        output.update(status='quantity_unavailable', reasons=['negative_sales_quantity'])
        return output
    scale = unit_scale(row['quantity_unit'])
    if scale is not None:
        multiplier = scale['multiplier_to_base']
        output.update(base_unit=scale['base_unit'], quantity_scale=multiplier,
            base_quantity=exact(quantity*multiplier))
    amount = Fraction(row['amount_rial'])
    if quantity == 0:
        output.update(status='undefined_zero_quantity',
            reasons=['zero_quantity_with_positive_revenue' if amount else 'zero_quantity_and_zero_revenue'])
        return output
    implied = amount/quantity
    output.update(status='computed' if scale is not None else 'reported_unit_only',
        implied_rate_rial_per_reported_unit=exact(implied))
    if scale is not None:
        output['implied_rate_rial_per_base_unit'] = exact(implied/output['quantity_scale'])
    else:
        output['reasons'].append('base_unit_scale_unknown')
    # This is a cached comparison value, never a primitive input or executed
    # publisher formula. Interval compatibility is mathematical, not attestation
    # of an issuer's rounding convention or invoice-level selling price.
    try:
        cached_decimal = _decimal(row['rate_raw_value'])
        cached = Fraction(cached_decimal)
        exponent = cached_decimal.as_tuple().exponent
        quantum = Fraction(10)**exponent
        difference = cached-implied
        output.update(cached_rate_rial_per_reported_unit=exact(cached),
            cached_minus_implied_rial_per_reported_unit=exact(difference),
            cached_rate_equals_implied=difference == 0,
            nearest_rounding_interval_contains_implied=abs(difference) <= quantum/2)
    except (ValueError, TypeError, DecimalException):
        output['reasons'].append('cached_rate_not_numeric')
    return output


def extract_operating_metrics(source_bytes, *, expected_sha256, available_at, observed_at, archived_at):
    """Data-only derivation. Undefined quantities/rates remain explicit in the row census."""
    revenue = extract_monthly_revenue(source_bytes, expected_sha256=expected_sha256,
        available_at=available_at, observed_at=observed_at, archived_at=archived_at)
    result = dict(version=VERSION, status='unavailable', source_sha256=revenue['source_sha256'],
        source_bytes_verified=revenue['source_bytes_verified'], source_available_at=revenue['available_at'],
        source_observed_at=None, source_archived_at=None, rows=None, row_statuses=None,
        total_revenue_rial=None, total_quantity=None, aggregate_unit_price=None,
        reasons=list(revenue['reasons']), **dict.fromkeys(FALSE_FLAGS, False))
    if revenue['status'] != 'reconciled':
        return result
    # The same immutable bytes already passed the strict decoder, byte limit,
    # unique-key and geometry checks in the revenue parser.
    data = json.loads(source_bytes.decode('utf-8'))
    table = next(t for t in data['tables'] if t.get('table_code') == revenue['table_code'])
    cells = {c['address']: c for c in table['cells']}
    rows = [_operating_row(row, cells) for row in revenue['rows']]
    result.update(status='observed', tracing_no=revenue['tracing_no'], table_code=revenue['table_code'],
        period_end_jalali=revenue['period_end_jalali'], period_end_iso=revenue['period_end_iso'],
        source_observed_at=revenue['observed_at'], source_archived_at=revenue['archived_at'],
        rows=rows, row_statuses=dict(sorted(Counter(row['status'] for row in rows).items())),
        revenue_sections=revenue['sections'], total_revenue_rial=revenue['total']['amount_rial'],
        structural_placeholders=revenue['structural_placeholders'],
        cached_revenue_totals_reconciled=True)
    return result
