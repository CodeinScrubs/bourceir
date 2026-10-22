"""Self-contained issuer funding study: no DB/network, holder entitlement or order submission."""

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

from config import settings
from core.rights_evidence import primary_rights_registration
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.capital_actions import NOTICE_KEYS, _clock, _source
from ops.report_files import private_report_path, read_private_report, write_private_report

VERSION = 'rights-registration-study-v1'
SEMANTIC_FILES = ('core/rights_evidence.py', 'core/capital_actions.py', 'core/dividend_evidence.py',
    'core/text.py', 'core/timeutils.py', 'core/codal_documents.py', 'ops/capital_actions.py',
    'ops/codal_documents.py', 'ops/rights_registration.py', 'ops/report_files.py')
CODE_FILES = (*SEMANTIC_FILES, 'config/settings.py', 'config/default.yaml',
              'config/production.yaml', 'requirements.lock.txt', 'pyproject.toml')
FLAGS = ('database_read', 'database_writes', 'network_requested', 'account_data_read',
         'feature_eligible', 'accepted_for_signals', 'profitability_proven')


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _code(name):
    return (settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')


def create_rights_study(*, notice_path, document, root=None, end_exclusive=None):
    cutoff = settings.RESEARCH_CONFIG['holdout_start'] if end_exclusive is None else end_exclusive
    if not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('مرز مطالعه از دوره مهروموم عبور نمی‌کند')
    raw = read_private_report(notice_path)
    notice = {key: raw[key] for key in NOTICE_KEYS}
    original = Path(document).resolve()
    source, text = _source(original, notice)
    folder = Path(root or settings.DATA_DIR/'private/rights-registrations')/('RIGHTS-'+uuid.uuid4().hex)
    private_report_path(folder/'plan.json', forbidden_paths=(settings.DB_PATH,))
    folder.mkdir(parents=True, exist_ok=False)
    copy = folder/'document'/source['snapshot_id']
    files = ['manifest.json', source['content_file'], *source['source_files_sha256']]
    for name in files:
        target = copy/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((original/name).read_bytes())
    copied, copied_text = _source(copy, notice)
    if copied != source or copied_text != text:
        raise ValueError('منبع هنگام ساخت مطالعه تغییر کرده است')
    prefix = 'document/'+source['snapshot_id']+'/'
    inputs = {prefix+name: _sha(copy/name) for name in files}
    inputs['notice.json'] = _sha(write_private_report(folder/'notice.json', notice))
    code = {}
    for name in CODE_FILES:
        target = folder/'code'/name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_code(name))
        code[name] = _sha(target)
    plan = dict(version=VERSION, study_id=folder.name, registered_at=now_tehran_iso(), cutoff=cutoff,
        inputs=inputs, code=code, source_document_snapshot=source['snapshot_id'], **dict.fromkeys(FLAGS, False))
    plan_hash = _sha(write_private_report(folder/'plan.json', plan))
    created = now_tehran_iso()
    if not _clock(source['available_at']) <= _clock(plan['registered_at']) <= _clock(created):
        raise ValueError('منبع با زمان دسترسی آینده یا ترتیب ثبت ناسازگار معتبر نیست')
    result = dict(version=VERSION, created_at=created,
        available_at=max(_clock(created), _clock(source['available_at'])).isoformat(),
        facts=primary_rights_registration(text, notice, end_exclusive=cutoff))
    result_hash = _sha(write_private_report(folder/'result.json', result))
    if any(hashlib.sha256(_code(name)).hexdigest() != value for name, value in code.items()):
        raise ValueError('کد هنگام ساخت مطالعه عوض شده است')
    write_private_report(folder/'manifest.json', dict(version=VERSION, study_id=folder.name,
        plan_sha256=plan_hash, result_sha256=result_hash))
    return dict(folder=str(folder), **review_rights_study(folder))


def review_rights_study(folder):
    folder = Path(folder).resolve()
    manifest = read_private_report(folder/'manifest.json')
    if (not re.fullmatch(r'RIGHTS-[a-f0-9]{32}', folder.name) or manifest.get('version') != VERSION
            or manifest.get('study_id') != folder.name or _sha(folder/'plan.json') != manifest['plan_sha256']
            or _sha(folder/'result.json') != manifest['result_sha256']):
        raise ValueError('هویت و هش مطالعه برابر نیست')
    plan = read_private_report(folder/'plan.json')
    result = read_private_report(folder/'result.json')
    if (plan.get('version') != VERSION or plan.get('study_id') != folder.name
            or not is_canonical_iso_date(plan.get('cutoff')) or plan['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or any(plan.get(flag) is not False for flag in FLAGS) or set(plan['code']) != set(CODE_FILES)):
        raise ValueError('قرارداد مطالعه و مرز داده معتبر نیست')
    for name, expected in {**plan['inputs'], **{'code/'+n: v for n, v in plan['code'].items()}}.items():
        path = (folder/name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or _sha(path) != expected:
            raise ValueError('هش یا مسیر ورودی و کد ناسازگار است')
    if any(hashlib.sha256(_code(name)).hexdigest() != plan['code'][name] for name in SEMANTIC_FILES):
        raise ValueError('منطق جاری با مفسر محفوظ برابر نیست؛ بازتفسیر صریح لازم است')
    notice = read_private_report(folder/'notice.json')
    if set(notice) != set(NOTICE_KEYS):
        raise ValueError('فراداده باید همان فهرست محدود عمومی باشد')
    snapshot = plan['source_document_snapshot']
    if not isinstance(snapshot, str) or not re.fullmatch(r'CODAL-[a-f0-9]{32}', snapshot):
        raise ValueError('شناسه سند معتبر نیست')
    source, text = _source(folder/'document'/snapshot, notice)
    prefix = 'document/'+snapshot+'/'
    expected_inputs = {'notice.json', prefix+'manifest.json', prefix+source['content_file'],
                       *(prefix+n for n in source['source_files_sha256'])}
    if set(plan['inputs']) != expected_inputs or source['snapshot_id'] != snapshot:
        raise ValueError('فهرست کامل ورودی‌های منبع برابر نیست')
    if (set(result) != {'version', 'created_at', 'available_at', 'facts'} or result['version'] != VERSION
            or result['facts'] != primary_rights_registration(text, notice, end_exclusive=plan['cutoff'])
            or _clock(result['available_at']) != max(_clock(result['created_at']), _clock(source['available_at']))
            or not _clock(source['available_at']) <= _clock(plan['registered_at'])
                <= _clock(result['created_at']) <= _clock(now_tehran_iso())):
        raise ValueError('نتیجه یا ترتیب زمان مطالعه بازحساب نشد')
    return dict(**result, artifacts_verified=True, semantic_code_matches_current=True,
        capture_code_matches_current=all(hashlib.sha256(_code(name)).hexdigest() == value
                                         for name, value in plan['code'].items()))


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
        result = (create_rights_study(notice_path=args.notice, document=args.document) if args.command == 'create'
                  else review_rights_study(args.study))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
