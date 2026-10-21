"""Archive/review a legacy price CSV without importing it into the market DB."""
import argparse
import hashlib
import json
import sys
import uuid
from pathlib import Path

from config import settings
from core.legacy_price_csv import inspect_legacy_price_csv
from core.timeutils import now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report

VERSION = 'csv-price-audit-run-v1'
CODE_FILES = ('core/legacy_price_csv.py', 'core/market_rules.py', 'core/text.py',
              'core/timeutils.py', 'ops/csv_price_audit.py', 'ops/report_files.py')


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _code():
    return {name:_sha((settings.BASE_DIR / name).read_bytes()) for name in CODE_FILES}


def capture_csv(source, *, symbol, unit, out=None):
    source = Path(source).resolve()
    if source.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('سقف CSV در این ممیزی ۱۶ مگابایت است')
    body = source.read_bytes()
    target = Path(out) if out else settings.DATA_DIR / 'private' / f'csv-price-audit-{uuid.uuid4().hex}'
    report_path = private_report_path(target / 'report.json', forbidden_paths=(source, settings.DB_PATH))
    target = report_path.parent
    target.mkdir(parents=True, exist_ok=False)
    plan = dict(version=VERSION, created_at=now_tehran_iso(), source_name=source.name,
        source_sha256=_sha(body), symbol=symbol, unit=unit, code_sha256=_code(),
        accepted_for_signals=False, database_mutated=False)
    write_private_report(target / 'plan.json', plan)
    (target / 'source.csv').write_bytes(body)
    try:
        frame, summary = inspect_legacy_price_csv(body, symbol=symbol, unit=unit)
        normalized = frame.to_csv(index=False, lineterminator='\n').encode('utf-8')
        (target / 'normalized.csv').write_bytes(normalized)
        report = dict(version=VERSION, status='complete', generated_at=now_tehran_iso(),
            summary=summary, normalized_sha256=_sha(normalized), accepted_for_signals=False)
    except (ValueError, TypeError, UnicodeError) as exc:
        write_private_report(report_path, dict(version=VERSION, status='failed',
            generated_at=now_tehran_iso(), error=str(exc), accepted_for_signals=False))
        raise
    write_private_report(report_path, report)
    return report_path


def review_csv(path):
    """Recompute using matching code and preserved input, not saved PASS flags."""
    report_path = private_report_path(Path(path) / 'report.json')
    target = report_path.parent
    plan, report = read_private_report(target / 'plan.json'), read_private_report(report_path)
    if plan.get('version') != VERSION or report.get('version') != VERSION or report.get('status') != 'complete':
        raise ValueError('آرشیو کامل این قرارداد موجود نیست')
    if plan.get('code_sha256') != _code():
        raise ValueError('کد ممیزی تغییر کرده؛ بازبینی آرشیو با همان نسخه کد لازم است')
    body = (target / 'source.csv').read_bytes()
    if _sha(body) != plan.get('source_sha256'):
        raise ValueError('اثر انگشت منبع محفوظ ناسازگار است')
    frame, summary = inspect_legacy_price_csv(body, symbol=plan['symbol'], unit=plan['unit'])
    normalized = frame.to_csv(index=False, lineterminator='\n').encode('utf-8')
    if (summary != report.get('summary') or _sha(normalized) != report.get('normalized_sha256')
            or normalized != (target / 'normalized.csv').read_bytes()):
        raise ValueError('محاسبه دوباره با گزارش یا ردیف‌های محفوظ برابر نیست')
    return dict(version=VERSION, replay_verified=True, summary=summary,
                database_mutated=False, accepted_for_signals=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    capture = commands.add_parser('capture')
    capture.add_argument('source', type=Path)
    capture.add_argument('--symbol', required=True)
    capture.add_argument('--unit', choices=['Rial'], required=True)
    capture.add_argument('--out', type=Path)
    review = commands.add_parser('review')
    review.add_argument('archive', type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == 'capture':
            result = dict(report=str(capture_csv(args.source, symbol=args.symbol, unit=args.unit, out=args.out)))
        else:
            result = review_csv(args.archive)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
