"""برنامه مستقل بازیابی قیمت گمشده از ریزمعاملات؛ اعمال صریح با پشتیبان و رسید."""

import argparse
import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.price_recovery import RECOVERABLE, recovery_candidate
from core.timeutils import now_tehran_iso
from data_collector.trade_archive import TradeArchive
from database.migrator import backup_database, discover_migrations
from database.price_recovery import apply_recoveries
from ops.price_recheck import SOURCE_FILES, _archived_preflight, _local_rows_unchanged
from ops.report_files import private_report_path, read_private_report, write_private_report

VERSION = 'price-recovery-run-v2'
CODE_FILES = tuple(dict.fromkeys((*SOURCE_FILES, 'ops/price_recovery.py', 'core/price_recovery.py',
    'core/query_screener.py', 'data_collector/trade_archive.py', 'database/price_recovery.py',
    'database/migrator.py', *(path.relative_to(settings.BASE_DIR).as_posix() for _, _, path in discover_migrations()))))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def code_identity(target=None):
    hashes = {}
    for name in CODE_FILES:
        content = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        hashes[name] = hashlib.sha256(content).hexdigest()
        if target is not None:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
    return hashes


def _validate_selection_options(max_rows, offset, include_warmup):
    if type(max_rows) is not int or not 1 <= max_rows <= 50:
        raise ValueError('سقف نمونه بازیابی باید صحیح و بین یک و پنجاه باشد')
    if type(offset) is not int or not 0 <= offset <= 10000 or type(include_warmup) is not bool:
        raise ValueError('آغاز دسته باید صحیح بین صفر و ده هزار و انتخاب سابقه صریح باشد')


def _selection(preflight, *, max_rows, offset, include_warmup):
    _validate_selection_options(max_rows, offset, include_warmup)
    phases = {'development', 'prior_history'} if include_warmup else {'development'}
    eligible = sorted((row for row in preflight['price_issues'] if row['phase'] in phases
                       and row['reasons'] and set(row['reasons']) <= RECOVERABLE.keys()),
                      key=lambda row: (row['date'], row['symbol'], row['ins_code']), reverse=True)
    if offset > len(eligible):
        raise ValueError('آغاز دسته از تعداد موارد واجد شرایط در همین گزارش بیشتر است')
    selected = eligible[offset:offset + max_rows]
    next_offset = offset + len(selected)
    return selected, dict(selection_offset=offset, next_offset=next_offset,
                          eligible_rows_in_frame=len(eligible), frame_issue_rows=len(preflight['price_issues']),
                          include_warmup=include_warmup,
                          sampling_frame_complete=preflight.get('price_details_truncated') is False,
                          frame_exhausted=next_offset == len(eligible),
                          selected_phases=dict(Counter(row['phase'] for row in selected)))


