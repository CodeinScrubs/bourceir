"""Keep broker classifications and exchange identities separate during the activity join."""

import re
from collections import defaultdict

from core.instrument_history import receipt_time
from core.text import normalize_symbol, to_en_digits

MENU_VIEW = 'farabi_industries_menu'
PEER_VIEW = 'farabi_peer_table'


def symbol_key(label):
    return to_en_digits(normalize_symbol(label))


def _symbol_label(label):
    return isinstance(label, str) and len(label) <= 128 and bool(symbol_key(label))


def _isin(value):
    return isinstance(value, str) and bool(re.fullmatch(r'IR[A-Z0-9]{10}', value))


def _board_url(isin):
    return 'https://farabixo.irfarabi.com/?isin=' + isin


def _table_symbols(row):
    status, symbols = row.get('status'), row.get('symbols')
    count = row.get('row_count')
    if (not isinstance(symbols, list) or not all(map(_symbol_label, symbols))
            or status not in ('observed', 'unverified_table')
            or (status == 'observed' and (not symbols or type(count) is not int or count != len(symbols)))
            or (status != 'observed' and (symbols or count is not None))):
        raise ValueError('ردیف‌های دسته با وضعیت برداشت سازگار نیستند')
    return symbols


def read_industry_roster(capture):
    if (not isinstance(capture, dict) or capture.get('version') != 'farabi-industries-dom-v1'
            or capture.get('source_url') != 'https://farabixo.irfarabi.com/'):
        raise ValueError('برداشت معتبر منوی صنایع فارابی لازم است')
    receipt_time(capture.get('captured_at'))
    menu, categories = capture.get('menu'), capture.get('categories')
    if not isinstance(menu, list) or not isinstance(categories, list) or not menu:
        raise ValueError('ساختار منو یا گروه‌های فارابی نامعتبر است')
    group, groups, seen = None, {}, set()
    for item in menu:
        if (not isinstance(item, dict) or type(item.get('heading')) is not bool
                or not isinstance(item.get('name'), str) or not item['name'].strip() or item['name'] in seen):
            raise ValueError('نام یا نوع گزینه منو مبهم است')
        seen.add(item['name'])
        if item['heading']:
            group = item['name']
        else:
            groups[item['name']] = group
    if not groups or any(not isinstance(row, dict) for row in categories) or list(groups) != [row.get('name') for row in categories]:
        raise ValueError('دسته‌های برداشت‌شده با گزینه‌های قابل انتخاب فارابی یکسان نیستند')
    labels, coverage = {}, []
    for category in categories:
        name = category['name']
        symbols = _table_symbols(category)
        coverage.append(dict(category=name, group=groups[name], status=category['status'],
                             broker_rows=category.get('row_count'), broker_view=MENU_VIEW))
        for label in symbols:
            item = labels.setdefault(symbol_key(label), dict(symbol=label, categories=[], displayed_labels=[], broker_views=[MENU_VIEW]))
            if name not in item['categories']:
                item['categories'].append(name)
            if label not in item['displayed_labels']:
                item['displayed_labels'].append(label)
    return labels, coverage


def read_peer_roster(observation, menu_labels):
    if observation is None:
        return {}, [], {}
    if (not isinstance(observation, dict) or observation.get('version') != 'farabi-peer-groups-dom-v1'
            or not isinstance(observation.get('groups'), list)):
        raise ValueError('قرارداد برداشت جدول‌های هم‌گروه معتبر نیست')
    receipt_time(observation.get('captured_at'))
    labels, categories, bindings, sectors = {}, [], {}, set()
    for row in observation['groups']:
        if not isinstance(row, dict):
            raise ValueError('ساختار جدول هم‌گروه نامعتبر است')
        code, name, seed, isin = (row.get(key) for key in ('sector_code', 'sector_name', 'seed_symbol', 'seed_isin'))
        if (not isinstance(code, str) or not re.fullmatch(r'[0-9]{2}', code) or code in sectors
                or (name is not None and not _symbol_label(name)) or not _symbol_label(seed)
                or not _isin(isin) or row.get('url') != _board_url(isin)):
            raise ValueError('هویت یا صنعت جدول هم‌گروه معتبر نیست')
        symbols = _table_symbols(row)
        if symbols and seed not in symbols:
            raise ValueError('نماد مبنا در جدول هم‌گروه مشاهده نشد')
        sectors.add(code)
        category = f'هم‌گروه · {name or "صنعت"} ({code})'
        categories.append(dict(category=category, group='هم‌گروه تکمیلی', status=row['status'],
                               broker_rows=row.get('row_count'), broker_view=PEER_VIEW))
        for label in symbols:
            key = symbol_key(label)
            if key in menu_labels:
                continue
            item = labels.setdefault(key, dict(symbol=label, categories=[], displayed_labels=[], broker_views=[PEER_VIEW]))
            if category not in item['categories']:
                item['categories'].append(category)
            if label not in item['displayed_labels']:
                item['displayed_labels'].append(label)
            if label == seed:
                if key in bindings and bindings[key] != isin:
                    raise ValueError('نماد مبنای هم‌گروه دو شناسه ناسازگار دارد')
                bindings[key] = isin
    return labels, categories, bindings


