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
from ops.report_files import private_report_path, write_private_report
from ops.research_preflight import research_preflight

VERSION = 'price-source-recheck-v1'
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


def run_recheck(db, *, provider=None, max_rows=5, before=None):
    if type(max_rows) is not int or not 1 <= max_rows <= 50:
        raise ValueError('تعداد موارد بازبینی باید عدد صحیح بین یک و پنجاه باشد')
    if provider is not None and getattr(provider, 'name', None) != 'tsetmc':
        raise ValueError('بازبینی این منبع فقط از مسیر تأمین‌کننده TSETMC مجاز است')
    if db.conn.in_transaction:
        raise ValueError('بازبینی شبکه نباید در تراکنش باز پایگاه داده اجرا شود')
    code = code_identity()
    preflight = research_preflight(db, before=before)
    eligible = [r for r in preflight['price_issues'] if r['phase'] == 'development'
                and is_canonical_iso_date(r['date']) and r['date'] < preflight['end_exclusive']]
    selected = sorted(eligible, key=lambda r: (r['date'], r['symbol'], r['ins_code']))[:max_rows]
    run_id = 'RECHECK-' + uuid.uuid4().hex
    folder = settings.DATA_DIR / 'private' / 'price-rechecks' / run_id
    plan_path = private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    if code_identity(folder / 'code') != code:
        raise ValueError('کد هنگام ثبت برنامه بازبینی تغییر کرد')
    plan = dict(version=VERSION, run_id=run_id, registered_at=now_tehran_iso(), code=code,
                preflight=preflight, selected=selected, max_rows=max_rows,
                selection='first chronological development defects among bounded stored details; no returns used',
                network_requested=provider is not None, automatic_repair=False,
                limitations=['Provider returns normalized/validated history, not original HTTP bytes.',
                             'History endpoint may return later dates; only the preregistered development dates are compared.',
                             'Same-source consistency is not independent correctness or historical availability.',
                             'A changed row is a review candidate, not permission to rewrite raw history.'])
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
    db.conn.execute('BEGIN')
    try:
        for row, issue in zip(rows, selected):
            identity = db.conn.execute('SELECT ins_code FROM symbols WHERE id=?', (issue['symbol_id'],)).fetchone()
            current = db.get_daily_prices_df(issue['symbol'], start_date=issue['date'], end_date=issue['date'])
            unchanged = (identity is not None and str(identity['ins_code']) == issue['ins_code'] and len(current) == 1
                         and json_price_row(current.iloc[0]) == issue['raw'])
            row['local_row_unchanged_at_completion'] = bool(unchanged)
    finally:
        db.conn.rollback()
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
                  accepted_for_signals=False, profitability_proven=False,
                  source_consistent_candidates=sum(r['status'] == 'source_changed_consistent'
                                                   and r['local_row_unchanged_at_completion'] for r in rows))
    write_private_report(folder / 'result.json', result)
    return result, folder


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--before', default=None)
    parser.add_argument('--max-rows', type=int, default=5)
    parser.add_argument('--fetch', action='store_true', help='دریافت واقعی؛ بدون این گزینه فقط برنامه ثبت می‌شود')
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    try:
        provider = None
        if args.fetch:
            from data_collector.tsetmc import TsetmcProvider
            provider = TsetmcProvider()
        with DatabaseManager(read_only=True) as db:
            report, folder = run_recheck(db, provider=provider, max_rows=args.max_rows, before=args.before)
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(2, str(exc) + '\n')
    print(json.dumps(dict(path=str(folder), statuses=report['statuses'], automatic_repair=False), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
