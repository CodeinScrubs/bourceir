"""Local operating-metrics receipts; no HTTP, financial account or feature-store writes."""

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path

from config import settings
from core.codal_operating import VERSION as METRICS_VERSION
from core.codal_operating import extract_operating_metrics
from core.opportunity_radar import digest, stamp
from core.timeutils import now_tehran_iso
from ops.codal_tables import SOURCE_FILES as INPUT_CODE_FILES
from ops.codal_tables import load_codal_tables
from ops.report_files import read_private_report, write_private_report

VERSION = 'codal-operating-report-v1'
MAX_ARCHIVE_BYTES = 64*1024*1024
MAX_FILE_BYTES = 20*1024*1024
MAX_RESULT_BYTES = 32*1024*1024
CODE_FILES = ('core/codal_operating.py', 'core/codal_revenue.py', 'core/codal_tables.py', 'core/text.py',
    'core/timeutils.py', 'core/opportunity_radar.py', 'ops/codal_operating.py', 'ops/codal_tables.py',
    'ops/codal_documents.py', 'core/codal_documents.py', 'ops/report_files.py',
    'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _read(path, root, *, limit=MAX_FILE_BYTES):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root) or '..' in path.parts:
        raise ValueError('Operating evidence path escapes its archive')
    for part in [root, *reversed(path.relative_to(root).parents), path.relative_to(root)]:
        target = part if part == root else root/part
        if not target.exists():
            raise ValueError('Operating evidence member is missing')
        info = target.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
            raise ValueError('Operating evidence cannot follow a symlink or junction')
    if settings.DB_PATH.exists() and os.path.samefile(path, settings.DB_PATH):
        raise ValueError('A financial account database is not operating evidence')
    with path.open('rb') as stream:
        body = stream.read(limit+1)
    if len(body) > limit:
        raise ValueError('Operating evidence exceeds its byte budget')
    return body


def _folder(folder, area, prefix):
    expected = (settings.DATA_DIR/'private'/area).absolute()
    folder = Path(folder).absolute()
    if (folder.parent != expected or '..' in folder.parts
            or not re.fullmatch(prefix+'[0-9a-f]{32}', folder.name)):
        raise ValueError('Only a designated private operating/table archive is allowed')
    # Check the containment root itself rather than trusting resolve() through a junction.
    for path in [Path(settings.DATA_DIR).absolute(), expected.parent, expected, folder]:
        if path.exists():
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('Operating archive root cannot follow a symlink or junction')
    return folder


def _input(folder, root):
    names = {'manifest.json', 'report.txt', 'tables.json'} | {'code/'+n for n in INPUT_CODE_FILES}
    bodies = {name: _read(folder/name, root) for name in names}
    if sum(map(len, bodies.values())) > MAX_ARCHIVE_BYTES:
        raise ValueError('Input archive exceeds its total byte budget')
    loaded = load_codal_tables(folder)
    # The loader and copied evidence must refer to one exact input version.
    if any(_read(folder/name, root) != body for name, body in bodies.items()):
        raise ValueError('Operating source changed while it was being read')
    return loaded, bodies


def _metrics(loaded, body):
    manifest = loaded['manifest']
    return extract_operating_metrics(body, expected_sha256=manifest['files_sha256']['tables.json'],
        available_at=manifest['available_at'], observed_at=manifest['observed_at'], archived_at=manifest['archived_at'])


def _inventory(folder):
    names = set()
    for current, directories, files in os.walk(folder, followlinks=False):
        for name in [*directories, *files]:
            path = Path(current)/name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400):
                raise ValueError('Operating inventory cannot contain a symlink or junction')
        names.update((Path(current)/name).relative_to(folder).as_posix() for name in files)
        if len(names) > 256:
            raise ValueError('Operating inventory exceeds its file budget')
    return names


