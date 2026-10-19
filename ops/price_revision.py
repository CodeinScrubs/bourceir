"""بازبینی و اعمال صریح اصلاح کندل بومی + مسیر کامل معامله؛ فقط داده توسعه بازار."""

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.price_revision import VERSION as CANDIDATE_VERSION
from core.price_revision import revision_candidate
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from data_collector.trade_archive import TradeArchive
from database.migrator import backup_database, discover_migrations
from database.price_revisions import apply_source_revisions
from ops.price_recheck import SOURCE_FILES, _archived_preflight, _local_rows_unchanged
from ops.report_files import private_report_path, read_private_report, write_private_report

VERSION = 'native-price-revision-run-v1'
CODE_FILES = tuple(dict.fromkeys((*SOURCE_FILES, 'ops/price_revision.py', 'core/price_revision.py',
    'core/instrument_history.py', 'core/query_screener.py', 'database/price_recovery.py',
    'database/price_revisions.py', 'database/migrator.py', 'data_collector/trade_archive.py',
    *(p.relative_to(settings.BASE_DIR).as_posix() for _, _, p in discover_migrations()))))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def code_identity(target=None):
    result = {}
    for name in CODE_FILES:
        body = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        result[name] = hashlib.sha256(body).hexdigest()
        if target is not None:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
    return result


def _options(max_rows, offset, include_warmup):
    if (type(max_rows) is not int or not 1 <= max_rows <= 50 or type(offset) is not int
            or not 0 <= offset <= 10000 or type(include_warmup) is not bool):
        raise ValueError('سقف دسته یک تا پنجاه، آغاز صفر تا ده هزار و انتخاب سابقه صریح لازم‌اند')


def _selection(preflight, *, max_rows, offset, include_warmup):
    _options(max_rows, offset, include_warmup)
    phases = {'development', 'prior_history'} if include_warmup else {'development'}
    eligible = sorted((r for r in preflight['price_issues'] if r['phase'] in phases),
                      key=lambda r: (r['date'], r['symbol'], r['ins_code']))
    if offset > len(eligible):
        raise ValueError('آغاز از چارچوب محفوظ بزرگ‌تر است')
    selected = eligible[offset:offset + max_rows]
    return selected, dict(selection_offset=offset, next_offset=offset + len(selected),
        eligible_rows_in_frame=len(eligible), include_warmup=include_warmup,
        sampling_frame_complete=preflight['price_details_truncated'] is False,
        frame_exhausted=offset + len(selected) == len(eligible))