def collect_recovery(db, *, from_preflight, provider=None, max_rows=5, offset=0, include_warmup=False):
    # اعتبار ورودی پیش از هر دسترسی فایل یا شبکه؛ انتخاب فقط از چارچوب ثابت محفوظ است.
    _validate_selection_options(max_rows, offset, include_warmup)
    if db.conn.in_transaction or provider is not None and getattr(provider, 'name', None) != 'tsetmc':
        raise ValueError('بازیابی فقط از تأمین‌کننده TSETMC و بیرون از تراکنش شبکه انجام می‌شود')
    code = code_identity()
    preflight = _archived_preflight(db, from_preflight, None)
    selected, selection = _selection(preflight, max_rows=max_rows, offset=offset, include_warmup=include_warmup)
    if not all(_local_rows_unchanged(db, selected)):
        raise ValueError('هویت یا داده نمونه پس از گزارش کیفیت تغییر کرده است')
    run_id = 'RECOVER-' + uuid.uuid4().hex
    folder = settings.DATA_DIR / 'private' / 'price-recovery' / run_id
    private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    if code_identity(folder / 'code') != code:
        raise ValueError('کد هنگام ثبت برنامه بازیابی تغییر کرد')
    report_path = write_private_report(folder / 'preflight.json', preflight)
    plan = dict(version=VERSION, run_id=run_id, registered_at=now_tehran_iso(), code=code, **selection,
                cutoff=preflight['end_exclusive'], selected=selected, max_rows=max_rows,
                preflight_sha256=sha(report_path), preflight_basis='archived_sampling_frame_only',
                selection='missing open/last in fixed preflight frame; descending date,symbol,ins_code; explicit offset; no returns used',
                network_requested=provider is not None, current_global_quality_verified=False,
                automatic_apply=False, accepted_for_signals=False,
                limitations=['Decoded provider JSON is preserved, not original HTTP bytes.',
                             'Surviving-trade endpoints may differ from a canceled original opening/last trade.',
                             'Full same-source reconciliation is not independent correctness or historical availability.',
                             'Reference, corporate entitlements and other fields are not inferred.'])
    plan_path = write_private_report(folder / 'plan.json', plan)
    artifacts = {'preflight.json': sha(report_path), 'plan.json': sha(plan_path)}
    archive, observations = TradeArchive(folder / 'trades'), []
    for index, issue in enumerate(selected):
        record, path = archive.capture(provider, issue['ins_code'], issue['date'])
        candidate = recovery_candidate(issue, record.get('observation'), cutoff=plan['cutoff'])
        item = dict(issue=issue, candidate=candidate, archive=path.relative_to(folder).as_posix(),
                    archive_sha256=sha(path), archive_status=record['status'])
        observations.append(item)
        stored = write_private_report(folder / f'observation-{index:03d}.json', item)
        artifacts[stored.name] = sha(stored)
        artifacts[item['archive']] = item['archive_sha256']
    for item, unchanged in zip(observations, _local_rows_unchanged(db, selected)):
        item['local_row_unchanged_at_completion'] = unchanged
    if code_identity() != code or any(sha(folder / name) != digest for name, digest in artifacts.items()):
        raise ValueError('کد یا شواهد محفوظ هنگام بازیابی تغییر کرد')
    for name, digest in code.items():
        if sha(folder / 'code' / name) != digest:
            raise ValueError('کپی کد بازیابی تغییر کرد')
    result = dict(version=VERSION, run_id=run_id, completed_at=now_tehran_iso(), observations=observations, **selection,
                  artifacts_sha256=artifacts, statuses=dict(Counter(item['candidate']['status'] for item in observations)),
                  selected_rows=len(selected), requests_made=len(selected) if provider is not None else 0,
                  current_global_quality_verified=False, applied=False,
                  accepted_for_signals=False, profitability_proven=False)
    write_private_report(folder / 'result.json', result)
    return result, folder


