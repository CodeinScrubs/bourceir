"""Four different evidence claims; a stopped record is not the planned endpoint."""

import math

VERSION = 'replay-outcome-status-v1'


def outcome_status(item, audit, sessions):
    actual = [row['date'] for row in item['equity']]
    if not actual or actual != list(sessions[:len(actual)]):
        raise ValueError('Recorded curve must be an exact prefix of the declared window')
    complete = len(actual) == len(sessions)
    last = item['equity'][-1]
    nav = audit['observed_path_model_return']
    cash = audit['modeled_cash_return']
    if nav is not None and (not complete or not math.isfinite(nav) or audit['invariant_violations']):
        raise ValueError('Known NAV needs the complete independently checked model path')
    if cash is not None and (nav is None or not math.isfinite(cash) or item['metrics']['open_positions']
            or item['metrics']['noncash_receivable_rial'] != 0 or 'terminal_assets_not_cash' in audit['blockers']):
        raise ValueError('Known modeled cash cannot contain unresolved or noncash assets')
    return dict(outcome_contract=VERSION, model_nav_known=nav is not None,
        model_nav_status='known_under_synthetic_contract' if nav is not None else 'unknown',
        modeled_terminal_cash_known=cash is not None,
        modeled_terminal_cash_status='known_under_model' if cash is not None else 'unknown',
        economic_accounting_verified=False, economic_accounting_status='unverified_synthetic_contract',
        actual_execution_verified=False, actual_execution_status='not_verified_by_daily_data',
        planned_endpoint_date=sessions[-1], recorded_asof_date=actual[-1],
        planned_endpoint_reached=complete,
        recorded_assets_context='planned_endpoint' if complete else 'engine_stop_before_planned_endpoint',
        model_nav_rial=last['equity_rial'] if nav is not None else None,
        modeled_terminal_cash_rial=last['cash_rial'] if cash is not None else None,
        model_nav_return=nav, modeled_terminal_cash_return=cash,
        recorded_cash_balance_rial=last['cash_rial'],
        recorded_open_positions=item['metrics']['open_positions'],
        recorded_noncash_receivable_rial=item['metrics']['noncash_receivable_rial'],
        recorded_assets_not_cash='terminal_assets_not_cash' in audit['blockers'],
        synthetic_corporate_entitlements='synthetic_corporate_entitlements' in audit['blockers'],
        economic_return=None, actual_terminal_cash_rial=None, accepted_for_signals=False)