def capture_operating_report(archive):
    source = _folder(archive, 'codal-tables', 'CODALTABLE-')
    loaded, bodies = _input(source, source)
    metrics = _metrics(loaded, bodies['tables.json'])
    calculated = now_tehran_iso()
    if stamp(calculated) < stamp(loaded['manifest']['available_at']):
        raise ValueError('Operating calculation precedes its preserved input availability')
    result = dict(version=VERSION, metrics_version=METRICS_VERSION, source_snapshot=source.name,
        origin_manifest_sha256=_sha(bodies['manifest.json']), calculated_at=calculated, available_at=calculated,
        source_available_at=loaded['manifest']['available_at'], metrics=metrics,
        accepted_for_features=False, accepted_for_signals=False, profitability_proven=False)
    result_size = len(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8'))
    if result_size > MAX_RESULT_BYTES:
        raise ValueError('Operating result exceeds its byte budget')
    target = _folder(settings.DATA_DIR/'private/codal-operating'/('OPERATING-'+uuid.uuid4().hex),
        'codal-operating', 'OPERATING-')
    target.mkdir(parents=True, exist_ok=False)
    relative = 'input/'+source.name
    saved = {relative+'/'+name: body for name, body in bodies.items()}
    saved.update({'code/'+name: (settings.BASE_DIR/name).read_bytes() for name in CODE_FILES})
    if sum(map(len, saved.values()))+result_size > MAX_ARCHIVE_BYTES:
        raise ValueError('Operating archive exceeds its total byte budget')
    for name, body in saved.items():
        path = target/name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    write_private_report(target/'result.json', result)
    files = {name: _sha(body) for name, body in saved.items()}
    files['result.json'] = _sha((target/'result.json').read_bytes())
    write_private_report(target/'manifest.json', dict(version=VERSION, id=target.name, input_archive=relative,
        files_sha256=files))
    checked = review_operating_report(target)
    return dict(folder=str(target), **checked)


def review_operating_report(folder):
    folder = _folder(folder, 'codal-operating', 'OPERATING-')
    _read(folder/'manifest.json', folder, limit=1024*1024)
    manifest = read_private_report(folder/'manifest.json', max_bytes=1024*1024)
    if set(manifest) != {'version', 'id', 'input_archive', 'files_sha256'} or manifest['version'] != VERSION or manifest['id'] != folder.name:
        raise ValueError('Operating archive manifest identity differs')
    relative = manifest['input_archive']
    if not isinstance(relative, str) or not re.fullmatch(r'input/CODALTABLE-[0-9a-f]{32}', relative):
        raise ValueError('Operating input identity differs')
    source = folder/relative
    input_names = {'manifest.json', 'report.txt', 'tables.json'} | {'code/'+n for n in INPUT_CODE_FILES}
    expected = {relative+'/'+n for n in input_names} | {'code/'+n for n in CODE_FILES} | {'result.json'}
    if not isinstance(manifest['files_sha256'], dict) or set(manifest['files_sha256']) != expected:
        raise ValueError('Operating archive inventory differs')
    if _inventory(folder) != expected | {'manifest.json'}:
        raise ValueError('Operating archive contains unbound or missing files')
    bodies = {}
    total = 0
    for name, wanted in manifest['files_sha256'].items():
        body = _read(folder/name, folder, limit=MAX_RESULT_BYTES if name == 'result.json' else MAX_FILE_BYTES)
        total += len(body)
        if total > MAX_ARCHIVE_BYTES or _sha(body) != wanted:
            raise ValueError('Operating archive fingerprint or total byte budget differs')
        bodies[name] = body
    if any(bodies['code/'+name] != (settings.BASE_DIR/name).read_bytes() for name in CODE_FILES):
        raise ValueError('Current code differs from the preserved operating decoder')
    loaded, originals = _input(source, folder)
    recorded = read_private_report(folder/'result.json', max_bytes=MAX_RESULT_BYTES)
    calculated = recorded.get('calculated_at')
    if stamp(calculated) < stamp(loaded['manifest']['available_at']):
        raise ValueError('Derived operating availability was backdated')
    expected_result = dict(version=VERSION, metrics_version=METRICS_VERSION, source_snapshot=source.name,
        origin_manifest_sha256=_sha(originals['manifest.json']), calculated_at=calculated, available_at=calculated,
        source_available_at=loaded['manifest']['available_at'], metrics=_metrics(loaded, originals['tables.json']),
        accepted_for_features=False, accepted_for_signals=False, profitability_proven=False)
    if digest(recorded) != digest(expected_result):
        raise ValueError('Operating result differs from its preserved source arithmetic')
    return dict(status=recorded['metrics']['status'], result=recorded, source_files_verified=len(bodies),
        exact_source_replay=True, network_requests=0)
