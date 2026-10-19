"""تطبیق فقط‌خواندنی دو آرشیو واقعی؛ بدون دریافت شبکه، دیتابیس یا ثبت وجه."""

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

from config import settings
from core.codal_documents import public_document_url
from core.dividend_reconciliation import VERSION, primary_annual_dividend, reconcile_dividend
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from ops.codal_documents import load_codal_document
from ops.dividend_evidence import load_dividend_evidence
from ops.report_files import private_report_path, read_private_report, write_private_report

CODE_FILES = ('core/dividend_reconciliation.py', 'ops/dividend_reconciliation.py',
              'core/dividend_evidence.py', 'ops/dividend_evidence.py', 'core/codal_documents.py',
              'ops/codal_documents.py', 'core/timeutils.py', 'core/text.py', 'config/settings.py',
              'ops/report_files.py', 'requirements.lock.txt')
NOTICE_FIELDS = ('tracing_no', 'symbol_fa', 'company_name', 'title', 'published_at', 'url')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _sources(notice, document, study):
    document, study = Path(document).resolve(), Path(study).resolve()
    for folder in (document, study):
        private_report_path(folder / 'manifest.json', forbidden_paths=(settings.DB_PATH,))
    manifest = load_codal_document(document)
    source = load_dividend_evidence(study)
    if manifest['format'] != 'text' or manifest['source_url'] != public_document_url(notice['url']):
        raise ValueError('متن همان پیوند رسمی اطلاعیه لازم است')
    body_path = document / manifest['content_file']
    body = body_path.read_bytes()
    if hashlib.sha256(body).hexdigest() != manifest['content_sha256']:
        raise ValueError('متن پس از کنترل آرشیو تغییر کرد')
    selected = [case for case in source['cases'] if normalize_symbol(case['request']['symbol'])
                == normalize_symbol(notice['symbol_fa'])]
    if len(selected) != 1 or selected[0]['status'] != 'observed_unverified':
        raise ValueError('یک پاسخ قابل خواندن برای همان نماد لازم است')
    native_manifest = read_private_report(study / 'manifest.json')
    paths = [document / name for name in ('manifest.json', manifest['content_file'])]
    paths += [document / name for name in manifest['source_files_sha256']]
    paths += [study / name for name in ('manifest.json', 'plan.json', 'result.json')]
    paths += [study / name for name in native_manifest['observations']]
    paths += [study / 'code' / name for name in native_manifest['code']]
    hashes = {str(path): _sha(path) for path in paths}
    return (body.decode('utf-8'), selected[0]['assessment']['rows'], source['plan']['cutoff'], hashes,
            max((manifest['available_at'], source['completed_at']), key=datetime.fromisoformat))


def _facts(text, rows, cutoff, notice):
    return reconcile_dividend(primary_annual_dividend(text, notice, end_exclusive=cutoff), rows)


def compare_archived_dividend(*, notice_path, document, study):
    raw_notice = read_private_report(notice_path)
    notice = {key: raw_notice[key] for key in NOTICE_FIELDS}
    text, rows, cutoff, hashes, available = _sources(notice, document, study)
    folder = settings.DATA_DIR / 'private' / 'dividend-reconciliation' / ('CORR-' + uuid.uuid4().hex)
    code = {name: (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') for name in CODE_FILES}
    write_private_report(folder / 'notice.json', notice)
    plan = dict(version=VERSION, registered_at=now_tehran_iso(), document=str(Path(document).resolve()),
                dividend_study=str(Path(study).resolve()), cutoff=cutoff, source_files_sha256=hashes,
                scope='one stated issuer/annual meeting; all matching native rows; not historical source coverage')
    write_private_report(folder / 'plan.json', plan)
    created = now_tehran_iso()
    result = dict(version=VERSION, created_at=created,
                  available_at=max((created, available), key=datetime.fromisoformat),
                  facts=_facts(text, rows, cutoff, notice))
    if any(_sha(path) != value for path, value in hashes.items()):
        raise ValueError('یکی از ورودی‌های محفوظ هنگام تطبیق تغییر کرد')
    if code != {name: (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') for name in CODE_FILES}:
        raise ValueError('کد هنگام تطبیق تغییر کرد')
    for name, body in code.items():
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    write_private_report(folder / 'result.json', result)
    files = ['notice.json', 'plan.json', 'result.json', *('code/' + name for name in CODE_FILES)]
    write_private_report(folder / 'manifest.json', dict(version=VERSION, files={name: _sha(folder / name) for name in files}))
    return dict(folder=str(folder), **load_dividend_comparison(folder))


def load_dividend_comparison(folder):
    folder = Path(folder).resolve()
    manifest = read_private_report(folder / 'manifest.json')
    files = {'notice.json', 'plan.json', 'result.json', *('code/' + name for name in CODE_FILES)}
    if manifest['version'] != VERSION or set(manifest['files']) != files:
        raise ValueError('نسخه یا فهرست آرشیو تطبیق ناسازگار است')
    for name, digest in manifest['files'].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or _sha(path) != digest:
            raise ValueError('هش محتوای تطبیق یا کد محفوظ ناسازگار است')
    notice, plan, result = [read_private_report(folder / name) for name in ('notice.json', 'plan.json', 'result.json')]
    if (plan['version'] != VERSION or result['version'] != VERSION
            or set(result) != {'version', 'created_at', 'available_at', 'facts'}):
        raise ValueError('قرارداد نتیجه تطبیق ناسازگار است')
    text, rows, cutoff, hashes, available = _sources(notice, plan['document'], plan['dividend_study'])
    if (cutoff != plan['cutoff'] or hashes != plan['source_files_sha256']
            or result['facts'] != _facts(text, rows, cutoff, notice)):
        raise ValueError('بازحساب تطبیق با آرشیو اولیه برابر نیست')
    clocks = [datetime.fromisoformat(value) for value in
              (plan['registered_at'], result['created_at'], result['available_at'], available)]
    if (any(t.tzinfo is None or t.utcoffset() is None for t in clocks)
            or clocks[1] < clocks[0] or clocks[2] < max(clocks[1], clocks[3])):
        raise ValueError('زمان دسترسی تطبیق به گذشته برده شده است')
    if any(_sha(path) != digest for path, digest in hashes.items()):
        raise ValueError('ورودی هنگام بازبینی تغییر کرد')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    compare = commands.add_parser('compare')
    compare.add_argument('--notice', required=True)
    compare.add_argument('--document', required=True)
    compare.add_argument('--dividend-study', required=True)
    review = commands.add_parser('review')
    review.add_argument('--study', required=True)
    args = parser.parse_args(argv)
    try:
        result = (load_dividend_comparison(args.study) if args.command == 'review' else
                  compare_archived_dividend(notice_path=args.notice, document=args.document, study=args.dividend_study))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    # خروجی هدایت‌شده در Windows هم UTF-8 است؛ locale نباید پس از ساخت آرشیو چاپ را خراب کند.
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