def collect_revision(db, *, from_preflight, provider=None, max_rows=5, offset=0, include_warmup=False):
    _options(max_rows, offset, include_warmup)
    if db.conn.in_transaction or provider is not None and getattr(provider, 'name', None) != 'tsetmc':
        raise ValueError('دریافت فقط از رابط TSETMC و بیرون تراکنش انجام می‌شود')
    code = code_identity()
    preflight = _archived_preflight(db, from_preflight, None)
    selected, selection = _selection(preflight, max_rows=max_rows, offset=offset, include_warmup=include_warmup)
    if not all(_local_rows_unchanged(db, selected)):
        raise ValueError('نمونه یا هویت از زمان گزارش کیفیت تغییر کرده است')
    folder = settings.DATA_DIR / 'private' / 'price-revisions' / ('NATIVEREV-' + uuid.uuid4().hex)
    private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    if code_identity(folder / 'code') != code:
        raise ValueError('کد هنگام ثبت برنامه تغییر کرد')
    preflight_path = write_private_report(folder / 'preflight.json', preflight)
    plan = dict(version=VERSION, run_id=folder.name, registered_at=now_tehran_iso(), code=code,
        cutoff=preflight['end_exclusive'], max_rows=max_rows, selected=selected, **selection,
        preflight_sha256=sha(preflight_path), network_requested=provider is not None,
        selection='fixed archived defects, ascending date,symbol,ins_code; offset; no returns or patterns used',
        automatic_apply=False, accepted_for_signals=False, profitability_proven=False,
        limitations=['Decoded provider JSON is preserved, not original HTTP bytes.',
                     'Both paths share one publisher; independent correctness and historical availability remain unverified.',
                     'Only complete seven-statistic reconciliation allows a revision; missing outcomes are not erased.',
                     'Official closing/reference prices, entitlements, fees and orders are outside this repair.'])
    plan_path = write_private_report(folder / 'plan.json', plan)
    artifacts = {'plan.json': sha(plan_path), 'preflight.json': sha(preflight_path)}
    archive, items = TradeArchive(folder / 'trades'), []
    for index, issue in enumerate(selected):
        observation, error = None, None
        if provider is not None:
            try:
                observation = provider.get_dated_daily_observation(issue['ins_code'], issue['date'])
                body = json.dumps(observation, allow_nan=False).encode()
                if len(body) > 262144:
                    raise ValueError('daily observation exceeded byte cap')
            except (NotImplementedError, ValueError, TypeError, OSError) as exc:
                observation, error = None, type(exc).__name__
        candidate = revision_candidate(issue, observation, None, cutoff=plan['cutoff'])
        trade_path, trade_observation = None, None
        if provider is not None and candidate['status'] == 'source_candidate_needs_trades':
            record, path = archive.capture(provider, issue['ins_code'], issue['date'])
            trade_observation, trade_path = record.get('observation'), path.relative_to(folder).as_posix()
            artifacts[trade_path] = sha(path)
            candidate = revision_candidate(issue, observation, trade_observation, cutoff=plan['cutoff'])
            if trade_observation is None:
                candidate['status'] = 'unverified_full_trade_path'
        item = dict(issue=issue, daily_observation=observation, daily_error_type=error,
                    trade_archive=trade_path, candidate=candidate)
        name = f'observation-{index:03d}.json'
        artifacts[name] = sha(write_private_report(folder / name, item))
        items.append(item)
    for item, unchanged in zip(items, _local_rows_unchanged(db, selected)):
        item['local_row_unchanged_at_completion'] = unchanged
    if code_identity() != code or any(sha(folder / name) != digest for name, digest in artifacts.items()):
        raise ValueError('کد یا شاهد حین دریافت تغییر کرده است')
    result = dict(version=VERSION, run_id=folder.name, completed_at=now_tehran_iso(), **selection,
        selected_rows=len(selected), observations=items, artifacts_sha256=artifacts,
        statuses=dict(Counter(item['candidate']['status'] for item in items)),
        daily_requests=len(selected) if provider is not None else 0,
        trade_requests=sum(item['trade_archive'] is not None for item in items),
        current_global_quality_verified=False, applied=False, accepted_for_signals=False, profitability_proven=False)
    write_private_report(folder / 'result.json', result)
    return result, folder


