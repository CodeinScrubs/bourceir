"""کسر مصنوعی سهم در مدل پژوهش؛ حق واقعی یا نقد قابل خرج نیست."""

import math


class SyntheticFractionBook:
    """مقدار و بهای کسر سهم جداست؛ اقدام بعدی و قیمت روز بر ارزش آن اثر می‌گذارند.

    کسر انباشته حتی اگر بعداً از یک سهم بیشتر شود قابل معامله فرض نمی‌شود.
    نقد شدن و زمان وصول حقوق شرکتی به شواهدی نیاز دارد که این مدل ندارد.
    """

    def __init__(self, sell_fee=0.):
        self.sell_fee = sell_fee
        self.exposures = {}

    def adjust(self, symbol, quantity_multiplier):
        if symbol in self.exposures:
            self.exposures[symbol]['quantity'] *= quantity_multiplier

    def add(self, symbol, quantity, cost):
        if quantity <= 0:
            return
        exposure = self.exposures.setdefault(symbol, dict(quantity=0., cost_rial=0., mark=None, mark_date=None))
        exposure['quantity'] += quantity
        exposure['cost_rial'] += cost

    def mark(self, symbol, price, day):
        if symbol in self.exposures:
            self.exposures[symbol].update(mark=price, mark_date=day)

    def value(self, prices=None):
        total = 0.
        for symbol, exposure in self.exposures.items():
            mark = exposure['mark'] if prices is None else prices.get(symbol, exposure['mark'])
            if mark is None or not math.isfinite(mark) or mark <= 0:
                raise ValueError('قیمت ارزش‌گذاری کسر مصنوعی سهم نامعلوم است')
            total += exposure['quantity'] * mark * (1 - self.sell_fee)
        return total

    @property
    def cost(self):
        return sum(exposure['cost_rial'] for exposure in self.exposures.values())

    def stale_count(self, day):
        return sum(exposure['mark_date'] != day for exposure in self.exposures.values())

    def records(self):
        return [dict(symbol=symbol, **exposure) for symbol, exposure in sorted(self.exposures.items())]
