"""نسخه مستقل سلول‌های عمومی کدال؛ از دفتر حساب و انبار فیچر مستقل است."""

import hashlib
import json
import os
import uuid
from pathlib import Path

from config import settings
from core.codal_documents import public_document_url
from core.codal_tables import TABLE_VERSION, _unique, numeric_candidate
from core.timeutils import now_tehran_iso
from ops.codal_documents import _stamp

ARCHIVE_VERSION = 'codal-monthly-archive-v1'
SOURCE_FILES = ('core/codal_tables.py', 'ops/codal_tables.py', 'data_collector/codal.py',
                'core/codal_documents.py', 'core/timeutils.py', 'core/text.py',
                'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')
UNVERIFIED = ('numeric_facts_reviewed', 'units_and_period_columns_verified', 'document_identity_verified',
              'publication_time_verified', 'formulas_executed', 'accepted_for_features', 'accepted_for_signals')


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _read_json(path):
    def invalid(value):
        raise ValueError('عدد غیرمتناهی در آرشیو جدول')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique, parse_constant=invalid)


def load_codal_tables(folder):
    folder = Path(folder).resolve()
    manifest = _read_json(folder / 'manifest.json')
    if manifest.get('version') != ARCHIVE_VERSION or manifest.get('snapshot_id') != folder.name:
        raise ValueError('هویت آرشیو جدول معتبر نیست')
    public_document_url(manifest['source_url'])
    public_document_url(manifest['effective_url'])
    if (_stamp(manifest['available_at']) < max(_stamp(manifest['observed_at']), _stamp(manifest['archived_at']))
            or _stamp(manifest['observed_at']) < _stamp(manifest['requested_at'])):
        raise ValueError('زمان جدول نباید به گذشته برده شود')
    expected_files = {'tables.json', 'report.txt'} | {'code/' + name for name in SOURCE_FILES}
    if set(manifest['files_sha256']) != expected_files or any(manifest.get(k) is not False for k in UNVERIFIED):
        raise ValueError('فهرست فایل یا مرز تأیید آرشیو معتبر نیست')
    for name, digest in manifest['files_sha256'].items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or _sha(path.read_bytes()) != digest:
            raise ValueError('اثر انگشت محتوای جدول یا کد ناسازگار است')
    data = _read_json(folder / 'tables.json')
    if (data.get('version') != TABLE_VERSION or any(data.get(k) is not False for k in UNVERIFIED)
            or data['report']['tracing_no'] != manifest['expected_tracing_no']):
        raise ValueError('شناسه گزارش یا مرز تأیید داده ناسازگار است')
    cells = [cell for table in data['tables'] for cell in table['cells']]
    if data['cell_count'] != len(cells) or data['table_count'] != len(data['tables']):
        raise ValueError('شمار جدول/سلول ناسازگار است')
    for cell in cells:
        expected = numeric_candidate(cell['raw_value']) if cell['visible'] is True and cell['data_type'] in ('Integer', 'Float') else None
        if cell['numeric_candidate'] != expected:
            raise ValueError('عدد پیشنهادی با متن سلول ناسازگار است')
    count = sum(c['numeric_candidate'] is not None for c in cells)
    if data['numeric_candidate_count'] != count or any(manifest[k] != data[k] for k in ('cell_count', 'table_count', 'numeric_candidate_count')):
        raise ValueError('شمار اعداد پیشنهادی با آرشیو ناسازگار است')
    return dict(manifest=manifest, data=data)


def capture_codal_tables(url, expected_tracing_no, *, provider=None, root=None):
    """خواندن فقط از provider؛ هیچ داده‌ای به مدل/حساب/دیتابیس اضافه نمی‌شود."""
    from data_collector.codal import CodalProvider

    public_document_url(url)
    if isinstance(expected_tracing_no, bool) or not isinstance(expected_tracing_no, int) or expected_tracing_no <= 0:
        raise ValueError('شماره پیگیری مثبت و صریح گزارش لازم است')
    sources = {name: (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') for name in SOURCE_FILES}
    result = (provider or CodalProvider()).get_document(url, structured_tables=True)
    if result['status'] != 'captured':
        return result
    if (result.get('format') != 'text' or not isinstance(result.get('table_data'), dict)
            or result['table_data']['report']['tracing_no'] != expected_tracing_no):
        raise ValueError('جدول با شماره پیگیری مورد انتظار تطبیق ندارد')
    for name, content in sources.items():
        if (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') != content:
            raise ValueError('کد دریافت جدول حین اجرا تغییر کرده است')
    data = result['table_data']
    files = {'report.txt': result['content'], 'tables.json': json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')}
    files.update({'code/' + name: body for name, body in sources.items()})
    snapshot_id = 'CODALTABLE-' + uuid.uuid4().hex
    folder = Path(root or settings.DATA_DIR / 'private' / 'codal-tables').resolve() / snapshot_id
    folder.mkdir(parents=True, exist_ok=False)
    archived = now_tehran_iso()
    manifest = dict(version=ARCHIVE_VERSION, snapshot_id=snapshot_id, expected_tracing_no=expected_tracing_no,
                    project_version=settings.VERSION,
                    source_url=result['source_url'], effective_url=result['effective_url'],
                    requested_at=result['requested_at'], observed_at=result['observed_at'], archived_at=archived,
                    available_at=max(_stamp(archived), _stamp(result['observed_at'])).isoformat(),
                    response_sha256=result['response_sha256'], response_bytes=result['response_bytes'],
                    files_sha256={name: _sha(body) for name, body in files.items()},
                    table_count=data['table_count'], cell_count=data['cell_count'],
                    numeric_candidate_count=data['numeric_candidate_count'], **{k: False for k in UNVERIFIED},
                    interpretation='Declared JSON cells only; no JavaScript or formulas executed. Decimal strings have no verified unit or period column. '
                                   'Source publication and company identity are unverified. No model feature or signal is created. '
                                   'Local hashes are not independent timestamp attestation.')
    for name, body in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    temporary = folder / 'manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, folder / 'manifest.json')
    load_codal_tables(folder)
    return dict(status='captured', folder=str(folder), **manifest)
