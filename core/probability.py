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
