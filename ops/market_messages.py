"""Archive one selected pre-holdout message window, never full mixed-period bytes."""

import argparse
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from config import settings
from core.market_messages import FALSE_FLAGS, MAX_BYTES, message_request, validate_projection
from core.timeutils import now_tehran_iso
from ops.codal_documents import _stamp

ARCHIVE_VERSION = 'market-message-window-archive-v2'
SOURCE_FILES_V1 = ('core/market_messages.py', 'data_collector/tsetmc.py', 'ops/market_messages.py',
                   'data_collector/source_cooldown.py', 'core/source_cooldown.py', 'core/timeutils.py',
                   'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')
SOURCE_FILES = SOURCE_FILES_V1 + ('core/daily_publications.py', 'core/quote_context.py',
                                 'ops/codal_documents.py')
SOURCE_FILES_BY_VERSION = {'market-message-window-archive-v1': SOURCE_FILES_V1,
                           ARCHIVE_VERSION: SOURCE_FILES}


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _encode(data):
    return json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def _unique(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('Duplicate archive JSON key')
        result[key] = value
    return result


def _read(path):
    def invalid(_):
        raise ValueError('Nonfinite archive number')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique, parse_constant=invalid)


def _receipt(receipt, code, url):
    expected = {'source', 'ins_code', 'url', 'requested_at', 'received_at',
                'http_status', 'response_bytes', 'body_sha256'}
    if (not isinstance(receipt, dict) or set(receipt) != expected or receipt['source'] != 'tsetmc'
            or receipt['ins_code'] != code or receipt['url'] != url or receipt['http_status'] != 200
            or type(receipt['http_status']) is not int or type(receipt['response_bytes']) is not int
            or not 0 < receipt['response_bytes'] <= MAX_BYTES
            or not isinstance(receipt['body_sha256'], str)
            or not re.fullmatch(r'[a-f0-9]{64}', receipt['body_sha256'])
            or _stamp(receipt['received_at']) < _stamp(receipt['requested_at'])):
        raise ValueError('Invalid source message receipt')


def load_message_window(folder):
    folder = Path(folder).resolve()
    manifest = _read(folder / 'manifest.json')
    if not isinstance(manifest, dict) or not isinstance(manifest.get('version'), str):
        raise ValueError('Invalid market message archive identity or claims')
    source_files = SOURCE_FILES_BY_VERSION.get(manifest.get('version'))
    if (source_files is None or manifest.get('snapshot_id') != folder.name
            or any(manifest.get(flag) is not False for flag in FALSE_FLAGS)):
        raise ValueError('Invalid market message archive identity or claims')
    code, start, end = manifest['instrument_code'], manifest['start'], manifest['end_exclusive']
    url = message_request(code, start, end)
    _receipt(manifest['receipt'], code, url)
    if (_stamp(manifest['available_at']) < max(_stamp(manifest['archived_at']),
                                                _stamp(manifest['receipt']['received_at']))
            or _stamp(manifest['archived_at']) < _stamp(manifest['receipt']['received_at'])
            or max(_stamp(manifest['archived_at']), _stamp(manifest['available_at'])) > _stamp(now_tehran_iso())):
        raise ValueError('Message archive timing cannot precede capture or be in the future')
    hashes = manifest.get('files_sha256')
    expected = {'projection.json'} | {'code/' + name for name in source_files}
    if not isinstance(hashes, dict) or set(hashes) != expected:
        raise ValueError('Archive file set differs')
    for name, expected_hash in hashes.items():
        target = (folder / name).resolve()
        if not target.is_relative_to(folder) or not target.is_file() or _sha(target.read_bytes()) != expected_hash:
            raise ValueError('Archive content or source hash differs')
    projection = _read(folder / 'projection.json')
    validate_projection(projection, code, start, end, received_at=manifest['receipt']['received_at'])
    return {'manifest': manifest, 'projection': projection}


def capture_message_window(ins_code, start, end_exclusive, *, provider=None, root=None):
    from data_collector.tsetmc import TsetmcProvider

    url = message_request(ins_code, start, end_exclusive)  # before source access
    sources = {name: (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') for name in SOURCE_FILES}
    owned = provider is None
    api = TsetmcProvider(realtime=True) if owned else provider
    try:
        result = api.get_historical_market_messages(ins_code, start, end_exclusive)
    finally:
        if owned:
            api.close()
    if result is None:
        return {'status': 'unavailable', 'instrument_code': ins_code, 'start': start,
                'end_exclusive': end_exclusive, 'complete_history_verified': False}
    if not isinstance(result, dict) or set(result) != {'receipt', 'projection'}:
        raise ValueError('Unexpected provider result')
    _receipt(result['receipt'], ins_code, url)
    data = validate_projection(result['projection'], ins_code, start, end_exclusive,
                               received_at=result['receipt']['received_at'])
    for name, body in sources.items():
        if (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') != body:
            raise ValueError('Source changed during capture')
    archived = now_tehran_iso()
    if _stamp(archived) < _stamp(result['receipt']['received_at']):
        raise ValueError('Future receipt cannot be archived')
    files = {'projection.json': _encode(data)}
    files.update({'code/' + name: body for name, body in sources.items()})
    snapshot_id = 'MARKETMSG-' + uuid.uuid4().hex
    archive_root = Path(root or settings.DATA_DIR / 'private' / 'market-messages').resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    stage, folder = archive_root / ('.staging-' + snapshot_id), archive_root / snapshot_id
    stage.mkdir(exist_ok=False)
    manifest = dict(version=ARCHIVE_VERSION, snapshot_id=snapshot_id, instrument_code=ins_code,
                    start=start, end_exclusive=end_exclusive, archived_at=archived, available_at=archived,
                    receipt=result['receipt'], files_sha256={k: _sha(v) for k, v in files.items()},
                    **dict.fromkeys(FALSE_FLAGS, False),
                    interpretation='Selected pre-holdout messages only. Response bytes are not stored; '
                                   'source hash cannot replay them. Identity is URL-only and completeness '
                                   'and historical availability are not verified.')
    for name, body in files.items():
        target = stage / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    (stage / 'manifest.json').write_bytes(_encode(manifest))
    os.replace(stage, folder)
    load_message_window(folder)
    return {'status': 'captured', 'folder': str(folder), **manifest}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Capture one pre-holdout market-message projection')
    parser.add_argument('--ins-code', required=True)
    parser.add_argument('--start', required=True)
    parser.add_argument('--end-exclusive', required=True)
    args = parser.parse_args(argv)
    result = capture_message_window(args.ins_code, args.start, args.end_exclusive)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result['status'] == 'captured' else 1


if __name__ == '__main__':
    raise SystemExit(main())
