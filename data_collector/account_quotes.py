"""Refresh public valuation quotes for broker holdings through BaseDataProvider only.

Account symbols never expand the research universe or overwrite canonical prices.
"""

import logging

from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from data_collector.base import BaseDataProvider
from database.broker import BrokerRepository

logger = logging.getLogger(__name__)


def refresh_account_quotes(db, provider: BaseDataProvider, progress=None):
    repo = BrokerRepository(db)
    portfolio = repo.latest('portfolio')
    if portfolio is None:
        raise ValueError('ابتدا اسنپ‌شات معتبر سبد فارابی را وارد کن')
    started_at, succeeded, errors = now_tehran_iso(), 0, []
    for index, position in enumerate(portfolio['rows'], start=1):
        symbol = position['symbol_fa']
        try:
            meta = provider.search_symbol(symbol)
            if not meta or normalize_symbol(meta.get('symbol_fa', '')) != symbol or not meta.get('ins_code'):
                raise ValueError('شناسه عمومی دقیق نماد پیدا نشد')
            history = provider.get_symbol_history(str(meta['ins_code']), last_n=10)
            if history is None or history.empty:
                raise ValueError('تاریخچه عمومی معتبر دریافت نشد')
            latest = history.sort_values('date').iloc[-1].to_dict()
            repo.record_market_observation(meta, latest)
            succeeded += 1
        except Exception as exc:
            # A failed request preserves the prior observation; it is never replaced with a fake quote.
            errors.append(dict(symbol=symbol, error_type=type(exc).__name__))
            logger.warning('به‌روزرسانی قیمت عمومی %s ناموفق: %s', symbol, type(exc).__name__)
        if progress:
            progress(index, len(portfolio['rows']), symbol)
    return dict(started_at=started_at, finished_at=now_tehran_iso(), requested=len(portfolio['rows']),
                succeeded=succeeded, failed=len(errors), errors=errors,
                status='success' if not errors else 'partial' if succeeded else 'failed')