def verified_revision(folder):
    folder = private_report_path(Path(folder) / 'plan.json').parent
    if not re.fullmatch(r'NATIVEREV-[a-f0-9]{32}', folder.name):
        raise ValueError('شناسه پرونده اصلاح نامعتبر است')
    plan, result = read_private_report(folder / 'plan.json'), read_private_report(folder / 'result.json')
    if (plan.get('version') != VERSION or result.get('version') != VERSION or plan.get('run_id') != folder.name
            or result.get('run_id') != folder.name or plan.get('code') != code_identity()
            or not is_canonical_iso_date(plan.get('cutoff')) or plan['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('نسخه، کد یا مرز اصلاح با وضعیت فعلی سازگار نیست')
    preflight = read_private_report(folder / 'preflight.json')
    if (preflight.get('end_exclusive') != plan['cutoff'] or plan['preflight_sha256'] != sha(folder / 'preflight.json')):
        raise ValueError('چارچوب محفوظ تغییر کرده است')
    selected, selection = _selection(preflight, max_rows=plan['max_rows'], offset=plan['selection_offset'],
                                     include_warmup=plan['include_warmup'])
    if (selected != plan['selected'] or len(selected) != result['selected_rows'] or len(selected) != len(result['observations'])
            or any(plan.get(k) != v or result.get(k) != v for k, v in selection.items())):
        raise ValueError('انتخاب اصلاح از چارچوب محفوظ بازتولید نمی‌شود')
    expected, candidates = {'plan.json', 'preflight.json'}, []
    archive = TradeArchive(folder / 'trades')
    for index, (issue, item) in enumerate(zip(selected, result['observations'])):
        name = f'observation-{index:03d}.json'
        expected.add(name)
        stored = read_private_report(folder / name)
        if stored != {k: v for k, v in item.items() if k != 'local_row_unchanged_at_completion'} or item['issue'] != issue:
            raise ValueError('نسخه مستقل مشاهده تغییر کرده است')
        observation, trade = item['daily_observation'], None
        if item['trade_archive'] is not None:
            trade_path = item['trade_archive']
            if not re.fullmatch(r'trades/[0-9]+_\d{4}-\d{2}-\d{2}_[a-f0-9]{64}\.json\.gz', trade_path):
                raise ValueError('مسیر آرشیو ریزمعاملات معتبر نیست')
            record = archive.load(folder / trade_path)
            if record['ins_code'] != issue['ins_code'] or record['trade_date'] != issue['date']:
                raise ValueError('آرشیو معامله به نمونه دیگری تعلق دارد')
            trade = record.get('observation')
            expected.add(trade_path)
        candidate = revision_candidate(issue, observation, trade, cutoff=plan['cutoff'])
        if item['trade_archive'] is not None and trade is None:
            candidate['status'] = 'unverified_full_trade_path'
        if candidate != item['candidate']:
            raise ValueError('اصلاح از منبع و ریزمعاملات محفوظ بازتولید نمی‌شود')
        if candidate['status'] == 'verified_native_trade_revision':
            if item['local_row_unchanged_at_completion'] is not True:
                raise ValueError('نمونه هنگام دریافت تغییر کرده است؛ پرونده تازه لازم است')
            candidates.append((issue, candidate))
    if (set(result['artifacts_sha256']) != expected or
            result['statuses'] != dict(Counter(item['candidate']['status'] for item in result['observations']))):
        raise ValueError('فهرست شاهدها یا شمار وضعیت‌ها تغییر کرده است')
    for name, digest in result['artifacts_sha256'].items():
        if sha(folder / name) != digest:
            raise ValueError('هش شاهد اصلاح تغییر کرده است')
    for name, digest in plan['code'].items():
        if sha(folder / 'code' / name) != digest:
            raise ValueError('کد محفوظ اصلاح تغییر کرده است')
    return plan, result, candidates, folder


def apply_revision(db, folder, *, review_note):
    if not isinstance(review_note, str) or not 10 <= len(review_note.strip()) <= 2000 or db.conn.in_transaction:
        raise ValueError('توضیح بررسی ده تا دوهزار نویسه و اتصال بدون تراکنش لازم است')
    plan, result, candidates, folder = verified_revision(folder)
    already = db.conn.execute('SELECT count(*) FROM price_recoveries WHERE run_id=?', (plan['run_id'],)).fetchone()[0]
    backup = backup_database(db.conn, db.db_path) if len(candidates) > already else None
    evidence = dict(version=CANDIDATE_VERSION, plan_sha256=sha(folder / 'plan.json'), result_sha256=sha(folder / 'result.json'),
                    review_note=review_note.strip(), artifacts_sha256=result['artifacts_sha256'], code=plan['code'],
                    source_independently_verified=False, historical_availability_verified=False)
    applied = apply_source_revisions(db, plan['run_id'], candidates, cutoff=plan['cutoff'], evidence=evidence,
                                     before_commit=lambda: verified_revision(folder))
    receipt = dict(version=VERSION, run_id=folder.name, applied_at=now_tehran_iso(), **applied,
        backup_name=backup.name if backup else None, feature_rebuild_required=bool(applied['applied']),
        old_research_rewritten=False, accepted_for_signals=False, profitability_proven=False)
    path = write_private_report(folder / ('application-' + uuid.uuid4().hex + '.json'), receipt)
    return receipt, path


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--from-preflight')
    mode.add_argument('--review-run')
    mode.add_argument('--apply-run')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--max-rows', type=int, default=5)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--include-warmup', action='store_true')
    parser.add_argument('--review-note')
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    provider = None
    try:
        if args.review_run or args.apply_run:
            if args.fetch or args.offset or args.include_warmup:
                raise ValueError('بازبینی/اعمال پرونده دریافت یا انتخاب تازه ندارد')
            _, result, candidates, folder = verified_revision(args.review_run or args.apply_run)
            report = dict(statuses=result['statuses'], verified_candidates=len(candidates), accepted_for_signals=False)
            path = folder / 'result.json'
            if args.apply_run:
                if not settings.DB_PATH.is_file():
                    raise ValueError('دیتابیس موجود برای اعمال لازم است')
                with DatabaseManager() as db:
                    report, path = apply_revision(db, folder, review_note=args.review_note)
        else:
            if args.fetch:
                from data_collector.tsetmc import TsetmcProvider
                provider = TsetmcProvider()
            with DatabaseManager(read_only=True) as db:
                report, path = collect_revision(db, from_preflight=args.from_preflight, provider=provider,
                    max_rows=args.max_rows, offset=args.offset, include_warmup=args.include_warmup)
        print(json.dumps(dict(path=str(path), statuses=report.get('statuses'), applied=report.get('applied'),
                              verified_candidates=report.get('verified_candidates')), ensure_ascii=False))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        parser.exit(2, f'اصلاح کامل نشد: {type(exc).__name__}: {exc}\n')
    finally:
        if provider is not None:
            provider.session.close()


if __name__ == '__main__':
    raise SystemExit(main())
