"""Read-only sensitivity from the hash-checked market-review archive."""

import hashlib
import json
import math
from pathlib import Path

from core.market_return_coverage import missing_open_sensitivity
from core.market_review import MARKET_REVIEW_VERSION
from ops.report_files import read_private_report


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _read_bound(path, expected):
    """Hash and parse the same bounded bytes; reject ambiguous JSON."""
    with path.open('rb') as stream:
        body = stream.read(16 * 1024 * 1024 + 1)
    if len(body) > 16 * 1024 * 1024 or hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('Market archive bytes or size differ')

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate market archive key')
            result[key] = value
        return result

    def finite(value):
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError('Nonfinite market archive number')
        return parsed

    def reject(_):
        raise ValueError('Nonfinite market archive number')

    try:
        result = json.loads(body.decode('utf-8'), object_pairs_hook=unique,
                            parse_float=finite, parse_constant=reject)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('Market archive is not valid JSON') from exc
    if not isinstance(result, dict):
        raise ValueError('Market archive must contain an object')
    return result


def load_sensitivity(folder, report):
    folder = Path(folder).resolve()
    if not isinstance(report, dict):
        raise ValueError('Market report is invalid')
    manifest = read_private_report(folder/'manifest.json')
    files = manifest.get('files')
    if (manifest.get('version') != MARKET_REVIEW_VERSION or report.get('version') != MARKET_REVIEW_VERSION
            or not isinstance(files, dict) or report.get('run_id') != folder.name):
        raise ValueError('Market report identity or manifest differs')
    report_path = folder/'report.json'
    if (report_path.is_symlink() or not report_path.resolve().is_relative_to(folder)
            or _read_bound(report_path, files.get('report.json')) != report):
        raise ValueError('Market report changed after verification')
    prefix = 'source/' if report.get('source_run_id') else ''
    observations = {}
    for row in report.get('instruments', []):
        if not isinstance(row, dict):
            raise ValueError('Market report row is invalid')
        if row.get('status') != 'invalid_candle':
            continue
        code = row.get('ins_code')
        if not isinstance(code, str) or not code.isascii() or not code.isdigit():
            raise ValueError('Market instrument identity is invalid')
        name = f'{prefix}closing-{code}.json'
        if name not in files:
            continue
        path = folder/name
        if path.is_symlink() or not path.resolve().is_relative_to(folder):
            raise ValueError('Market observation path is invalid')
        observations[code] = _read_bound(path, files[name])
    result = missing_open_sensitivity(report, observations)
    if (_digest(report_path) != files.get('report.json')
            or read_private_report(folder/'manifest.json') != manifest):
        raise ValueError('Market report or manifest changed during review')
    return result
