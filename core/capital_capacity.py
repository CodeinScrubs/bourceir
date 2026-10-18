"""تفکیک حساب نقد و هزینه؛ بازگرداندن کارمزد یک بکتست بدون هزینه نیست."""

import math
from collections import Counter

VERSION = 'capital-capacity-v1'


def _number(value, *, nonnegative=False):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
            or nonnegative and value < 0):
        raise ValueError('عدد مالی باید معلوم و متناهی باشد')
    return float(value)


def diagnose_cash_ledger(equity, executions, decisions, metrics, selected_opportunities):
    """حتی برش نامعلوم، حساب نقد و دفتر اجرای معلوم را نگه می‌دارد؛ سود نامعلوم عدد نمی‌گیرد."""
    if type(selected_opportunities) is not int or selected_opportunities < 0 or equity.empty:
        raise ValueError('شمار نامزد و منحنی سرمایه لازم است')
    initial = _number(metrics['initial_capital_rial'], nonnegative=True)
    if initial <= 0:
        raise ValueError('سرمایه باید مثبت باشد')
    buys, sells, fees, realized = [], [], 0., 0.
    for row in executions.to_dict('records'):
        if row['side'] not in ('buy', 'sell'):
            raise ValueError('سمت اجرای نامعتبر')
        quantity, price, fee = (_number(row[key], nonnegative=True) for key in ('quantity', 'price_rial', 'fee_rial'))
        if quantity <= 0 or price <= 0:
            raise ValueError('تعداد و قیمت اجرا باید مثبت باشند')
        (buys if row['side'] == 'buy' else sells).append(quantity * price)
        fees += fee
        if row['side'] == 'sell':
            realized += _number(row['realized_net_rial'])
    if len(executions) != metrics['executions'] or not math.isclose(fees, metrics['fees_rial'], rel_tol=1e-9, abs_tol=.01):
        raise ValueError('تعداد اجرا یا کارمزد با دفتر آشتی ندارد')
    if not math.isclose(realized, metrics['realized_net_rial'], rel_tol=1e-9, abs_tol=.01):
        raise ValueError('سود محقق‌شده با دفتر آشتی ندارد')
    cash = _number(equity.iloc[-1]['cash_rial'], nonnegative=True)
    expected_cash = initial - sum(buys) + sum(sells) - fees
    if not math.isclose(cash, expected_cash, rel_tol=1e-9, abs_tol=.01):
        raise ValueError('حساب نقد با گردش اجرا و کارمزد آشتی ندارد')
    known = metrics['status'] == 'complete'
    if not known and metrics['status'] != 'invalid_data':
        raise ValueError('وضعیت برش ناشناخته است')
    pnl, exposure, drawdown, loss = None, None, None, None
    if len(buys) > selected_opportunities:
        raise ValueError('تعداد ورود از نامزد بیشتر است')
    if known:
        values = [_number(v, nonnegative=True) for v in equity['equity_rial']]
        final = _number(metrics['final_equity_rial'], nonnegative=True)
        observed = _number(equity.iloc[-1]['equity_rial'], nonnegative=True)
        total = _number(metrics['total_return'])
        if not math.isclose(final, observed, rel_tol=1e-9, abs_tol=.01) or not math.isclose(
                total, final / initial - 1, rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError('ارزش نهایی با بازده و منحنی آشتی ندارد')
        pnl = final - initial
        peak, drawdown = initial, 0.
        for value in values:
            peak = max(peak, value)
            drawdown = max(drawdown, 1 - value / peak)
        loss = max(0., 1 - min(values + [initial]) / initial)
        if not math.isclose(_number(metrics['max_drawdown']), drawdown, rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError('افت سرمایه با منحنی آشتی ندارد')
        if 'max_loss_from_initial' in metrics and not math.isclose(_number(metrics['max_loss_from_initial']), loss, rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError('زیان از مبنا با منحنی آشتی ندارد')
        if all(v > 0 for v in values):
            exposure = sum((value - _number(c, nonnegative=True)) / value for value, c in zip(values, equity.cash_rial)) / len(values)
    elif metrics['final_equity_rial'] is not None or metrics['total_return'] is not None:
        raise ValueError('برش نامعلوم نباید سود عددی داشته باشد')
    elif all(isinstance(v, (float, int)) and math.isfinite(v) for v in equity['equity_rial']):
        raise ValueError('شاهد ارزش‌گذاری نامعلوم در منحنی موجود نیست')
    statuses = dict(Counter(decisions['status'])) if 'status' in decisions else {}
    addback = None if pnl is None else pnl + fees
    return dict(selected_opportunities=selected_opportunities, buy_executions=len(buys), sell_executions=len(sells),
        entry_fill_fraction=None if not known or not selected_opportunities else len(buys) / selected_opportunities,
        entry_outcomes_complete=known,
        initial_capital_rial=initial, ending_cash_rial=cash, buy_notional_rial=sum(buys), sell_notional_rial=sum(sells),
        recorded_fees_rial=fees, recorded_fees_fraction_initial=fees / initial,
        turnover_notional_over_initial=(sum(buys) + sum(sells)) / initial,
        realized_net_rial=realized, net_pnl_rial=pnl, net_return=metrics['total_return'],
        mean_noncash_equity_share=exposure, max_drawdown=drawdown, max_loss_from_initial=loss,
        fixed_fills_fee_addback_pnl_rial=addback,
        nonrealized_proxy_residual_rial=None if pnl is None else pnl - realized,
        fee_addback_is_counterfactual_backtest=False,
        diagnosis=('unknown' if pnl is None else 'idle' if not buys else 'net_positive' if pnl > 0 else
                   'nonpositive_even_after_recorded_fee_addback' if addback <= 0 else 'fee_sensitive_fixed_fills'),
        decision_statuses=statuses, open_positions=metrics['open_positions'],
        stale_mark_days=metrics['stale_mark_days'], corporate_action_events=metrics['corporate_action_events'],
        accepted_for_signals=False)


def summarize_cases(rows):
    if not rows:
        raise ValueError('مطالعه بدون برش معتبر نیست')
    known = [r for r in rows if r['net_return'] is not None]
    invested = [r for r in known if r['buy_executions'] > 0]
    selected = sum(r['selected_opportunities'] for r in rows)
    filled = sum(r['buy_executions'] for r in rows)
    return dict(folds=len(rows), known_folds=len(known), unknown_folds=len(rows) - len(known),
        invested_known_folds=len(invested), idle_known_folds=len(known) - len(invested),
        positive_known_folds=sum(r['net_return'] > 0 for r in known),
        mean_known_fold_return=None if not known else sum(r['net_return'] for r in known) / len(known),
        all_fold_mean=None if len(known) != len(rows) else sum(r['net_return'] for r in known) / len(rows),
        mean_recorded_fee_fraction=sum(r['recorded_fees_fraction_initial'] for r in rows) / len(rows),
        selected_opportunities=selected, buy_executions=filled,
        pooled_model_entry_fill_fraction=None if not selected or any(not r['entry_outcomes_complete'] for r in rows) else filled / selected,
        diagnoses=dict(Counter(r['diagnosis'] for r in rows)),
        mean_known_noncash_equity_share=None if not known or any(r['mean_noncash_equity_share'] is None for r in known) else
            sum(r['mean_noncash_equity_share'] for r in known) / len(known),
        maximum_known_drawdown=None if not known else max(r['max_drawdown'] for r in known),
        corporate_action_events=sum(r['corporate_action_events'] for r in rows),
        return_is_continuous_account_cagr=False, accepted_for_signals=False)