def read_broker_bindings(observation, labels):
    if observation is None:
        return {}
    if not isinstance(observation, dict) or observation.get('version') != 'farabi-industry-isin-v1':
        raise ValueError('قرارداد مشاهده شناسه فارابی معتبر نیست')
    receipt_time(observation.get('captured_at'))
    if not isinstance(observation.get('rows'), list):
        raise ValueError('فهرست مشاهده‌های شناسه لازم است')
    resolved = {}
    for row in observation['rows']:
        if not isinstance(row, dict) or not _symbol_label(row.get('symbol')):
            raise ValueError('قالب مشاهده شناسه نامعتبر است')
        key, isin = symbol_key(row['symbol']), row.get('isin')
        if (key not in labels or row.get('category') not in labels[key]['categories']
                or not _isin(isin) or row.get('url') != _board_url(isin)
                or (key in resolved and resolved[key] != isin)):
            raise ValueError('مشاهده شناسه فارابی با نماد و گروه آن سازگار نیست')
        resolved[key] = isin
    return resolved


def join_activity(labels, market_days, target_day, *, bindings=None):
    """One positive exchange session is sufficient. Absence remains a coverage status."""
    bindings = bindings or {}
    by_name, by_isin, identities = defaultdict(set), defaultdict(set), {}
    for day, rows in sorted(market_days.items()):
        for row in rows.values():
            if not row['trade_count'] or not row['isin']:
                continue
            identity = row['ins_code'], row['isin']
            item = identities.setdefault(identity, dict(days=set(), official_symbol=row['symbol']))
            item['days'].add(day)
            item['official_symbol'] = row['symbol']
            by_name[symbol_key(row['symbol'])].add(identity)
            by_isin[row['isin']].add(identity)
    listings, active = [], {}
    for label, source in labels.items():
        isin = bindings.get(label)
        matches = by_isin[isin] if isin else by_name[label]
        listing = {**source, 'broker_isin':isin, 'ins_code':None, 'exchange_isin':None,
                   'identity_basis': 'farabi_board_isin' if isin else 'unique_exchange_display_name',
                   'activity_status': 'activity_unobserved', 'activity_days':None, 'last_trade_date':None,
                   'target_day_status':'unknown', 'official_symbol':None}
        if len(matches) > 1:
            listing.update(activity_status='identity_ambiguous', candidate_codes=sorted(code for code, _ in matches))
        if len(matches) == 1:
            identity, = matches
            code, exchange_isin = identity
            evidence = identities[identity]
            dates = evidence['days']
            target_status = ('traded' if target_day in dates else
                             'not_reported_in_day_list' if target_day in market_days else 'unknown')
            listing.update(ins_code=code, exchange_isin=exchange_isin, activity_status='active_observed',
                           activity_days=len(dates), last_trade_date=max(dates),
                           target_day_status=target_status, official_symbol=evidence['official_symbol'])
            if code not in active:
                active[code] = dict(listing)
            else:
                current = active[code]
                if current['exchange_isin'] != exchange_isin:
                    raise ValueError('یک کد بورس به دو شناسه ابزار نسبت داده شده است')
                for key in ('categories', 'displayed_labels', 'broker_views'):
                    current[key] = list(dict.fromkeys(current[key] + listing[key]))
        listings.append(listing)
    return listings, active
