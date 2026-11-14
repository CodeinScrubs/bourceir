"""Reconcile an external wealth table without accepting its events or executions.

The import contract labels its monetary inputs as Toman. All computed money is
returned in Rial. Arithmetic, funding policy and evidence validity stay separate.
No source, database, model, order or accounting book is changed by this module.
"""

from decimal import Decimal, InvalidOperation, localcontext

from core.text import normalize_symbol

VERSION = 'external-wealth-reconciliation-v1'
MONEY_COLUMNS = (
    'initial_toman', 'subscriptions_toman', 'dps_received_toman', 'dps_receivable_toman',
    'rights_sale_proceeds_toman', 'buy_fee_toman', 'sell_fee_toman',
    'final_holdings_toman', 'final_net_toman', 'total_contributed_toman',
)
POLICIES = ('sell_rights_no_external_cash', 'exercise_with_external_cash')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Explicit finite numeric cells are required')
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError('Explicit finite numeric cells are required') from error
    if not number.is_finite() or abs(number) > Decimal(2**63 - 1):
        raise ValueError('Missing or nonfinite money is unknown, not zero')
    return number


def _money(row, name):
    value = _number(row[name]) * 10
    if abs(value) > Decimal(2**63 - 1):
        raise ValueError('Money is outside the bounded Rial contract')
    if value < 0 and name != 'final_net_toman':
        raise ValueError('Assets, fees and contributed capital must be nonnegative')
    return value


def _json_number(value):
    return int(value) if value == value.to_integral_value() else float(value)


def review_wealth_rows(rows, *, rights_policy, expected_initial_rial=None):
    """Audit a declared table, including fractional shares and outside funding.

``final_net_toman`` in this external format means end assets less subsequent
subscriptions. It is not literal end cash. Net assets include reported dividend
receivables at their stated face value; their worth/receipt is not verified here.
Rounded monetary cells allow half a Rial of reconciliation error. Reported
returns are percentages rounded to two decimals, not assumed exact portfolio
returns. Malformed or duplicate inputs raise rather than yield partial totals.
"""
    if rights_policy not in POLICIES:
        raise ValueError('An explicit supported rights policy is required')
    if not isinstance(rows, (list, tuple)) or not 0 < len(rows) <= 1000:
        raise ValueError('A bounded nonempty external table is required')
    with localcontext() as context:
        context.prec = 40
        return _review(rows, rights_policy, expected_initial_rial)


def _review(rows, rights_policy, expected_initial_rial):
    seen, results = set(), []
    totals = dict.fromkeys(MONEY_COLUMNS, Decimal(0))
    for row in rows:
        if not isinstance(row, dict) or not set(MONEY_COLUMNS) <= set(row):
            raise ValueError('The declared wealth table is missing accounting columns')
        symbol = row.get('symbol')
        if not isinstance(symbol, str) or not normalize_symbol(symbol):
            raise ValueError('Explicit symbol identity required')
        symbol = normalize_symbol(symbol)
        if symbol in seen:
            raise ValueError('Duplicate normalized symbols cannot be counted twice')
        seen.add(symbol)
        money = {key: _money(row, key) for key in MONEY_COLUMNS}
        if money['initial_toman'] <= 0:
            raise ValueError('Strictly positive initial capital required for each sleeve')
        shares = _number(row.get('shares_bought'))
        if shares < 0:
            raise ValueError('Purchased share quantities cannot be negative')
        assets = (money['final_holdings_toman'] + money['dps_received_toman']
            + money['dps_receivable_toman'] + money['rights_sale_proceeds_toman'] - money['sell_fee_toman'])
        contributed = money['initial_toman'] + money['subscriptions_toman']
        net_initial = assets - money['subscriptions_toman']
        model_return = net_initial / money['initial_toman'] - 1
        contributed_return = assets / contributed - 1
        residuals = dict(
            net_value_rial=money['final_net_toman'] - net_initial,
            contributed_rial=money['total_contributed_toman'] - contributed,
        )
        errors = [key for key, value in residuals.items() if abs(value) > Decimal('.5')]
        for field, computed in (('ret_net_pct', model_return), ('ret_on_contributed_pct', contributed_return)):
            if field in row and abs(_number(row[field]) - computed * 100) > Decimal('.0051'):
                errors.append(field)
        issues = []
        if shares != shares.to_integral_value():
            issues.append('fractional_initial_purchase_not_executable_as_ordinary_shares')
        if money['subscriptions_toman'] > 0 and rights_policy == 'sell_rights_no_external_cash':
            issues.append('subscriptions_conflict_with_owner_no_extra_cash_policy')
        if money['dps_receivable_toman'] > 0:
            issues.append('receivable_face_in_assets_not_liquid_cash')
        if str(row.get('basis', '')).startswith('TOTAL'):
            issues.append('external_total_label_is_not_independent_verification')
        results.append(dict(symbol=symbol, arithmetic_errors=errors,
            reconciliation_residuals_rial={key: _json_number(value) for key, value in residuals.items()},
            issues=issues, computed_end_assets_rial=_json_number(assets),
            computed_value_net_subscriptions_rial=_json_number(net_initial),
            computed_model_return=float(model_return), return_on_total_contributed=float(contributed_return),
            economic_return=None, executable_return=None))
        for key, value in money.items():
            totals[key] += value
    initial, subscriptions = totals['initial_toman'], totals['subscriptions_toman']
    assets = (totals['final_holdings_toman'] + totals['dps_received_toman'] + totals['dps_receivable_toman']
        + totals['rights_sale_proceeds_toman'] - totals['sell_fee_toman'])
    value_net_subscriptions = assets - subscriptions
    if expected_initial_rial is not None and _number(expected_initial_rial) != initial:
        raise ValueError('Initial capital disagrees with the declared experiment')
    arithmetic_errors = sum(bool(row['arithmetic_errors']) for row in results)
    policy_conflicts = sum('subscriptions_conflict_with_owner_no_extra_cash_policy' in row['issues'] for row in results)
    return dict(version=VERSION, source_money_unit='Toman', output_money_unit='Rial',
        symbols=len(results), rights_policy=rights_policy, arithmetic_error_rows=arithmetic_errors,
        policy_conflict_rows=policy_conflicts,
        fractional_purchase_rows=sum('fractional_initial_purchase_not_executable_as_ordinary_shares'
            in row['issues'] for row in results),
        initial_rial=_json_number(initial), subsequent_subscriptions_rial=_json_number(subscriptions),
        total_contributed_rial=_json_number(initial + subscriptions),
        computed_end_assets_rial=_json_number(assets),
        end_assets_excluding_reported_receivables_rial=_json_number(assets-totals['dps_receivable_toman']),
        reported_dividend_cash_rial=_json_number(totals['dps_received_toman']),
        reported_dividend_receivables_rial=_json_number(totals['dps_receivable_toman']),
        reported_rights_sale_proceeds_rial=_json_number(totals['rights_sale_proceeds_toman']),
        reported_value_net_subscriptions_rial=_json_number(totals['final_net_toman']),
        computed_value_net_subscriptions_rial=_json_number(value_net_subscriptions),
        computed_model_return=float(value_net_subscriptions / initial - 1),
        return_on_total_contributed=float(assets / (initial + subscriptions) - 1),
        numeric_result_reconciled=arithmetic_errors == 0,
        owner_funding_policy_matches=policy_conflicts == 0,
        economic_return=None, actual_receipts_verified=False, actual_fills_verified=False,
        accepted_for_signals=False, rows=results)
