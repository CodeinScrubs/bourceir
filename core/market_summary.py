"""نبض روزانه با پوشش صریح؛ جمعِ فاقد داده صفر و کندل تک‌قیمت صف قطعی نیست."""

from core import market_rules
from core.client_quality import coherent_side, finite_number


def summarize_daily_market(frame, *, expected_count):
    volume = finite_number(frame, 'volume')
    active = volume > 0
    returns = market_rules.pit_daily_returns(frame).where(active)
    flags = market_rules.lock_flags(frame)
    value_known = market_rules.daily_vwap(frame).notna()
    values = finite_number(frame, 'value').where(value_known)
    net = (coherent_side(frame, frame, 'buy')[2] - coherent_side(frame, frame, 'sell')[2]).where(value_known)
    return dict(count=len(frame), expected_count=expected_count,
                missing_count=max(0, expected_count - len(frame)),
                up=int((returns > 0).sum()), down=int((returns < 0).sum()),
                flat=int((returns == 0).sum()), return_coverage=int(returns.notna().sum()),
                return_unknown_count=int(returns.isna().sum()),
                locked_up=int(flags['locked_up'].sum()), locked_down=int(flags['locked_down'].sum()),
                lock_classification='one_price_proxy_not_verified_order_queue',
                value=float(values.sum(min_count=1)) if values.notna().any() else None,
                value_coverage=int(values.notna().sum()),
                real_flow=float(net.sum(min_count=1)) if net.notna().any() else None,
                flow_coverage=int(net.notna().sum()))
