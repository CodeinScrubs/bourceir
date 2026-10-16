"""ثبت برنامه بازبینی قبل از دریافت؛ مقایسه با تأمین‌کننده، بدون اصلاح خودکار داده."""

import argparse
import hashlib
import json
import sqlite3
import uuid
from collections import Counter

import pandas as pd

from config import settings
from core.price_quality import json_price_row, price_quality
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from features.provenance import feature_logic_fingerprint
from ops.report_files import private_report_path, read_private_report, write_private_report
from ops.research_preflight import VERSION as PREFLIGHT_VERSION
from ops.research_preflight import diagnostic_fingerprint, research_preflight

VERSION = 'price-source-recheck-v2'
SOURCE_FILES = ('ops/price_recheck.py', 'ops/research_preflight.py', 'ops/report_files.py',
                'core/price_quality.py', 'core/client_quality.py', 'core/timeutils.py', 'core/text.py',
                'core/data_quality.py', 'core/market_rules.py',
                'features/provenance.py', 'database/db.py', 'data_collector/base.py',
                'data_collector/tsetmc.py', 'data_collector/validator.py',
                'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')


def code_identity(target=None):
    hashes = {}
    for name in SOURCE_FILES:
        content = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        hashes[name] = hashlib.sha256(content).hexdigest()
        if target is not None:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return hashes


def compare_row(issue, history):
    """خروجی نرمال‌شده provider است؛ نبود ردیف علت نبود داده در سرور را معلوم نمی‌کند."""
    result = dict(symbol=issue['symbol'], date=issue['date'], ins_code=issue['ins_code'],
                  before=issue['raw'], after=None, changed_fields=[], remaining_reasons=[],
                  source='tsetmc', source_independently_verified=False, applied=False)
    if history is None:
        return result | dict(status='source_unavailable')
    if not isinstance(history, pd.DataFrame) or 'date' not in history:
        return result | dict(status='provider_contract_error')
    rows = history.loc[history.date.eq(issue['date'])]
    if len(rows) != 1:
        return result | dict(status='provider_row_missing' if rows.empty else 'provider_date_ambiguous')
    raw = json_price_row(rows.iloc[0])
    _, flags = price_quality(rows)
    reasons = flags.columns[flags.iloc[0]].tolist()
    changed = [key for key in raw if raw[key] != issue['raw'].get(key)]
    status = 'source_unchanged' if not changed else 'source_changed_invalid' if reasons else 'source_changed_consistent'
    return result | dict(status=status, after=raw, changed_fields=changed, remaining_reasons=reasons)


def _archived_preflight(db, path, before):
    """گزارش گذشته فقط فهرست نمونه است؛ سلامت کل دیتابیس فعلی از آن استنباط نمی‌شود."""
    report = read_private_report(path)
    cutoff = settings.RESEARCH_CONFIG['holdout_start'] if before is None else before
    expected = dict(version=PREFLIGHT_VERSION, end_exclusive=cutoff,
                    development_start=settings.RESEARCH_CONFIG['dev_start'], database_schema=db.schema_version(),
                    feature_logic_sha256=feature_logic_fingerprint(), diagnostic_code_sha256=diagnostic_fingerprint(),
                    accepted_for_signals=False, profitability_proven=False)
    if (not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']
            or any(report.get(k) != v or type(report.get(k)) is not type(v) for k, v in expected.items())):
        raise ValueError('قرارداد، کد، طرح دیتابیس یا مرز گزارش محفوظ با وضعیت فعلی سازگار نیست')
    issues = report.get('price_issues')
    if not isinstance(issues, list) or len(issues) > 10000:
        raise ValueError('فهرست نقص‌های گزارش محفوظ معتبر نیست')
    keys = set()
    for issue in issues:
        if (not isinstance(issue, dict) or type(issue.get('symbol_id')) is not int or issue['symbol_id'] <= 0
                or not isinstance(issue.get('symbol'), str) or not issue['symbol']
                or not isinstance(issue.get('ins_code'), str) or not issue['ins_code']
                or not is_canonical_iso_date(issue.get('date')) or issue['date'] >= cutoff
                or issue.get('phase') != ('development' if issue['date'] >= expected['development_start'] else 'prior_history')
                or not isinstance(issue.get('raw'), dict) or issue['raw'] != json_price_row(issue['raw'])
                or any(value is not None and type(value) not in (int, float)
                       for key, value in issue['raw'].items() if key != 'date')):
            raise ValueError('هویت، تاریخ یا داده نقص محفوظ معتبر نیست')
        key = (issue['symbol_id'], issue['date'])
        if key in keys:
            raise ValueError('نقص تکراری در گزارش محفوظ')
        keys.add(key)
    # یک محاسبه دسته‌ای؛ ساخت صدها DataFrame تک‌ردیفی هزینه بازبینی محدود را بی‌دلیل بالا می‌برد.
    _, flags = price_quality(pd.DataFrame([issue['raw'] for issue in issues]))
    for index, issue in enumerate(issues):
        reasons = flags.columns[flags.iloc[index]].tolist()
        if not reasons or issue['raw']['date'] != issue['date'] or issue.get('reasons') != reasons:
            raise ValueError('علت نقص با داده محفوظ سازگار نیست')
    return report


