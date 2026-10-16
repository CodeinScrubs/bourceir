"""ثبت قرارداد پیش از خواندن و حفظ ورودی/کد هر پرسش توصیفی."""

import hashlib
import json
import math
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from config import settings
from core.file_lock import exclusive_file
from core.query_screener import (
    VERSION,
    daily_evidence,
    iso_day,
    predicate,
    rank_caps,
    summarize_symbol,
    trade_evidence,
    validate_spec,
)
from core.timeutils import is_trading_session, now_tehran, now_tehran_iso
from data_collector.trade_archive import TradeArchive

CODE_FILES = ('core/query_screener.py', 'core/file_lock.py', 'ops/query_screener.py', 'ops/query_report.py',
              'data_collector/base.py', 'data_collector/tsetmc.py', 'data_collector/trade_archive.py',
              'core/timeutils.py', 'core/text.py', 'requirements.lock.txt')


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    return None if isinstance(value, float) and not math.isfinite(value) else value


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name('.'+path.name+'-'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            stream.write(json.dumps(clean(value), sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_sources(folder=None):
    manifest = {}
    for name in CODE_FILES:
        data = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        manifest[name] = hashlib.sha256(data).hexdigest()
        if folder is not None:
            target = folder / 'code' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    return manifest


def verify_inputs(folder):
    folder = Path(folder)
    manifest = json.loads((folder/'inputs.json').read_text(encoding='utf-8'))
    required = {'protocol.json','instruments.json','latest-prices.json','daily-prices.json','calendar.json','quote-observations.json'}
    required.update('code/'+name for name in CODE_FILES)
    if not isinstance(manifest,dict) or not required <= set(manifest):
        raise ValueError('فهرست ورودی محفوظ ناقص است')
    for name, expected in manifest.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder.resolve()) or sha(path) != expected:
            raise ValueError('ورودی یا کد محفوظ پرسش مخدوش است')
    protocol = json.loads((folder/'protocol.json').read_text(encoding='utf-8'))
    if protocol['code'] != {name:manifest['code/'+name] for name in CODE_FILES}:
        raise ValueError('نسخه کد با قرارداد محفوظ متفاوت است')
    return manifest


def start_query(db, spec, *, output_root=None, quote_provider=None):
    spec = validate_spec(spec)
    if iso_day(spec['end']) >= now_tehran().date():
        raise ValueError('فقط روزهای کامل گذشته در این پرسش توصیفی مجازند')
    root = Path(output_root or settings.DATA_DIR/'private'/'market-queries').resolve()
    if not root.is_relative_to((settings.DATA_DIR/'private').resolve()):
        raise ValueError('خروجی پرسش باید در data/private/ باشد')
    folder = root / ('QUERY-'+uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    sources = code_sources(folder)
    protocol = dict(id=folder.name, version=VERSION, registered_at=now_tehran_iso(), spec=spec, code=sources,
        purpose='owner-requested descriptive query; no model, feature or parameter selection', holdout_opened=False,
        independent_validation_for_derived_hypotheses=False, historical_membership_verified=False,
        tick_priority='ambiguous daily order first, then candle-confirmed audit; date and symbol_id order',
        signals_enabled=False, source_field='raw last trade: close_raw or dated snapshot last_price', market_cap_basis='current stored shares x latest observed raw last trade',
        current_snapshot_not_historical_pit_master=True, annual_calendar_coverage_verified=False)
    write_json(folder/'protocol.json', protocol)
    registered = db.log_experiment(folder.name+'-PROTOCOL', 'descriptive_market_query_protocol', 'current ordinary universe',
        json.dumps(protocol, ensure_ascii=False), '{}', start_date=spec['start'], end_date=spec['end'], status='registered',
        dataset_version='public-raw', features_version='not_used', model_version='not_a_model')
    if not registered:
        raise RuntimeError('ثبت قرارداد پیش از خواندن داده ناموفق بود')
    snapshots = quote_provider.get_market_snapshots() if quote_provider is not None else None
    with db.conn:
        db.conn.execute('BEGIN')
        instruments = [r for r in db.get_universe_symbols() if r['instrument_type_code'] in (300, 303)]
        ids = {r['id'] for r in instruments}
        latest = {}
        for row in db.conn.execute("SELECT p.* FROM daily_prices p JOIN (SELECT symbol_id,max(date) AS d FROM daily_prices "
            "WHERE source='tsetmc' AND date<=? GROUP BY symbol_id) x ON p.symbol_id=x.symbol_id AND p.date=x.d "
            "WHERE p.source='tsetmc'", (now_tehran().date().isoformat(),)):
            if row['symbol_id'] in ids:
                latest[row['symbol_id']] = {k: row[k] for k in ('date','close_raw','price_unit','source','captured_at')}
        columns = 'symbol_id,date,open_raw,high_raw,low_raw,close_raw,close_final_raw,ref_price_raw,volume,value,trade_count,price_unit,source,captured_at'
        prices = [dict(r) for r in db.conn.execute(f'SELECT {columns} FROM daily_prices '
            "WHERE source='tsetmc' AND date>=? AND date<=? ORDER BY symbol_id,date", (spec['start'],spec['end'])) if r['symbol_id'] in ids]
        calendar = db.get_market_calendar()
    if snapshots is not None:
        identities = {str(r['ins_code']):r['id'] for r in instruments}
        seen = set()
        for quote in snapshots:
            identity = str(quote.get('ins_code'))
            if identity not in identities:
                continue
            if identity in seen:
                raise ValueError('دو مظنه برای یک شناسه در پاسخ جاری')
            seen.add(identity)
            stamp = quote.get('provider_timestamp')
            try:
                clock = datetime.fromisoformat(stamp)
                day = clock.date().isoformat()
                if clock.utcoffset() is None or clock > now_tehran():
                    continue
            except (ValueError, TypeError):
                continue
            key = identities[identity]
            current = latest.get(key,{})
            from core.query_screener import decimal
            price = decimal(quote.get('last_price'))
            if (day >= current.get('date','') and price is not None and price > 0 and
                quote.get('source') == 'tsetmc' and quote.get('price_unit') == 'Rial'):
                latest[key] = dict(date=day, close_raw=quote['last_price'], price_unit='Rial', source='tsetmc',
                    captured_at=quote.get('captured_at'), provider_timestamp=stamp, quote_kind='dated_market_snapshot')
    for name, value in (('instruments.json',instruments), ('latest-prices.json',latest), ('daily-prices.json',prices),
                        ('calendar.json',calendar),('quote-observations.json',snapshots)):
        write_json(folder/name,value)
    if sources != code_sources():
        raise RuntimeError('کد در میانه خواندن پرسش تغییر کرده است')
    files = [p for p in folder.rglob('*') if p.is_file()]
    write_json(folder/'inputs.json', {p.relative_to(folder).as_posix():sha(p) for p in files})
    evaluate_query(folder)
    return folder


def evaluate_query(folder, **kwargs):
    folder = Path(folder).resolve()
    if not folder.is_relative_to((settings.DATA_DIR/'private').resolve()):
        raise ValueError('پرسش فقط از پوشه خصوصی محلی خوانده می‌شود')
    with exclusive_file(folder/'.evaluate.lock'):
        return _evaluate_query(folder, **kwargs)


def _evaluate_query(folder, *, provider=None, archive_root=None, max_trade_days=0, refresh_archive=False, progress=None):
    if isinstance(max_trade_days, bool) or not isinstance(max_trade_days, int) or not 0 <= max_trade_days <= 100000:
        raise ValueError('سقف دریافت ریزمعاملات باید عدد صحیح بین صفر و صد هزار باشد')
    if max_trade_days and provider is None:
        raise ValueError('دریافت شبکه‌ای فقط با تأمین‌کننده صریح انجام می‌شود')
    folder = Path(folder)
    verify_inputs(folder)
    protocol = json.loads((folder/'protocol.json').read_text(encoding='utf-8'))
    if protocol['version'] != VERSION or protocol['code'] != code_sources():
        raise ValueError('نسخه کد تغییر کرده؛ پرسش تازه با قرارداد مستقل ثبت کن')
    spec = validate_spec(protocol['spec'])
    instruments = json.loads((folder/'instruments.json').read_text(encoding='utf-8'))
    latest = {int(k):v for k,v in json.loads((folder/'latest-prices.json').read_text(encoding='utf-8')).items()}
    prices = json.loads((folder/'daily-prices.json').read_text(encoding='utf-8'))
    calendar = json.loads((folder/'calendar.json').read_text(encoding='utf-8'))
    caps = rank_caps(instruments, latest, low=spec['cap_low'], high=spec['cap_high'])
    members = {r['symbol_id']:r for r in caps['rows'] if r['slice_membership'] in ('inside','boundary_uncertain','unknown')}
    market_dates = sorted({r['date'] for r in prices})
    groups = defaultdict(list)
    for row in prices:
        if row['symbol_id'] in members:
            groups[row['symbol_id']].append(daily_evidence(dict(row, ins_code=members[row['symbol_id']]['ins_code']),
                dip_pct=spec['dip_pct'], rally_pct=spec['rally_pct']))
    archive = TradeArchive(archive_root or settings.DATA_DIR/'private'/'trade-archive')
    prior = json.loads((folder/'trade-references.json').read_text(encoding='utf-8')) if (folder/'trade-references.json').exists() else {}
    references, fetched = dict(prior), 0
    consecutive_failures, stopped_reason = 0, None
    candidates = [(key,row) for key,days in groups.items() for row in days if row['event_possible'] is True] if spec['kind']=='dip_rally' else []
    candidates.sort(key=lambda pair:(pair[1]['event_confirmed'] is True,pair[1]['date'],pair[0]))
    # همه نمادها و روزهای کاندید بررسی می‌شوند؛ شاهد کندل با شاهد تیک قاطی نمی‌شود.
    for key,row in candidates:
        ref = f'{key}:{row["date"]}'
        record, path = None, None
        if ref in references:
            path = Path(references[ref]['path']).resolve()
            if not path.is_relative_to(archive.root.resolve()):
                raise ValueError('مسیر نسخه ریزمعاملات خارج از آرشیو است')
            if sha(path) != references[ref]['sha256']:
                raise ValueError('نسخه محفوظ ریزمعاملات تغییر کرده است')
            record = archive.load(path)
        if fetched < max_trade_days and (record is None or refresh_archive):
            record, path = archive.capture(provider, members[key]['ins_code'], row['date'], refresh=refresh_archive)
            fetched += 1
            # پاسخ HTTP خالی، فقدان آرشیو همان روز است؛ خطای دریافت کل سرویس نیست.
            consecutive_failures = consecutive_failures+1 if record.get('status')=='unavailable' else 0
        if record is None:
            continue
        evidence = trade_evidence(record['observation'], row, dip_pct=spec['dip_pct'], rally_pct=spec['rally_pct'])
        evidence['archive_status']=record.get('status')
        if record.get('status') not in ('ok',None):
            evidence['issues'].append('وضعیت نسخه آرشیو: '+str(record['status']))
        row['tick_evidence'] = evidence
        if evidence.get('contradictory'):
            row.update(event_confirmed=None, evidence='daily_trade_conflict')
        elif evidence['event_confirmed'] is True:
            row.update(event_confirmed=True, evidence='ordered_trade_witness')
        elif evidence['event_confirmed'] is False:
            if row['event_confirmed'] is True:
                row.update(event_confirmed=None, evidence='daily_trade_conflict')
            else:
                row.update(event_confirmed=False, event_possible=False, evidence='complete_trades_no_event')
        references[ref] = dict(path=str(path.resolve()), sha256=sha(path), ins_code=members[key]['ins_code'], trade_date=row['date'])
        if fetched and fetched % 25 == 0:
            write_json(folder/'trade-references.json', references)
            if progress:
                progress(dict(fetched=fetched, candidates=len(candidates), referenced=len(references)))
        if consecutive_failures >= 3:
            stopped_reason = 'three_consecutive_failed_trade_requests'
            break
    write_json(folder/'trade-references.json', references)
    results = []
    for key, meta in members.items():
        summary = summarize_symbol(meta, groups[key], market_dates=market_dates)
        summary['where_result'] = predicate(spec['where'], summary)
        results.append(summary)
    results.sort(key=lambda r: (-r['confirmed_count'], -r['possible_count'], r['symbol_fa']))
    start, end = iso_day(spec['start']), iso_day(spec['end'])
    weekday_candidates = []
    day = start
    while day <= end:
        if is_trading_session(day, calendar):
            weekday_candidates.append(day.isoformat())
        day += timedelta(days=1)
    result = dict(protocol=protocol, evaluated_at=now_tehran_iso(), caps=caps, results=results,
        details={str(k):v for k,v in groups.items()}, market_observed_dates=market_dates,
        calendar_unverified_no_data_dates=sorted(set(weekday_candidates)-set(market_dates)),
        trade_references=references, candidate_days=len(candidates), tick_attempts_this_run=fetched,
        collection_stopped_reason=stopped_reason,
        count_semantics='confirmed lower bound among observed daily bars; possible includes ambiguous and invalid observed bars',
        predicates=dict(Counter(str(r['where_result']) for r in results)),
        rank_semantics='sorted by confirmed lower bound, not a uniquely proven annual ranking',
        accepted_for_signals=False, profitability_proven=False, execution_proven=False,
        membership_is_current_not_historical=True, annual_coverage_verified=False)
    if protocol['code'] != code_sources():
        raise RuntimeError('کد حین ارزیابی تغییر کرده؛ خروجی نهایی منتشر نشد')
    revision = folder / ('RESULT-'+uuid.uuid4().hex)
    revision.mkdir()
    write_json(revision/'result.json', result)
    from ops.query_report import write_report
    write_report(revision, result)
    write_json(revision/'checksums.json', {p.name:sha(p) for p in revision.iterdir() if p.is_file()})
    verify_inputs(folder)
    write_json(folder/'current.json', dict(revision=revision.name, result_sha256=sha(revision/'result.json')))
    return result, revision


def read_query(folder):
    """نسخه کامل قبلی در زمان انتشار نسخه بعدی نیز قابل خواندن است."""
    folder = Path(folder).resolve()
    verify_inputs(folder)
    pointer = json.loads((folder/'current.json').read_text(encoding='utf-8'))
    revision = (folder/pointer['revision']).resolve()
    if revision.parent != folder or not revision.name.startswith('RESULT-'):
        raise ValueError('مسیر نسخه نتیجه نامعتبر است')
    hashes = json.loads((revision/'checksums.json').read_text(encoding='utf-8'))
    required = {'result.json','report.html','companies.csv','summary.csv','days.csv','report.md'}
    if not required <= set(hashes):
        raise ValueError('نسخه نتیجه ناقص است')
    for name, expected in hashes.items():
        path = (revision/name).resolve()
        if path.parent != revision or sha(path) != expected:
            raise ValueError('محتوا یا هش نتیجه پرسش تغییر کرده است')
    if hashes['result.json'] != pointer['result_sha256']:
        raise ValueError('اشاره‌گر با هش نتیجه سازگار نیست')
    result = json.loads((revision/'result.json').read_text(encoding='utf-8'))
    if result['protocol'] != json.loads((folder/'protocol.json').read_text(encoding='utf-8')):
        raise ValueError('قرارداد نتیجه با ورودی محفوظ متفاوت است')
    return result, revision
