"""Compare reported broker charges with an explicit scenario, never infer a tariff."""

from collections import Counter
from decimal import Decimal, DecimalException, Inexact, localcontext
from math import isfinite

from core.timeutils import is_canonical_iso_date

VERSION = 'broker-fee-comparison-v1'


def _number(value, *, positive=False):
    if isinstance(value, (bool, str)) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('عدد مالی معتبر لازم است')
    result = Decimal(str(value))
    if not result.is_finite() or (result <= 0 if positive else result < 0):
        raise ValueError('عدد مالی متناهی و مثبت/نامنفی لازم است')
    return result


def compare_export_fees(records, rates, *, tolerance_rial=1, detail_limit=50):
    """Retain duplicates, unknown symbols and both signs of fee differences.

    Exact decimal *amounts* are serialized as strings. Weighted rates are approximate
    descriptions of this export, not proposed historical/current tariff rates.
    A one-rial tolerance is an explicit comparison policy, not proof of broker rounding.
    """
    if not isinstance(records, (list, tuple)):
        raise ValueError('فهرست صریح ردیف‌های خروجی لازم است')
    if not isinstance(rates, dict) or set(rates) != {'buy', 'sell'}:
        raise ValueError('نرخ صریح هر دو سمت معامله لازم است')
    rates = {side: _number(value) for side, value in rates.items()}
    if any(value >= 1 for value in rates.values()):
        raise ValueError('نرخ سناریو باید کسری کمتر از یک باشد')
    tolerance = _number(tolerance_rial)
    if type(detail_limit) is not int or not 0 <= detail_limit <= 1000:
        raise ValueError('سقف جزئیات باید صحیح و بین صفر و هزار باشد')
    totals = {side: dict(rows=0, notional=Decimal(0), reported=Decimal(0), scenario=Decimal(0),
                        absolute_difference=Decimal(0), max_absolute_difference=Decimal(0),
                        missing_symbol_rows=0, statuses=Counter()) for side in rates}
    details, dates, row_count, different_count = [], [], 0, 0
    # Refuse arithmetic that would round an amount into/out of a comparison status.
    with localcontext() as context:
        context.prec = 80
        context.traps[Inexact] = True
        try:
            for index, row in enumerate(records):
                if (not isinstance(row, dict) or row.get('fee_source') != 'broker_export'
                        or not is_canonical_iso_date(row.get('date')) or not isinstance(row.get('side'), str)
                        or row['side'] not in rates):
                    raise ValueError('ردیف تاریخ‌دار خروجی کارگزار با سمت و منشأ کارمزد لازم است')
                try:
                    quantity, price = (_number(row[key], positive=True) for key in ('quantity', 'price_rial'))
                    reported = _number(row['fee_rial'])
                except KeyError as exc:
                    raise ValueError('تعداد، قیمت و کارمزد گزارش باید موجود باشند') from exc
                if quantity != quantity.to_integral_value():
                    raise ValueError('تعداد گزارش باید صحیح باشد')
                side = row['side']
                notional = quantity * price
                scenario = notional * rates[side]
                difference = reported - scenario
                absolute = abs(difference)
                status = ('exact_scenario_match' if difference == 0 else 'within_declared_tolerance'
                          if absolute <= tolerance else 'different_from_scenario')
                group = totals[side]
                group['rows'] += 1
                group['notional'] += notional
                group['reported'] += reported
                group['scenario'] += scenario
                group['absolute_difference'] += absolute
                group['max_absolute_difference'] = max(group['max_absolute_difference'], absolute)
                group['missing_symbol_rows'] += not bool(row.get('raw_symbol'))
                group['statuses'][status] += 1
                dates.append(row['date'])
                row_count += 1
                if status == 'different_from_scenario':
                    different_count += 1
                    if len(details) < detail_limit:
                        details.append(dict(source_row_index=index, date=row['date'], side=side,
                            raw_symbol=row.get('raw_symbol'), notional_rial=str(notional),
                            reported_fee_rial=str(reported), scenario_fee_rial=str(scenario),
                            reported_minus_scenario_rial=str(difference)))
            sides = {}
            for side, group in totals.items():
                sides[side] = dict(row_count=group['rows'], status_counts=dict(group['statuses']),
                    missing_symbol_rows=group['missing_symbol_rows'], notional_rial=str(group['notional']),
                    reported_fee_rial=str(group['reported']), scenario_fee_rial=str(group['scenario']),
                    reported_minus_scenario_rial=str(group['reported'] - group['scenario']),
                    absolute_difference_rial=str(group['absolute_difference']),
                    max_absolute_difference_rial=str(group['max_absolute_difference']))
                # A ratio can be recurring; only monetary arithmetic above must be exact.
                with localcontext() as ratio_context:
                    ratio_context.traps[Inexact] = False
                    ratio = float(group['reported'] / group['notional']) if group['notional'] else None
                    sides[side]['observed_weighted_fee_fraction'] = ratio if ratio is None or isfinite(ratio) else None
                    sides[side]['weighted_fraction_overflow'] = ratio is not None and not isfinite(ratio)
        except DecimalException as exc:
            raise ValueError('مبالغ گزارش از دقت محاسبه دقیق فراتر رفته‌اند') from exc
    return dict(version=VERSION, price_unit='Rial', amount_encoding='exact_decimal_strings',
        scenario=dict(model='fixed_undated_proportional_rates', rates={k: str(v) for k, v in rates.items()}),
        tolerance_rial=str(tolerance), row_count=row_count, different_row_count=different_count,
        date_start=min(dates) if dates else None, date_end=max(dates) if dates else None, sides=sides,
        differences=details, detail_limit=detail_limit, details_truncated=different_count > len(details),
        historical_fee_schedule_verified=False, actual_history_complete=False,
        fee_tax_breakdown_verified=False, historical_instrument_scope_verified=False,
        accepted_for_signals=False,
        limitations=['Reported combined charges are not independently verified exchange fills.',
                     'Mixed instruments, dates, caps, discounts and displayed average prices may explain differences.',
                     'A match does not establish applicable legal tariffs, rounding, tax or complete history.',
                     'No difference is repaired and no rate is fitted or substituted into research.'])
