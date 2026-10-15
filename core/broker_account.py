"""Descriptive reconciliation of broker observations. No trade recommendations."""

from collections import Counter, defaultdict
from decimal import Decimal

from core.screening import current_daily_row


def compare_trade_cash(ledger_rows, export_rows):
    """Compare date/side cash totals without guessing aliases or identifying exchange fills.

    A matching aggregate does not establish completeness: offsetting errors, rounded
    average prices and different report coverage can remain. Keep every difference.
    """
    ledger, exported = defaultdict(list), defaultdict(list)
    for row in ledger_rows:
        if row['category'] in ('buy', 'sell'):
            ledger[row['date'], row['category']].append(row)
    for row in export_rows:
        exported[row['date'], row['side']].append(row)
    groups = []
    for date, side in sorted(ledger.keys() | exported.keys()):
        cash_rows, order_rows = ledger[date, side], exported[date, side]
        posted = sum(-r['debit_rial'] if side == 'buy' else r['credit_rial'] for r in cash_rows)
        implied = sum((Decimal(str(r['quantity'])) * Decimal(str(r['price_rial']))
                       + Decimal(str(r['fee_rial'])) * (1 if side == 'buy' else -1)
                       for r in order_rows), Decimal(0))
        difference = implied - posted
        # Whole-rial display/fee rounding only; this is not a tolerance on price or quantity.
        tolerance = len(cash_rows) + len(order_rows)
        if not order_rows:
            status = 'only_ledger'
        elif not cash_rows:
            status = 'only_order_export'
        elif difference == 0:
            status = 'equal_cash_total'
        elif abs(difference) <= tolerance:
            status = 'within_display_rounding'
        else:
            status = 'amount_difference'
        groups.append(dict(date=date, side=side, ledger_cash_rial=posted,
                           order_export_implied_cash_rial=float(implied), difference_rial=float(difference),
                           ledger_row_count=len(cash_rows), order_export_row_count=len(order_rows),
                           rounding_tolerance_rial=tolerance, status=status))
    return dict(grouping='date_and_side_without_symbol_aliases', groups=groups,
                status_counts=dict(Counter(g['status'] for g in groups)),
                complete_execution_history_verified=False)


def reconcile_account(portfolio, turnover=None, fills=None, *, market_rows=(), baseline=None):
    if portfolio is None:
        return None
    rows, summary = portfolio['rows'], portfolio['summary']
    total = summary['portfolio_value_rial']
    quotes = {r['symbol_fa']: r for r in market_rows}
    positions = []
    for row in rows:
        quote = quotes.get(row['symbol_fa'])
        item = {**row, 'weight_pct': row['value_rial'] / total * 100 if total else None,
                'archive_date': quote.get('date') if quote else None,
                'archive_last_rial': quote.get('close_raw') if quote else None,
                'sector_code': quote.get('sector_code') if quote else None,
                'instrument_type_code': quote.get('instrument_type_code') if quote else None,
                'research_scope_eligible': quote.get('instrument_type_code') in (300, 303) if quote else None,
                'archive_session_current': bool(quote and current_daily_row(quote.get('date')))}
        if not quote or quote.get('close_raw') is None:
            item['price_check'] = 'missing_archive'
        elif row['last_price_rial'] != quote['close_raw']:
            item['price_check'] = 'price_mismatch'
        elif portfolio.get('market_date') is None:
            item['price_check'] = 'price_equal_date_unverified'
        elif quote.get('date') != portfolio['market_date']:
            item['price_check'] = 'date_mismatch'
        else:
            item['price_check'] = 'matched'
        positions.append(item)
    positions.sort(key=lambda x: x['value_rial'], reverse=True)
    report = dict(captured_at=portfolio['captured_at'], positions=positions, summary=summary,
                  rounding_differences_rial=portfolio['rounding_differences_rial'],
                  price_checks=dict(Counter(x['price_check'] for x in positions)),
                  actual_history_complete=False, trade_recommendations_enabled=False,
                  total_account_return=None, cash_available_rial=summary['buying_power_rial'])
    report['concentration'] = dict(top_two_weight_pct=sum(p['value_rial'] for p in positions[:2]) / total * 100 if total else None,
                                   top_ten_weight_pct=sum(p['value_rial'] for p in positions[:10]) / total * 100 if total else None)
    if turnover:
        ledger = turnover['rows']
        report['turnover'] = dict(count=len(ledger), start=turnover['date_start'], end=turnover['date_end'],
                                  categories=dict(Counter(x['category'] for x in ledger)),
                                  balance_chain_verified=turnover['balance_chain_verified'],
                                  opening_balance_rial=turnover.get('opening_balance_rial'),
                                  ending_balance_rial=turnover['ending_balance_rial'],
                                  cash_difference_rial=summary['cash_rial'] - turnover['ending_balance_rial'],
                                  classified_cash_in_rial=sum(x['credit_rial'] for x in ledger if x['category'] == 'cash_in'),
                                  classified_cash_out_rial=-sum(x['debit_rial'] for x in ledger if x['category'] == 'cash_out'))
        incoming = report['turnover']['classified_cash_in_rial']
        outgoing = report['turnover']['classified_cash_out_rial']
        report['turnover']['net_classified_cash_in_rial'] = incoming - outgoing
        report['turnover']['equity_minus_net_classified_cash_rial'] = summary['net_assets_rial'] - incoming + outgoing
        report['turnover']['unclassified_event_count'] = sum(x['category'] == 'unclassified' for x in ledger)
        # Aggregated settlement rows do not prove corporate-action quantities or execution timestamps.
        quantities = Counter()
        for row in ledger:
            if row['category'] in ('buy', 'sell'):
                quantities[row['raw_symbol']] += row['quantity'] * (1 if row['category'] == 'buy' else -1)
        report['inventory_unreconciled_symbols'] = [p['symbol_fa'] for p in positions
                                                    if quantities.get(p['symbol_fa']) != p['quantity']]
    if fills:
        records = fills['rows']
        dates = [r['date'] for r in records]
        report['fills'] = dict(count=len(records), date_start=min(dates) if dates else None,
                               date_end=max(dates) if dates else None,
                               fees_rial=float(sum((Decimal(str(x['fee_rial'])) for x in records), Decimal(0))),
                               missing_symbol_count=sum(x['raw_symbol'] is None for x in records),
                               side_counts=dict(Counter(r['side'] for r in records)))
        if turnover:
            report['trade_cash_comparison'] = compare_trade_cash(turnover['rows'], records)
    if baseline:
        floor = baseline['capital_rial'] - baseline['max_loss_rial']
        report['phase'] = dict(**baseline, floor_rial=floor,
                               change_rial=summary['net_assets_rial'] - baseline['capital_rial'],
                               remaining_loss_budget_rial=max(0, summary['net_assets_rial'] - floor),
                               loss_limit_reached=summary['net_assets_rial'] <= floor)
    return report
