"""آرشیو فقط‌افزودنی اسناد عمومی؛ HTML فعال و اطلاعات نشست ذخیره نمی‌شوند."""

import hashlib
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from config import settings
from core.codal_documents import DOCUMENT_VERSION, public_document_url
from core.timeutils import TEHRAN_TZ, now_tehran_iso

SOURCE_FILES = ('core/codal_documents.py', 'data_collector/codal.py', 'ops/codal_documents.py',
                'core/timeutils.py', 'core/text.py', 'config/settings.py', 'requirements.lock.txt')


def _sha(content):
    return hashlib.sha256(content).hexdigest()


def _root(root):
    return Path(root or settings.DATA_DIR / 'private' / 'codal-documents').resolve()


def _stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError('زمان سند باید منطقه زمانی داشته باشد')
    return parsed.astimezone(TEHRAN_TZ)


def load_codal_document(folder):
    """خواندن همراه با کنترل هویت، مسیر و اثر انگشت محتوا و کد محفوظ."""
    folder = Path(folder).resolve()
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != DOCUMENT_VERSION or manifest.get('snapshot_id') != folder.name:
        raise ValueError('هویت آرشیو سند کدال معتبر نیست')
    public_document_url(manifest['source_url'])
    public_document_url(manifest['effective_url'])
    available = _stamp(manifest['available_at'])
    if (available < max(_stamp(manifest['observed_at']), _stamp(manifest['archived_at']))
            or _stamp(manifest['observed_at']) < _stamp(manifest['requested_at'])):
        raise ValueError('زمان دسترس‌پذیری سند نباید به گذشته برده شود')
    unchecked = ('publication_time_verified', 'document_identity_verified', 'numeric_facts_reviewed',
                 'text_extraction_complete')
    if any(manifest.get(key) is not False for key in unchecked):
        raise ValueError('این قرارداد تأیید انتشار، هویت یا استخراج کامل عددی ندارد')
    if set(manifest['source_files_sha256']) != {'code/' + name for name in SOURCE_FILES}:
        raise ValueError('فهرست کد محفوظ سند کامل نیست')
    for link in manifest['links']:
        public_document_url(link)
    expected = 'report.pdf' if manifest['format'] == 'pdf' else 'report.txt'
    if manifest['format'] not in ('pdf', 'text') or manifest['content_file'] != expected:
        raise ValueError('نوع یا مسیر محتوای سند معتبر نیست')
    for name, digest in {expected: manifest['content_sha256'], **manifest['source_files_sha256']}.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or _sha(path.read_bytes()) != digest:
            raise ValueError('اثر انگشت محتوا یا کد محفوظ سند ناسازگار است')
    return manifest


def capture_codal_document(url, *, provider=None, root=None):
    """هر دریافت موفق، نسخه مستقل؛ دریافت ناموفق/کپچا هرگز سند موفق منتشر نمی‌کند."""
    from data_collector.codal import CodalProvider

    public_document_url(url)
    sources = {name: (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') for name in SOURCE_FILES}
    result = (provider or CodalProvider()).get_document(url)
    if result['status'] != 'captured':
        return result
    for name, content in sources.items():
        if (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') != content:
            raise RuntimeError('کد دریافت سند هنگام اجرا تغییر کرده است؛ با کد ثابت تکرار شود')
    if result['format'] not in ('pdf', 'text') or not isinstance(result.get('content'), bytes):
        raise ValueError('محتوای سند معتبر نیست')
    snapshot_id = 'CODAL-' + uuid.uuid4().hex
    folder = _root(root) / snapshot_id
    folder.mkdir(parents=True, exist_ok=False)
    filename = 'report.pdf' if result['format'] == 'pdf' else 'report.txt'
    archived_at = now_tehran_iso()
    available = max(_stamp(archived_at), _stamp(result['observed_at'])).isoformat()
    manifest = {
        'version': DOCUMENT_VERSION, 'snapshot_id': snapshot_id,
        'source_url': result['source_url'], 'effective_url': result['effective_url'],
        'requested_at': result['requested_at'], 'observed_at': result['observed_at'],
        'archived_at': archived_at, 'available_at': available,
        'publication_time_verified': False, 'document_identity_verified': False,
        'numeric_facts_reviewed': False, 'text_extraction_complete': False,
        'format': result['format'], 'content_file': filename,
        'content_sha256': _sha(result['content']), 'response_sha256': result['response_sha256'],
        'response_bytes': result['response_bytes'], 'links': result['links'],
        'source_files_sha256': {'code/' + name: _sha(content) for name, content in sources.items()},
        'interpretation': 'Public response capture only. Visible HTML text can omit dynamic tables; PDFs need review. '
                          'Publication, company identity and numerical facts are not verified. '
                          'Available_at is no earlier than capture; never backdate a historical feature.',
    }
    (folder / filename).write_bytes(result['content'])
    for name, content in sources.items():
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    # اشاره‌گر سراسری نداریم؛ manifest اتمی پایان یک نسخه مستقل را اعلام می‌کند.
    temporary = folder / 'manifest.json.tmp'
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    os.replace(temporary, folder / 'manifest.json')
    load_codal_document(folder)
    return dict(status='captured', folder=str(folder), **manifest)


def recent_codal_documents(*, root=None, limit=20):
    folder = _root(root)
    if not folder.exists():
        return []
    manifests = sorted(folder.glob('CODAL-*/manifest.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    results = []
    for path in manifests[:limit]:
        try:
            results.append(dict(status='captured', folder=str(path.parent), **load_codal_document(path.parent)))
        except (ValueError, KeyError, TypeError, OSError):
            results.append(dict(status='invalid', folder=str(path.parent), message='آرشیو ناسازگار است؛ معتبر نمایش داده نشد'))
    return results
