"""Describe a live worker's requested scope without asserting a successful capture."""

from core.text import normalize_symbol


def _option(args, name):
    values = []
    for index, item in enumerate(args):
        if item == name:
            if index + 1 >= len(args) or args[index + 1].startswith('--'):
                return False, None
            values.append(args[index + 1])
        elif item.startswith(name + '='):
            values.append(item[len(name) + 1:])
    return (False, None) if len(values) > 1 else (True, values[0] if values else None)


def requested_live_scope(worker):
    """The job arguments are a request, not proof of a registered run or fresh data."""
    if not isinstance(worker, dict):
        return dict(state='unknown', symbols=None, source=None, interval=None)
    if worker.get('running') is False:
        return dict(state='stopped', symbols=None, source=None, interval=None)
    if worker.get('running') is not True:
        return dict(state='unknown', symbols=None, source=None, interval=None)
    args = worker.get('args')
    if not isinstance(args, list) or not args or args[0] != 'live' or not all(isinstance(x, str) for x in args):
        return dict(state='unknown', symbols=None, source=None, interval=None)
    symbols_ok, raw_symbols = _option(args, '--symbols')
    source_ok, source = _option(args, '--quote-source')
    interval_ok, raw_interval = _option(args, '--interval')
    if not (symbols_ok and source_ok and interval_ok):
        return dict(state='unknown', symbols=None, source=None, interval=None)
    source = source if source is not None else 'market-watch'
    if source not in ('market-watch', 'dated-quote', 'dated-quote-context'):
        return dict(state='unknown', symbols=None, source=None, interval=None)
    if raw_interval is not None and (len(raw_interval) > 10 or not raw_interval.isascii()
                                     or not raw_interval.isdigit() or int(raw_interval) <= 0):
        return dict(state='unknown', symbols=None, source=None, interval=None)
    interval = int(raw_interval) if raw_interval is not None else None
    symbols = list(dict.fromkeys(normalize_symbol(value) for value in raw_symbols.split(',') if value.strip())) \
        if raw_symbols else None
    if source != 'market-watch' and (not symbols or len(symbols) > 10):
        return dict(state='unknown', symbols=None, source=None, interval=None)
    return dict(state='explicit' if symbols else 'default', symbols=symbols, source=source, interval=interval)


def requested_scope_label(scope, symbol):
    if scope['state'] == 'stopped':
        return 'پایش متوقف'
    if scope['state'] != 'explicit':
        return 'نامعلوم از تنظیمات'
    return 'نام در درخواست' if normalize_symbol(symbol) in scope['symbols'] else 'نام خارج از درخواست'
