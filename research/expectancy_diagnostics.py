"""زمان بندی محاسبات مستقل؛ ترتیب و مولد تصادفی هر مقایسه تغییر نمی کند."""

from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

from research.uncertainty import compare_equity_curves


def checked_workers(workers):
    if type(workers) is not int or not 1 <= workers <= 4:
        raise ValueError('تعداد پردازش محاسبات باید عدد صحیح از ۱ تا ۴ باشد')
    return workers


def _compare(request):
    strategy, benchmark, parameters = request
    return compare_equity_curves(strategy, benchmark, **parameters)


def compare_jobs(requests, *, workers=1):
    """فقط منحنی ها و پارامترهای محاسبه به فرزند می روند؛ بدون DB یا نشست کارگزار."""
    checked_workers(workers)
    if workers == 1:
        return [_compare(request) for request in requests]
    # spawn روی Windows و Linux یک قرارداد دارد؛ هر درخواست بذر مستقل و ثابت دارد.
    with ProcessPoolExecutor(max_workers=workers, mp_context=get_context('spawn')) as pool:
        return list(pool.map(_compare, requests, chunksize=1))
