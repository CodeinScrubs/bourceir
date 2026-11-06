"""Public-data batch for the visible Farabi Industries roster. Never writes the market database."""

import gzip
import json
import os
import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp
from threading import Event, local
from zipfile import ZIP_DEFLATED, ZipFile

from config import settings
from core.farabi_industries import (
    MENU_VIEW,
    PEER_VIEW,
    join_activity,
    read_broker_bindings,
    read_industry_roster,
    read_peer_roster,
)
from core.file_lock import exclusive_file
from core.market_day import MAX_BYTES, parse_market_day
from core.minute_candles import CONTROLS, closed_trade_day, reconcile_session
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from data_collector.trade_archive import TradeArchive
from ops.query_report import export_csv
from ops.report_files import private_report_path, read_private_report, write_private_report

RECEIPT_VERSION = 'farabi-industry-candles-session-v1'
SOURCE_PAUSE = {'rate_limited', 'source_cooldown'}
CANCELED_OPENING = 'canceled_opening_conflict'


def run_folder(day):
    return private_report_path(settings.DATA_DIR / 'private' / 'farabi-industry-candles' / day / 'status.json').parent


def month_dates(day):
    end = date.fromisoformat(day)
    return [(end - timedelta(days=offset)).isoformat() for offset in range(29, -1, -1)]


def _read_market_archive(path, day):
    with gzip.open(path, 'rb') as stream:
        data = stream.read(MAX_BYTES * 2 + 1)
    if len(data) > MAX_BYTES * 2:
        raise ValueError('حجم آرشیو آمار بازار نامعتبر است')
    receipt = json.loads(data)
    receipt['content'] = receipt.pop('body').encode('utf-8')
    return parse_market_day(day, receipt)


