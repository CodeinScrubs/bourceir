"""Local operational evidence for the command desk; no network or order access."""

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
    output.update(heartbeat_at=stamp.isoformat(), heartbeat_age_seconds=age,
                  heartbeat_fresh=0 <= age <= 180)
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
                source_freshness_verified=False, signal_accepted=False, automatic_orders=False)
