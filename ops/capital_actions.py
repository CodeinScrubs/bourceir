"""مطالعه خودبسنده آگهی ثبت سرمایه؛ بدون شبکه، دیتابیس، تعیین حق یا سیگنال."""

import argparse
import hashlib
import json
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

from config import settings
from core.capital_actions import MAX_TEXT_CHARS, primary_capital_registration
from core.codal_documents import public_document_url
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, now_tehran_iso
from ops.codal_documents import load_codal_document
from ops.report_files import private_report_path, read_private_report, write_private_report

VERSION = 'capital-registration-study-v1'
CODE_FILES = ('core/capital_actions.py', 'ops/capital_actions.py', 'core/dividend_evidence.py',
              'core/text.py', 'core/timeutils.py', 'core/codal_documents.py', 'ops/codal_documents.py',
              'ops/report_files.py', 'config/settings.py', 'config/default.yaml',
              'config/production.yaml', 'requirements.lock.txt')
NOTICE_KEYS = ('tracing_no', 'symbol_fa', 'company_name', 'title', 'published_at', 'url')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _clock(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('زمان مطالعه باید منطقه زمانی داشته باشد')
    return stamp.astimezone(TEHRAN_TZ)


def _source(folder, notice):
    if not re.fullmatch(r'CODAL-[a-f0-9]{32}', Path(folder).name):
        raise ValueError('شناسه سند اولیه معتبر نیست')
    text_file = Path(folder) / 'report.txt'
    if not text_file.is_file() or text_file.stat().st_size > 4 * MAX_TEXT_CHARS:
        raise ValueError('سند باید متن محدود پشتیبانی‌شده باشد')
    manifest = load_codal_document(folder)
    if manifest['format'] != 'text' or public_document_url(notice['url']) != manifest['source_url']:
        raise ValueError('آرشیو متن همان نشانی اطلاعیه لازم است')
    return manifest, (Path(folder) / 'report.txt').read_text(encoding='utf-8')


def create_capital_study(*, notice_path, document, root=None, end_exclusive=None):
    cutoff = settings.RESEARCH_CONFIG['holdout_start'] if end_exclusive is None else end_exclusive
    if not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('مرز مطالعه از دوره مهروموم عبور نمی‌کند')
    raw = read_private_report(notice_path)
    notice = {key: raw[key] for key in NOTICE_KEYS}
    original = Path(document).resolve()
    manifest, text = _source(original, notice)
    folder = Path(root or settings.DATA_DIR / 'private' / 'capital-actions') / ('CAPITAL-' + uuid.uuid4().hex)
    private_report_path(folder / 'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    prefix = 'document/' + manifest['snapshot_id'] + '/'
    copy = folder / 'document' / manifest['snapshot_id']
    files = ['manifest.json', manifest['content_file'], *manifest['source_files_sha256']]
    for name in files:
        target = copy / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((original / name).read_bytes())
    copied, copied_text = _source(copy, notice)
    if copied != manifest or copied_text != text:
        raise ValueError('سند هنگام کپی مطالعه تغییر کرد؛ تلاش ناقص محفوظ است')
    inputs = {prefix + name: _sha(copy / name) for name in files}
    inputs['notice.json'] = _sha(write_private_report(folder / 'notice.json', notice))
    code = {}
    for name in CODE_FILES:
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n'))
        code[name] = _sha(target)
    plan = dict(version=VERSION, study_id=folder.name, registered_at=now_tehran_iso(), cutoff=cutoff,
                inputs=inputs, code=code, source_document_snapshot=manifest['snapshot_id'],
                database_read=False, network_requested=False, feature_eligible=False, accepted_for_signals=False)
    plan_hash = _sha(write_private_report(folder / 'plan.json', plan))
    created = now_tehran_iso()
    result = dict(version=VERSION, created_at=created,
        available_at=max(_clock(created), _clock(manifest['available_at'])).isoformat(),
        facts=primary_capital_registration(text, notice, end_exclusive=cutoff))
    result_hash = _sha(write_private_report(folder / 'result.json', result))
    if any(hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != sha
           for name, sha in code.items()):
        raise ValueError('کد هنگام بررسی عوض شد؛ نتیجه کامل منتشر نشد')
    write_private_report(folder / 'manifest.json', dict(version=VERSION, study_id=folder.name,
        plan_sha256=plan_hash, result_sha256=result_hash))
    return dict(folder=str(folder), **review_capital_study(folder))


def review_capital_study(folder):
    folder = Path(folder).resolve()
    manifest = read_private_report(folder / 'manifest.json')
    if (not re.fullmatch(r'CAPITAL-[a-f0-9]{32}', folder.name) or manifest.get('version') != VERSION
            or manifest.get('study_id') != folder.name or _sha(folder / 'plan.json') != manifest['plan_sha256']
            or _sha(folder / 'result.json') != manifest['result_sha256']):
        raise ValueError('هویت یا هش مطالعه سرمایه ناسازگار است')
    plan, result = [read_private_report(folder / name) for name in ('plan.json', 'result.json')]
    if (plan.get('version') != VERSION or plan.get('study_id') != folder.name
            or not is_canonical_iso_date(plan.get('cutoff')) or plan['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or plan.get('database_read') is not False or plan.get('network_requested') is not False
            or plan.get('feature_eligible') is not False or plan.get('accepted_for_signals') is not False
            or set(plan['code']) != set(CODE_FILES)):
        raise ValueError('قرارداد مطالعه سرمایه معتبر نیست')
    for name, sha in {**plan['inputs'], **{'code/' + n: v for n, v in plan['code'].items()}}.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or _sha(path) != sha:
            raise ValueError('هش یا مسیر ورودی/کد مطالعه ناسازگار است')
    notice = read_private_report(folder / 'notice.json')
    if set(notice) != set(NOTICE_KEYS):
        raise ValueError('فراداده مطالعه باید همان فهرست محدود عمومی باشد')
    snapshot = plan['source_document_snapshot']
    if not isinstance(snapshot, str) or not re.fullmatch(r'CODAL-[a-f0-9]{32}', snapshot):
        raise ValueError('هویت سند مرجع معتبر نیست')
    prefix = 'document/' + snapshot + '/'
    source, text = _source(folder / 'document' / snapshot, notice)
    expected_inputs = {'notice.json', prefix + 'manifest.json', prefix + source['content_file'],
                       *(prefix + name for name in source['source_files_sha256'])}
    if set(plan['inputs']) != expected_inputs or plan['source_document_snapshot'] != source['snapshot_id']:
        raise ValueError('فهرست ورودی‌ها یا پیوند سند کامل نیست')
    if (set(result) != {'version', 'created_at', 'available_at', 'facts'} or result['version'] != VERSION
            or result['facts'] != primary_capital_registration(text, notice, end_exclusive=plan['cutoff'])
            or _clock(result['created_at']) < _clock(plan['registered_at'])
            or _clock(result['available_at']) < max(_clock(result['created_at']), _clock(source['available_at']))):
        raise ValueError('بازحساب نتیجه یا زمان دسترسی مطالعه برابر نیست')
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--notice', required=True)
    create.add_argument('--document', required=True)
    review = commands.add_parser('review')
    review.add_argument('--study', required=True)
    args = parser.parse_args(argv)
    try:
        result = (create_capital_study(notice_path=args.notice, document=args.document) if args.command == 'create'
                  else review_capital_study(args.study))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
