"""Local operational evidence for the command desk; no network or order access."""

import json
import re
from datetime import datetime
from pathlib import Path

from core.timeutils import is_canonical_iso_date, now_tehran
from ops.board_status import read_board_status
from ops.report_files import read_private_report


def _optional_record(path):
    """Distinguish a missing status from a malformed or unreadable status."""
    try:
        return 'present', read_private_report(path, max_bytes=65536)
    except FileNotFoundError:
        return 'absent', None
    except (OSError, ValueError, TypeError):
        return 'invalid', None


def _worker_state(worker):
    value = worker.get('running') if isinstance(worker, dict) else None
    return 'running' if value is True else 'stopped' if value is False else 'unknown'


def _radar_state(private_root, worker, at):
    status, heartbeat = _optional_record(private_root/'opportunity-radar'/'heartbeat.json')
    output = dict(worker=_worker_state(worker), heartbeat=status, heartbeat_at=None,
                  heartbeat_age_seconds=None, heartbeat_fresh=False, recorded_status=None,
                  price_session_date=None, news_window_complete=None)
    if status != 'present':
        return output
    try:
        stamp = datetime.fromisoformat(heartbeat['heartbeat_at'])
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError('heartbeat requires a timezone')
        age = (at-stamp).total_seconds()
    except (KeyError, ValueError, TypeError, OverflowError):
        output['heartbeat'] = 'invalid'
        return output
    fresh = 0 <= age <= 180
    if output['worker'] == 'running' and 'started_at' in worker:
        try:
            fresh = fresh and datetime.fromisoformat(worker['started_at']) <= stamp
        except (TypeError, ValueError, OverflowError):
            fresh = False
    output.update(heartbeat_at=stamp.isoformat(), heartbeat_age_seconds=age, heartbeat_fresh=fresh)
    if isinstance(heartbeat.get('status'), str):
        output['recorded_status'] = heartbeat['status']
    if is_canonical_iso_date(heartbeat.get('price_session_date')):
        output['price_session_date'] = heartbeat['price_session_date']
    coverage = heartbeat.get('news_coverage')
    if isinstance(coverage, dict) and type(coverage.get('window_scan_satisfied')) is bool:
        output['news_window_complete'] = coverage['window_scan_satisfied']
    return output


def _document_source_state(private_root):
    status, record = _optional_record(private_root/'source-health'/'codal-documents'/'manual-stop.json')
    if status != 'present':
        return status
    if record.get('version') != 'codal-documents-manual-stop-v1' or record.get('blocked') is not True:
        return 'invalid'
    return 'blocked'


def feature_store_state(data_root):
    """Check manifest metadata and file presence, without claiming data or hash validity."""
    try:
        from features.daily import FEATURE_SET_VERSION
        from features.store import feature_data_path
    except ImportError:
        return dict(version=None, state='invalid', date_max=None,
                    file_hash_verified=False, raw_inputs_verified=False)

    root = Path(data_root) / 'features'
    manifest_path = root / FEATURE_SET_VERSION / 'manifest.json'
    result = dict(version=FEATURE_SET_VERSION, state='invalid', date_max=None,
                  file_hash_verified=False, raw_inputs_verified=False)
    def unique(pairs):
        output = {}
        for key, value in pairs:
            if key in output:
                raise ValueError('duplicate manifest key')
            output[key] = value
        return output

    try:
        with manifest_path.open('rb') as stream:
            body = stream.read(10_000_001)
        if len(body) > 10_000_000:
            raise ValueError('feature manifest is too large')
        manifest = json.loads(body, object_pairs_hook=unique,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite number')))
        if (not isinstance(manifest, dict) or manifest.get('version') != FEATURE_SET_VERSION
                or type(manifest.get('manifest_format')) is not int or manifest['manifest_format'] != 2
                or type(manifest.get('rows')) is not int or manifest['rows'] <= 0
                or not is_canonical_iso_date(manifest.get('date_max'))
                or not isinstance(manifest.get('data_file'), str) or not manifest['data_file']
                or not isinstance(manifest.get('provenance'), dict)
                or not isinstance(manifest['provenance'].get('parquet_sha256'), str)
                or not re.fullmatch(r'[0-9a-f]{64}', manifest['provenance']['parquet_sha256'])):
            raise ValueError('feature manifest metadata is invalid')
        if not feature_data_path(manifest, version=FEATURE_SET_VERSION, root=root).is_file():
            raise ValueError('feature file is unavailable')
        result.update(state='present_unverified', date_max=manifest['date_max'])
    except FileNotFoundError:
        result['state'] = 'absent'
    except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError, RuntimeError):
        pass
    return result


def command_health(private_root, *, radar_worker, live_worker, at=None):
    """Report process, recorded data and source stop separately; absence is not success."""
    at = at or now_tehran()
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError('A timezone-aware check time is required')
    private_root = Path(private_root)
    board = read_board_status(private_root, at=at)
    items = (board.get('status') or {}).get('boards', [])
    valid_boards = sum(isinstance(item, dict) and item.get('ok') is True for item in items)
    return dict(checked_at=at.isoformat(), radar=_radar_state(private_root, radar_worker, at),
                public_live_worker=_worker_state(live_worker),
                board_state=board['state'], board_valid_extracts=valid_boards if board['state'] == 'current' else None,
                codal_document_source=_document_source_state(private_root),
                feature_store=feature_store_state(private_root.parent),
                source_freshness_verified=False, signal_accepted=False, automatic_orders=False)