def _write_market_archive(path, observation):
    receipt = {key: value for key, value in observation.items() if key != 'content'}
    receipt['body'] = observation['content'].decode('utf-8')
    data = gzip.compress(json.dumps(receipt, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode(), mtime=0)
    with NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as stream:
        temporary = stream.name
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            os.unlink(temporary)
            raise
    try:
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _market_cache(root):
    shared = private_report_path(root.parent / 'market-days' / 'cache.json').parent
    shared.mkdir(parents=True, exist_ok=True)
    previous = sorted((p / 'market-days' for p in root.parent.iterdir()
                       if p != root and is_canonical_iso_date(p.name) and (p / 'market-days').is_dir()), reverse=True)
    return shared, [root / 'market-days', shared, *previous]


def _cached_market_day(paths, target):
    error = None
    for directory in paths:
        path = directory / f'{target}.json.gz'
        if path.is_file():
            try:
                return _read_market_archive(path, target), None
            except (ValueError, OSError, KeyError, TypeError) as exc:
                error = exc
    return None, error


def collect_market_days(provider, requested_day, *, progress=None):
    day = closed_trade_day(requested_day)
    root = run_folder(day)
    directory, cached_paths = _market_cache(root)
    days, coverage = {}, {}
    stop = None
    for target in reversed(month_dates(day)):
        try:
            cached, cache_error = _cached_market_day(cached_paths, target)
            if cached is not None:
                rows, metadata = cached
            elif provider is not None and stop is None:
                observation = provider.get_market_day_observation(target)
                rows, metadata = parse_market_day(target, observation)
                _write_market_archive(directory / f'{target}.json.gz', observation)
            elif cache_error is not None:
                raise cache_error
            else:
                coverage[target] = dict(status='not_observed')
                continue
        except (ValueError, OSError, KeyError, TypeError) as exc:
            transport = getattr(provider, 'last_transport_evidence', None) or {}
            coverage[target] = dict(status='unavailable', detail=str(exc), transport=transport.get('status'))
            if transport.get('status') in SOURCE_PAUSE:
                stop = transport['status']
        else:
            days[target] = rows
            coverage[target] = dict(status='observed' if rows else 'observed_empty', **metadata)
        if progress:
            progress(target, coverage[target])
    # A repeated nonempty body across dates is not evidence for either date.
    seen = {}
    for target, rows in list(days.items()):
        if rows:
            digest = coverage[target]['body_sha256']
            if digest in seen:
                for duplicate in (seen[digest], target):
                    coverage[duplicate]['status'] = 'repeated_nonempty_body'
                    days.pop(duplicate, None)
            seen.setdefault(digest, target)
    write_private_report(root / 'market-coverage.json', dict(start=month_dates(day)[0], end=day, days=coverage, stopped=stop))
    return days, coverage


def load_roster(root):
    capture_path = root / 'farabi-industries.json'
    if not capture_path.is_file():
        raise ValueError('ابتدا برداشت منوی صنایع را برای تاریخ انتخابی در پوشهٔ خصوصی ثبت کنید')
    capture, capture_sha = read_private_report(capture_path, include_sha256=True)
    labels, categories = read_industry_roster(capture)
    binding_path = root / 'farabi-isin-resolution.json'
    observation, binding_sha = (read_private_report(binding_path, include_sha256=True)
                                if binding_path.is_file() else (None, None))
    peer_path = root / 'farabi-peer-groups.json'
    peers, peer_sha = (read_private_report(peer_path, include_sha256=True)
                       if peer_path.is_file() else (None, None))
    peer_labels, peer_categories, peer_bindings = read_peer_roster(peers, labels)
    labels.update(peer_labels)
    categories.extend(peer_categories)
    bindings = read_broker_bindings(observation, labels)
    if any(key in bindings and bindings[key] != isin for key, isin in peer_bindings.items()):
        raise ValueError('شناسهٔ ثبت‌شده و نماد مبنای هم‌گروه متفاوت‌اند')
    return labels, categories, peer_bindings | bindings, dict(captured_at=capture['captured_at'],
        capture_sha256=capture_sha, binding_sha256=binding_sha, peer_capture_sha256=peer_sha,
        peer_captured_at=peers['captured_at'] if peers else None)


def _receipt(path, code, day):
    if not path.is_file():
        return None
    result = read_private_report(path)
    if (result.get('version') != RECEIPT_VERSION or result.get('date') != day or result.get('ins_code') != code
            or 'daily' not in result or (result['daily'] is not None and not isinstance(result['daily'], dict))
            or not isinstance(result.get('audit'), dict) or not isinstance(result['audit'].get('status'), str)):
        raise ValueError('رسید کندل با نماد و روز اجرا سازگار نیست')
    filename = result.get('trade_file')
    if filename is not None and (not isinstance(filename, str) or not re.fullmatch(fr'{code}_{day}_[0-9a-f]{{64}}\.json\.gz', filename)):
        raise ValueError('نام فایل ریزمعامله در رسید معتبر نیست')
    return result


def _read_receipt(root, code, day):
    try:
        return _receipt(root / 'sessions' / f'{code}.json', code, day), None
    except (ValueError, OSError) as exc:
        return None, str(exc)


def _reference(market_rows, day_metadata, code):
    row = market_rows.get(code)
    if row is None:
        return None
    return dict(row=row['raw'], url=day_metadata['url'], body_sha256=day_metadata['body_sha256'])


def _check(code, day, daily, tape, market_reference=None):
    try:
        return reconcile_session(code, day, daily, tape, market_reference=market_reference)
    except ValueError as exc:
        return [], dict(status='invalid_source', detail=str(exc), bar_count=0, deltas=None)


def _collect_one(provider, archive, root, code, day, prior, reference):
    old_audit = prior['audit']['status'] if prior else None
    daily = prior.get('daily') if prior else None
    _, audit = _check(code, day, daily, None, reference)
    if daily is None or audit['status'] == 'invalid_source' or old_audit in ('invalid_source', CANCELED_OPENING):
        fresh = provider.get_dated_daily_observation(code, day)
        if fresh is not None or daily is None:
            daily = fresh
            _, audit = _check(code, day, daily, None, reference)
    filename = None
    if audit['status'] == 'trade_history_unavailable':
        try:
            record, path = archive.capture(provider, code, day, refresh=old_audit == 'archive_error')
            filename = path.name
            _, audit = _check(code, day, daily, record.get('observation'), reference)
            if audit['status'] not in ('reconciled', CANCELED_OPENING) and old_audit in ('daily_mismatch', 'invalid_source') and record.get('observation') is not None:
                fresh_record, fresh_path = archive.capture(provider, code, day, refresh=True)
                if fresh_record['status'] == 'ok':
                    _, fresh_audit = _check(code, day, daily, fresh_record['observation'], reference)
                    if fresh_audit['status'] != 'invalid_source' or audit['status'] == 'invalid_source':
                        filename, audit = fresh_path.name, fresh_audit
        except (ValueError, OSError, KeyError) as exc:
            audit = dict(status='archive_error', detail=str(exc), bar_count=0, deltas=None)
    transport = getattr(provider, 'last_transport_evidence', None) or {}
    receipt = dict(version=RECEIPT_VERSION, ins_code=code, date=day, received_at=now_tehran_iso(),
                   daily=daily, trade_file=filename, audit=audit, transport=transport.get('status'))
    write_private_report(root / 'sessions' / f'{code}.json', receipt, forbidden_paths=(settings.DB_PATH,))
    return audit['status'], transport.get('status')


def collect_sessions(root, day, active, provider_factory, market_rows, day_metadata, *, retry=False, workers=2, progress=None):
    archive_root = private_report_path(settings.DATA_DIR / 'private' / 'trade-archive' / 'record.json').parent
    archive = TradeArchive(archive_root)
    codes = sorted(code for code, item in active.items() if item['target_day_status'] == 'traded')
    pending, states = [], Counter()
    for code in codes:
        previous, error = _read_receipt(root, code, day)
        if error is not None and not retry:
            states['receipt_error'] += 1
        elif previous is None or (retry and previous['audit']['status'] != 'reconciled'):
            pending.append((code, previous))
        else:
            states[previous['audit']['status']] += 1
    state = local()
    providers = []
    paused = Event()

    def collect(job):
        if paused.is_set():
            return 'pending'
        if not hasattr(state, 'provider'):
            state.provider = provider_factory()
            providers.append(state.provider)
        code, prior = job
        status, transport = _collect_one(state.provider, archive, root, code, day, prior,
                                         _reference(market_rows, day_metadata, code))
        if transport in SOURCE_PAUSE:
            paused.set()
        return status

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            attempted = 0
            for status in pool.map(collect, pending):
                states[status] += 1
                if status != 'pending':
                    attempted += 1
                    if progress and attempted % 25 == 0:
                        progress(attempted, len(pending), dict(states))
            if progress:
                progress(attempted, len(pending), dict(states))
    finally:
        for provider in providers:
            provider.close()
    return dict(targets=len(codes), states=dict(states), paused=paused.is_set())


def publish_batch(root, day, listings, active, categories, market_coverage, source, market_rows):
    folder = Path(mkdtemp(prefix='.export-', dir=root))
    archive = TradeArchive(settings.DATA_DIR / 'private' / 'trade-archive')
    audit_rows, candles, canceled_opening_bars, states = [], [], [], Counter()
    for code, item in sorted(active.items(), key=lambda pair: pair[1]['symbol']):
        status = item['target_day_status']
        receipt, receipt_error = _read_receipt(root, code, day) if status == 'traded' else (None, None)
        bars, audit = [], {}
        if receipt_error is not None:
            status, audit = 'receipt_error', dict(detail=receipt_error)
        elif receipt is not None:
            try:
                record = archive.load(archive.root / receipt['trade_file']) if receipt['trade_file'] else None
                if record and (record['ins_code'] != code or record['trade_date'] != day):
                    raise ValueError('آرشیو ریزمعامله مربوط به نماد و روز دیگری است')
                bars, audit = _check(code, day, receipt['daily'], record.get('observation') if record else None,
                                     _reference(market_rows, market_coverage[day], code))
                status = audit['status'] if receipt['audit']['status'] != 'archive_error' else 'archive_error'
                if status not in ('reconciled', CANCELED_OPENING):
                    bars = []
            except (ValueError, OSError, KeyError, TypeError) as exc:
                status, audit = 'archive_error', dict(detail=str(exc))
        elif status == 'traded':
            status = 'pending'
        states[status] += 1
        prefix = dict(symbol=item['symbol'], official_symbol=item['official_symbol'], ins_code=code,
                      isin=item['exchange_isin'], date=day, categories=' | '.join(item['categories']))
        verified_bars = len(bars) if status == 'reconciled' else 0
        supplemental_bars = len(bars) if status == CANCELED_OPENING else 0
        if verified_bars:
            candles.extend({**prefix, **bar} for bar in bars)
        if supplemental_bars:
            canceled_opening_bars.extend({**prefix, **bar, 'status':status} for bar in bars)
        audit_rows.append(dict(prefix, status=status, bar_count=verified_bars, canceled_opening_bar_count=supplemental_bars,
            first_bar_tehran=audit.get('first_bar_tehran'), last_bar_tehran=audit.get('last_bar_tehran'),
            reference_basis=audit.get('reference_basis'), dated_symbol_open_rial=audit.get('dated_symbol_open_rial'),
            market_open_rial=audit.get('market_open_rial'), market_open_minus_symbol_rial=audit.get('market_open_minus_symbol_rial'),
            daily_source_disagreements=audit.get('daily_source_disagreements'), opening_cancellation=audit.get('opening_cancellation'),
            **(audit.get('aggregate') or {}),
            **{f'reference_{key}':(audit.get('official_daily') or {}).get(key) for key in CONTROLS},
            **{f'{key}_delta':(audit.get('deltas') or {}).get(key) for key in CONTROLS},
            detail=audit.get('detail'), session_receipt=f'sessions/{code}.json' if receipt else None,
            daily_source_url=audit.get('daily_source_url') or ((receipt.get('daily') or {}).get('url') if receipt else None),
            trade_source_url=audit.get('trade_source_url'), trade_archive_file=receipt.get('trade_file') if receipt else None))
        item['candle_status'] = status
        item['bar_count'] = verified_bars
        item['canceled_opening_bar_count'] = supplemental_bars
    for category in categories:
        name = category['category']
        members = [item for item in active.values() if name in item['categories']]
        category.update(active_symbols=len(members), target_day_traded=sum(x['target_day_status'] == 'traded' for x in members),
                        reconciled_symbols=sum(x['candle_status'] == 'reconciled' for x in members),
                        canceled_opening_symbols=sum(x['candle_status'] == CANCELED_OPENING for x in members),
                        activity_unobserved=sum(x['activity_status'] != 'active_observed' and name in x['categories'] for x in listings))
    identity_fields = ('symbol', 'official_symbol', 'ins_code', 'exchange_isin', 'broker_isin', 'identity_basis',
                       'categories', 'broker_views', 'displayed_labels', 'activity_status', 'activity_days', 'last_trade_date', 'target_day_status')
    prefix_fields = ('symbol', 'official_symbol', 'ins_code', 'isin', 'date', 'categories')
    export_csv(folder / 'all_listings.csv', listings, identity_fields + ('candidate_codes',))
    export_csv(folder / 'active_symbols.csv', sorted(active.values(), key=lambda x:x['symbol']),
               identity_fields + ('candle_status', 'bar_count', 'canceled_opening_bar_count'))
    export_csv(folder / 'categories.csv', categories, ('group', 'category', 'broker_view', 'status', 'broker_rows', 'active_symbols',
               'target_day_traded', 'reconciled_symbols', 'canceled_opening_symbols', 'activity_unobserved'))
    export_csv(folder / 'audit.csv', audit_rows, prefix_fields + ('status', 'bar_count', 'canceled_opening_bar_count', 'first_bar_tehran', 'last_bar_tehran',
               *CONTROLS, *(f'reference_{key}' for key in CONTROLS), *(f'{key}_delta' for key in CONTROLS),
               'reference_basis', 'dated_symbol_open_rial', 'market_open_rial', 'market_open_minus_symbol_rial',
               'daily_source_disagreements', 'opening_cancellation', 'detail', 'daily_source_url', 'trade_source_url',
               'session_receipt', 'trade_archive_file'))
    candle_fields = prefix_fields + ('time_tehran', 'time_utc', 'open_rial', 'high_rial',
                                     'low_rial', 'close_rial', 'volume_shares', 'trade_count', 'trade_value_rial')
    export_csv(folder / 'candles.csv', candles, candle_fields)
    export_csv(folder / 'canceled-opening-candles.csv', canceled_opening_bars, candle_fields + ('status',))
    menu = [c for c in categories if c['broker_view'] == MENU_VIEW]
    peer_groups = [c for c in categories if c['broker_view'] == PEER_VIEW]
    market_statuses = Counter(x['status'] for x in market_coverage.values())
    observed_market_dates = market_statuses['observed'] + market_statuses['observed_empty']
    summary = dict(version='farabi-industry-candles-report-v1', date=day, start=month_dates(day)[0], end=day,
                   created_at=now_tehran_iso(), categories=len(categories),
                   observed_categories=sum(c['status'] == 'observed' for c in categories),
                   menu_categories=len(menu), observed_menu_categories=sum(c['status'] == 'observed' for c in menu),
                   menu_broker_labels=sum(MENU_VIEW in x['broker_views'] for x in listings),
                   menu_active_symbols=sum(MENU_VIEW in x['broker_views'] for x in active.values()),
                   peer_groups=len(peer_groups), observed_peer_groups=sum(c['status'] == 'observed' for c in peer_groups),
                   peer_only_active_symbols=sum(x['broker_views'] == [PEER_VIEW] for x in active.values()),
                   target_day_traded=sum(x['target_day_status'] == 'traded' for x in active.values()),
                   broker_labels=len(listings), active_symbols=len(active), bars=len(candles),
                   canceled_opening_bars=len(canceled_opening_bars), candle_status=dict(states),
                   activity_status=dict(Counter(x['activity_status'] for x in listings)),
                   market_day_status=dict(market_statuses), observed_market_dates=observed_market_dates,
                   activity_window_requests_observed=observed_market_dates == len(month_dates(day)),
                   native_market_date_verified_days=sum(x.get('native_date_verified') is True for x in market_coverage.values()),
                   market_open_reference_symbols=sum(row['status'] == 'reconciled' and row['reference_basis'] ==
                                                     'market_day_open_with_six_dated_symbol_controls' for row in audit_rows),
                   source=source, scope='visible Farabi Industries presets and observed supplemental peer tables', descriptive_export_only=True,
                   whole_broker_universe_verified=False)
    write_private_report(folder / 'summary.json', summary)
    write_private_report(folder / 'market-coverage.json', dict(start=summary['start'], end=day, days=market_coverage))
    with ZipFile(folder / 'farabi-industry-candles.zip', 'w', compression=ZIP_DEFLATED) as bundle:
        for name in ('all_listings.csv', 'active_symbols.csv', 'categories.csv', 'audit.csv', 'candles.csv',
                     'canceled-opening-candles.csv', 'summary.json', 'market-coverage.json'):
            bundle.write(folder / name, name)
    return folder.rename(folder.with_name(folder.name[1:])), summary


def run_batch(provider_factory, requested_day, *, retry=False, inventory_only=False, progress=None):
    day = closed_trade_day(requested_day)
    root = run_folder(day)
    with exclusive_file(root / 'capture.lock'):
        labels, categories, bindings, source = load_roster(root)
        provider = provider_factory()
        try:
            market, coverage = collect_market_days(provider, day)
        finally:
            provider.close()
        listings, active = join_activity(labels, market, day, bindings=bindings)
        if not inventory_only and day in market:
            collect_sessions(root, day, active, provider_factory, market[day], coverage[day], retry=retry, progress=progress)
        return publish_batch(root, day, listings, active, categories, coverage, source, market.get(day, {}))
