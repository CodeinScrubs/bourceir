"""حساب سناریوی دو پیامدی؛ احتمال ورودی فرض کاربر است، نه پیش‌بینی سامانه."""

import math
from numbers import Real

from config import settings
from core.risk import finite_number


def scenario_expectancy(*, entry_rial, stop_rial, target_rial, target_probability=None, slippage_bps=None):
    values = [entry_rial, stop_rial, target_rial] + [v for v in (target_probability, slippage_bps) if v is not None]
    if any(not isinstance(v, Real) for v in values):
        raise ValueError('قیمت، لغزش و احتمال باید عدد حقیقی باشند')
    entry, stop, target = map(finite_number, (entry_rial, stop_rial, target_rial))
    if not stop < entry < target:
        raise ValueError('حد زیان باید کمتر از ورود، و هدف بیشتر از ورود باشد')
    slip = finite_number(settings.BACKTEST_CONFIG['default_slippage_bps'] if slippage_bps is None else slippage_bps,
                         positive=False) / 10000
    if slip >= 1:
        raise ValueError('لغزش باید کمتر از صد درصد باشد')
    probability = None if target_probability is None else finite_number(target_probability, positive=False)
    if probability is not None and probability > 1:
        raise ValueError('احتمال فرضی باید بین صفر و یک باشد')
    buy, sell = settings.FEES['buy'], settings.FEES['sell']
    cost = entry * (1 + slip) * (1 + buy)
    gain = target * (1 - slip) * (1 - sell) - cost
    loss = cost - stop * (1 - slip) * (1 - sell)
    if not all(math.isfinite(v) for v in (cost, gain, loss)) or cost <= 0 or loss <= 0:
        raise ValueError('مقادیر سناریو خارج از دامنه محاسبه معتبرند')
    threshold = loss / (gain + loss) if gain > 0 else None
    expected = None if probability is None else probability * gain - (1 - probability) * loss
    return dict(version='binary-payoff-v1', entry_cost_per_share_rial=cost,
                target_net_per_share_rial=gain, stop_loss_per_share_rial=loss,
                target_net_return=gain / cost, stop_net_return=-loss / cost,
                break_even_target_probability=threshold, assumed_target_probability=probability,
                expected_net_per_share_rial=expected, expected_net_return=None if expected is None else expected / cost,
                target_profitable_after_costs=gain > 0, fees=dict(buy=buy, sell=sell), slippage_bps=slip*10000,
                hypothetical_two_outcome_scenario=True, stop_guaranteed=False, fill_guaranteed=False,
                probability_of_real_trade_profit=None, accepted_for_signals=False)


def outcome_expectancy(*, outcomes, entry_rial=None, exit_now_rial=None, exit_now_executable=None,
                       slippage_bps=None):
    """توزیع صریحِ هدف/زمان/تأخیر؛ هزینه خریدِ پرداخت‌شده در مقایسه نگهداری دوباره کم نمی‌شود.

    احتمال ورودی فرض سناریو است. نبود احتمال/قیمت، امید نامعلوم می‌دهد؛ هیچ
    کسر احتمال به خروج زمانی یا عدم اجرا تخصیص حدسی داده نمی‌شود.
    """
    if not isinstance(outcomes, list) or not outcomes or len(outcomes) > 100:
        raise ValueError('فهرست محدود و غیرخالی پیامدها لازم است')
    if exit_now_executable is not None and type(exit_now_executable) is not bool:
        raise ValueError('وضعیت اجرای خروج باید معلوم یا بولی باشد')
    slip = finite_number(settings.BACKTEST_CONFIG['default_slippage_bps'] if slippage_bps is None else slippage_bps,
                         positive=False) / 10000
    if slip >= 1:
        raise ValueError('لغزش باید کمتر از صد درصد باشد')
    names, checked, unknown = set(), [], False
    for row in outcomes:
        if not isinstance(row, dict) or not isinstance(row.get('name'), str) or not row['name'].strip() or row['name'] in names:
            raise ValueError('نام هر پیامد باید مشخص و یکتا باشد')
        names.add(row['name'])
        p, quote = row.get('probability'), row.get('exit_rial')
        probability = None if p is None else finite_number(p, positive=False)
        price = None if quote is None else finite_number(quote)
        if probability is not None and probability > 1:
            raise ValueError('احتمال پیامد باید بین صفر و یک باشد')
        unknown |= probability is None or price is None
        net = price * (1 - slip) * (1 - settings.FEES['sell']) if price is not None else None
        checked.append(dict(name=row['name'], probability=probability, exit_rial=price, net_proceeds_per_share_rial=net))
    probabilities = [r['probability'] for r in checked]
    if math.fsum(p for p in probabilities if p is not None) > 1. + 1e-10:
        raise ValueError('احتمال‌های معلوم بیش از یک هستند')
    if all(p is not None for p in probabilities) and not math.isclose(math.fsum(probabilities), 1., rel_tol=0., abs_tol=1e-10):
        raise ValueError('جمع احتمال تمام پیامدها باید یک باشد؛ پیامد زمانی/معوق حذف نمی‌شود')
    future = None if unknown else math.fsum(r['probability'] * r['net_proceeds_per_share_rial'] for r in checked)
    cost = None if entry_rial is None else finite_number(entry_rial) * (1 + slip) * (1 + settings.FEES['buy'])
    quote = None if exit_now_rial is None else finite_number(exit_now_rial)
    immediate = quote * (1 - slip) * (1 - settings.FEES['sell']) if quote is not None and exit_now_executable is True else None
    values = (future, cost, immediate)
    if any(v is not None and not math.isfinite(v) for v in values):
        raise ValueError('جمع مالی سناریو متناهی نیست')
    entry_net = future - cost if future is not None and cost is not None else None
    holding = future - immediate if future is not None and immediate is not None else None
    return dict(version='multi-outcome-payoff-v1', outcomes=checked, expected_future_proceeds_per_share_rial=future,
        entry_cost_per_share_rial=cost, expected_entry_net_per_share_rial=entry_net,
        expected_entry_net_return=entry_net / cost if entry_net is not None else None,
        immediate_exit_proceeds_per_share_rial=immediate, expected_hold_advantage_per_share_rial=holding,
        exit_now_executable=exit_now_executable, unresolved_distribution=unknown, slippage_bps=slip * 10000,
        expected_entry_payoff_conditions_on_fill=True,
        probabilities_are_scenario_assumptions=True, actual_exit_timing_and_fill_unverified=True,
        probability_of_real_trade_profit=None, accepted_for_signals=False)