def verified_recovery(folder):
    folder = private_report_path(Path(folder) / 'plan.json').parent
    if not re.fullmatch(r'RECOVER-[a-f0-9]{32}', folder.name):
        raise ValueError('شناسه پوشه بازیابی معتبر نیست')
    plan, result = read_private_report(folder / 'plan.json'), read_private_report(folder / 'result.json')
    if (plan.get('version') != VERSION or result.get('version') != VERSION or plan.get('run_id') != folder.name
            or result.get('run_id') != folder.name or plan.get('code') != code_identity()
            or plan.get('cutoff') > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('قرارداد، کد یا مرز بازیابی با وضعیت فعلی سازگار نیست')
    observations = result['observations']
    selected, selection = _selection(read_private_report(folder / 'preflight.json'), max_rows=plan['max_rows'],
                                     offset=plan['selection_offset'], include_warmup=plan['include_warmup'])
    if plan['selected'] != selected or any(plan.get(key) != value or result.get(key) != value
                                          for key, value in selection.items()):
        raise ValueError('دسته بازیابی با ترتیب و چارچوب محفوظ بازتولید نمی‌شود')
    if len(observations) != len(plan['selected']) or result['selected_rows'] != len(observations):
        raise ValueError('نمونه بازیابی ناقص یا تغییرکرده است')
    expected = {'plan.json', 'preflight.json'}
    candidates, archive = [], TradeArchive(folder / 'trades')
    for index, (item, issue) in enumerate(zip(observations, plan['selected'])):
        name = item['archive']
        if not re.fullmatch(r'trades/[0-9]+_\d{4}-\d{2}-\d{2}_[a-f0-9]{64}\.json\.gz', name):
            raise ValueError('مسیر شاهد بازیابی نامعتبر است')
        expected.update((name, f'observation-{index:03d}.json'))
        stored = read_private_report(folder / f'observation-{index:03d}.json')
        if stored != {key: value for key, value in item.items() if key != 'local_row_unchanged_at_completion'} or item['issue'] != issue:
            raise ValueError('مشاهده محفوظ با نتیجه بازیابی ناسازگار است')
        record = archive.load(folder / name)
        candidate = recovery_candidate(issue, record.get('observation'), cutoff=plan['cutoff'])
        if candidate != item['candidate'] or item['archive_sha256'] != sha(folder / name):
            raise ValueError('پیشنهاد از شاهد محفوظ بازتولید نمی‌شود')
        if candidate['status'] == 'verified_same_source_candidate':
            if item['local_row_unchanged_at_completion'] is not True:
                raise ValueError('نمونه حین دریافت تغییر کرده است؛ برنامه تازه لازم است')
            candidates.append((issue, candidate))
    if set(result['artifacts_sha256']) != expected or plan['preflight_sha256'] != sha(folder / 'preflight.json'):
        raise ValueError('فهرست شواهد بازیابی ناقص یا اضافه است')
    for name, digest in result['artifacts_sha256'].items():
        if sha(folder / name) != digest:
            raise ValueError('هش شاهد بازیابی تغییر کرده است')
    for name, digest in plan['code'].items():
        if sha(folder / 'code' / name) != digest:
            raise ValueError('هش کد محفوظ بازیابی تغییر کرده است')
    return plan, result, candidates, folder


def apply_recovery_run(db, folder, *, review_note):
    if not isinstance(review_note, str) or not 10 <= len(review_note.strip()) <= 2000:
        raise ValueError('توضیح بررسی شواهد با طول ده تا دوهزار نویسه لازم است')
    plan, result, candidates, folder = verified_recovery(folder)
    if db.conn.in_transaction:
        raise ValueError('پیش از پشتیبان‌گیری تراکنش باز مجاز نیست')
    already = db.conn.execute('SELECT count(*) FROM price_recoveries WHERE run_id=?', (plan['run_id'],)).fetchone()[0]
    backup = backup_database(db.conn, db.db_path) if len(candidates) > already else None
    evidence = dict(plan_sha256=sha(folder / 'plan.json'), result_sha256=sha(folder / 'result.json'),
                    review_note=review_note.strip(), source_independently_verified=False, historical_availability_verified=False,
                    artifacts=result['artifacts_sha256'], code=plan['code'])
    applied = apply_recoveries(db, plan['run_id'], candidates, cutoff=plan['cutoff'], evidence=evidence,
                               before_commit=lambda: verified_recovery(folder))
    report = dict(version=VERSION, run_id=plan['run_id'], applied_at=now_tehran_iso(), **applied,
                  backup_name=backup.name if backup else None, accepted_for_signals=False, profitability_proven=False,
                  feature_rebuild_required=bool(applied['applied']), old_research_rewritten=False)
    path = write_private_report(folder / ('application-' + uuid.uuid4().hex + '.json'), report)
    return report, path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--from-preflight')
    mode.add_argument('--apply-run')
    parser.add_argument('--fetch', action='store_true')
    parser.add_argument('--max-rows', type=int, default=5)
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--include-warmup', action='store_true')
    parser.add_argument('--review-note', default=None)
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    try:
        if args.apply_run:
            if args.fetch or args.offset or args.include_warmup:
                raise ValueError('اعمال شاهد محفوظ دریافت یا انتخاب دسته تازه ندارد')
            verified_recovery(args.apply_run)
            with DatabaseManager() as db:
                report, path = apply_recovery_run(db, args.apply_run, review_note=args.review_note)
        else:
            provider = None
            try:
                if args.fetch:
                    from data_collector.tsetmc import TsetmcProvider
                    provider = TsetmcProvider()
                with DatabaseManager(read_only=True) as db:
                    report, path = collect_recovery(db, from_preflight=args.from_preflight,
                                                    provider=provider, max_rows=args.max_rows,
                                                    offset=args.offset, include_warmup=args.include_warmup)
            finally:
                if provider is not None:
                    provider.session.close()
    except (ValueError, OSError, sqlite3.Error, KeyError, TypeError) as exc:
        parser.exit(2, f'بازیابی کامل نشد: {type(exc).__name__}: {exc}\n')
    print(json.dumps(dict(path=str(path), statuses=report.get('statuses'), applied=report.get('applied')), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