def _local_rows_unchanged(db, selected):
    db.conn.execute('BEGIN')
    try:
        result = []
        for issue in selected:
            identity = db.conn.execute('SELECT ins_code, symbol_fa, instrument_type_code FROM symbols WHERE id=?',
                                       (issue['symbol_id'],)).fetchone()
            current = db.get_daily_prices_df(issue['symbol'], start_date=issue['date'], end_date=issue['date'])
            result.append(bool(identity is not None and str(identity['ins_code']) == issue['ins_code']
                               and identity['symbol_fa'] == issue['symbol'] and identity['instrument_type_code'] in (300, 303)
                               and len(current) == 1 and json_price_row(current.iloc[0]) == issue['raw']))
        return result
    finally:
        db.conn.rollback()


def run_recheck(db, *, provider=None, max_rows=5, before=None, from_preflight=None):
    if type(max_rows) is not int or not 1 <= max_rows <= 50:
        raise ValueError('تعداد موارد بازبینی باید عدد صحیح بین یک و پنجاه باشد')
    if provider is not None and getattr(provider, 'name', None) != 'tsetmc':
        raise ValueError('بازبینی این منبع فقط از مسیر تأمین‌کننده TSETMC مجاز است')
    if db.conn.in_transaction:
        raise ValueError('بازبینی شبکه نباید در تراکنش باز پایگاه داده اجرا شود')
    code = code_identity()
    preflight = research_preflight(db, before=before) if from_preflight is None else _archived_preflight(db, from_preflight, before)
    eligible = [r for r in preflight['price_issues'] if r['phase'] == 'development'
                and is_canonical_iso_date(r['date']) and r['date'] < preflight['end_exclusive']]
    selected = sorted(eligible, key=lambda r: (r['date'], r['symbol'], r['ins_code']))[:max_rows]
    if not all(_local_rows_unchanged(db, selected)):
        raise ValueError('ردیف یا هویت نمونه محفوظ تغییر کرده است؛ پیش از دریافت گزارش تازه تهیه کنید')
    run_id = 'RECHECK-' + uuid.uuid4().hex
    folder = settings.DATA_DIR / 'private' / 'price-rechecks' / run_id
    plan_path = private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    if code_identity(folder / 'code') != code:
        raise ValueError('کد هنگام ثبت برنامه بازبینی تغییر کرد')
    plan = dict(version=VERSION, run_id=run_id, registered_at=now_tehran_iso(), code=code,
                preflight=preflight, selected=selected, max_rows=max_rows,
                preflight_basis='fresh_full_scan' if from_preflight is None else 'archived_sample_only',
                fresh_global_scan_performed=from_preflight is None,
                current_global_quality_verified=False,
                preflight_canonical_json_sha256=hashlib.sha256(json.dumps(preflight, ensure_ascii=False,
                    sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest(),
                selection='first chronological development defects among bounded stored details; no returns used',
                network_requested=provider is not None, automatic_repair=False,
                limitations=['Provider returns normalized/validated history, not original HTTP bytes.',
                             'History endpoint may return later dates; only the preregistered development dates are compared.',
                             'Same-source consistency is not independent correctness or historical availability.',
                             'A changed row is a review candidate, not permission to rewrite raw history.'])
    if from_preflight is not None:
        plan['limitations'].append('Archived preflight is a fixed sampling frame, not a fresh global database audit.')
    write_private_report(plan_path, plan, forbidden_paths=(settings.DB_PATH,))
    plan_sha = hashlib.sha256(plan_path.read_bytes()).hexdigest()
    rows, cache, observation_files = [], {}, {}
    for issue in selected:
        requested_at = now_tehran_iso()
        key = issue['ins_code']
        if provider is None:
            result = compare_row(issue, None) | dict(status='not_requested')
        else:
            if key not in cache:
                try:
                    # یک دریافت برای هر نماد؛ فقط روزهای ثبت‌شده مقایسه و محفوظ می‌شوند.
                    cache[key] = (provider.get_symbol_history(key), None, now_tehran_iso())
                except Exception as exc:
                    # متن استثنا ممکن است URL یا اطلاعات نشست داشته باشد؛ فقط نوع ثبت می‌شود.
                    cache[key] = (None, type(exc).__name__, now_tehran_iso())
            history, error_kind, observed_at = cache[key]
            result = compare_row(issue, history)
            result.update(observed_at=observed_at, error_kind=error_kind)
        result['request_group_started_at'] = requested_at if key not in {r['ins_code'] for r in rows} else None
        rows.append(result)
        # نتیجه هر مورد مستقل محفوظ است؛ قطع اجرا، شواهد دریافت‌شده را حذف نمی‌کند.
        observation_path = write_private_report(folder / f'observation-{len(rows):03d}.json', result)
        observation_files[observation_path.name] = hashlib.sha256(observation_path.read_bytes()).hexdigest()
    for row, unchanged in zip(rows, _local_rows_unchanged(db, selected)):
        row['local_row_unchanged_at_completion'] = unchanged
    if code_identity() != code or hashlib.sha256(plan_path.read_bytes()).hexdigest() != plan_sha:
        raise ValueError('کد یا برنامه محفوظ هنگام بازبینی تغییر کرد؛ نتیجه نهایی منتشر نشد')
    for name, digest in observation_files.items():
        if hashlib.sha256((folder / name).read_bytes()).hexdigest() != digest:
            raise ValueError('مشاهده محفوظ هنگام بازبینی تغییر کرد')
    for name, digest in code.items():
        if hashlib.sha256((folder / 'code' / name).read_bytes()).hexdigest() != digest:
            raise ValueError('نسخه کد محفوظ هنگام بازبینی تغییر کرد')
    result = dict(version=VERSION, run_id=run_id, completed_at=now_tehran_iso(), plan_sha256=plan_sha,
                  observations=rows, statuses=dict(Counter(r['status'] for r in rows)),
                  requests_made=len(cache), selected_rows=len(selected), observation_files=observation_files,
                  automatic_repair=False,
                  preflight_basis=plan['preflight_basis'], current_global_quality_verified=False,
                  fresh_global_scan_performed=plan['fresh_global_scan_performed'],
                  accepted_for_signals=False, profitability_proven=False,
                  source_consistent_candidates=sum(r['status'] == 'source_changed_consistent'
                                                   and r['local_row_unchanged_at_completion'] for r in rows))
    write_private_report(folder / 'result.json', result)
    return result, folder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', default=None)
    parser.add_argument('--max-rows', type=int, default=5)
    parser.add_argument('--from-preflight', default=None, help='نمونه‌گیری از گزارش خصوصی محفوظ؛ بررسی تازه کل دیتابیس نیست')
    parser.add_argument('--fetch', action='store_true', help='دریافت واقعی؛ بدون این گزینه فقط برنامه ثبت می‌شود')
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    try:
        provider = None
        if args.fetch:
            from data_collector.tsetmc import TsetmcProvider
            provider = TsetmcProvider()
        with DatabaseManager(read_only=True) as db:
            report, folder = run_recheck(db, provider=provider, max_rows=args.max_rows, before=args.before,
                                        from_preflight=args.from_preflight)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(dict(path=str(folder), statuses=report['statuses'], automatic_repair=False), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
