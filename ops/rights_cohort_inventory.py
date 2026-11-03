"""Offline retrospective inventory of every reviewed candidate right start.

This does not establish historical issuer membership, a rights issue, a discount,
the trading window, or any outcome. It never requests network or opens an account.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from collections import Counter
from pathlib import Path

from config import settings
from core.instrument_history import receipt_time
from core.timeutils import now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report
from ops.rights_discovery import SOURCE_FILES as DISCOVERY_SOURCE_FILES
from ops.rights_discovery import _canonical, _code_fingerprints, _folder
from ops.rights_discovery import review as review_discovery
from ops.rights_history import SOURCE_FILES, _read_plan
from ops.rights_history import review as review_history

VERSION = 'rights-cohort-inventory-v1'
CODE_FILES = tuple(dict.fromkeys((*SOURCE_FILES, *DISCOVERY_SOURCE_FILES,
                                 'core/source_cooldown.py', 'core/daily_publications.py',
                                 'config/production.yaml', 'ops/rights_cohort_inventory.py')))


def _sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _source_receipt(folder, codes, registered_at, checked_at):
    """Hash source files and validate local receipt clocks; never copy raw rows."""
    registration = receipt_time(registered_at)
    boundary = receipt_time(checked_at)
    if registration > boundary:
        raise ValueError('Source plan registration is in the future')
    latest = registration
    files = [folder / 'plan.json']
    directory = folder / 'observations'
    if directory.exists():
        for candidate in directory.iterdir():
            if not candidate.is_dir() or candidate.name not in codes:
                raise ValueError('Unexpected history observation directory')
            for path in candidate.iterdir():
                if not path.is_file() or not re.fullmatch(r'[0-9]{4}\.json', path.name):
                    raise ValueError('Unexpected history observation file')
                files.append(path)
    records = []
    for file in sorted(files, key=lambda p: p.relative_to(folder).as_posix()):
        resolved = file.resolve()
        if not resolved.is_relative_to(folder) or not resolved.is_file():
            raise ValueError('History source file escapes its archive')
        if file.name != 'plan.json':
            attempt = read_private_report(resolved)
            attempted_at = receipt_time(attempt.get('attempted_at'))
            if not registration <= attempted_at <= boundary:
                raise ValueError('Attempt time is outside source registration and inventory observation')
            latest = max(latest, attempted_at)
            for label in ('observation', 'transport_evidence'):
                metadata = attempt.get(label)
                if metadata is not None and not isinstance(metadata, dict):
                    raise ValueError('Source receipt clock metadata is invalid')
                for key in ('requested_at', 'received_at', 'checked_at'):
                    if isinstance(metadata, dict) and key in metadata:
                        stamp = receipt_time(metadata[key])
                        if not registration <= stamp <= attempted_at:
                            raise ValueError('Receipt time is outside source registration and attempt')
        records.append({'path': file.relative_to(folder).as_posix(), 'sha256': _sha(resolved)})
    plan_digest = next(row['sha256'] for row in records if row['path'] == 'plan.json')
    return {'file_count': len(records), 'plan_sha256': plan_digest,
            'files_sha256': hashlib.sha256(_canonical(records)).hexdigest(),
            'latest_source_at': latest.isoformat()}


def _projection(reviewed, discovery, plan):
    rows, starts = [], []
    parent_rows = [{'parent': row['parent'], 'status': row['status'], 'attempt_count': row['attempt_count'],
                    'candidate_codes': row['candidate_codes']} for row in discovery['rows']]
    parent_rows.sort(key=lambda row: row['parent']['parent_code'])
    for row in reviewed['rows']:
        candidate = row['candidate']
        history = row['assessment']
        gap = row['gap_discovery']
        item = {'ins_code': candidate['ins_code'], 'parents': candidate['parents'],
                'parent_ambiguous': len(candidate['parents']) != 1,
                'history_status': row['status'], 'attempt_count': row['attempt_count'],
                'history_quality': None if history is None else {
                    key: history.get(key) for key in ('selected_rows', 'positive_volume_rows',
                        'zero_volume_rows', 'invalid_volume_rows', 'invalid_traded_rows',
                        'inconsistent_zero_volume_rows', 'duplicate_dates')},
                'gap_discovery_status': None if gap is None else gap['status'],
                'observed_start_count': (None if gap is None or gap['status'] == 'unusable_for_gap_discovery'
                                         else len(gap['candidate_starts']))}
        rows.append(item)
        if gap is not None:
            for start in gap['candidate_starts']:
                starts.append({'ins_code': candidate['ins_code'], **start,
                    'parent_ambiguous': item['parent_ambiguous'],
                    'history_status': item['history_status'],
                    'gap_discovery_status': item['gap_discovery_status'],
                    'issue_identity_verified': False, 'initial_trading_window_verified': False})
    rows.sort(key=lambda row: row['ins_code'])
    starts.sort(key=lambda row: (row['ins_code'], row['date']))
    counts = Counter(row['history_status'] for row in rows)
    if (len(rows) != reviewed['candidate_count'] or dict(counts) != reviewed['status_counts']
            or len(starts) != reviewed['candidate_observed_starts']
            or sum(row['left_censored'] for row in starts) != reviewed['left_censored_first_starts']
            or sum(not row['left_censored'] for row in starts) != reviewed['candidate_starts_after_prior_gap']):
        raise ValueError('Inventory does not reconcile with the reviewed history')
    if any(not plan['start_date'] <= row['date'] < plan['end_date_exclusive'] for row in starts):
        raise ValueError('Candidate date is outside the development boundary')
    return {'source_start': plan['start_date'], 'end_date_exclusive': plan['end_date_exclusive'],
            'source_parent_count': reviewed['source_parent_count'],
            'source_parent_status_counts': reviewed['source_parent_status_counts'],
            'source_parent_rows': parent_rows,
            'candidate_count': len(rows), 'history_status_counts': dict(sorted(counts.items())),
            'observed_candidate_start_count': len(starts),
            'left_censored_start_count': sum(row['left_censored'] for row in starts),
            'candidate_rows': rows, 'candidate_starts': starts,
            'source_capture_code_fingerprint_present': reviewed['capture_code_fingerprint_present'],
            'current_code_matches_capture_plan': reviewed['current_code_matches_capture_plan'],
            'retrospective_after_prior_data_exposure': True, 'independent_blind_preregistration': False,
            'historical_universe_complete': False, 'historical_instrument_identity_verified': False,
            'original_source_bodies_archived': False, 'event_identity_verified': False,
            'selection_uses_opening_prices_or_outcomes': False,
            'future_price_outcomes_computed': False, 'accepted_for_signals': False,
            'profitability_proven': False}


def _recompute(source):
    source = _folder(source)
    plan = _read_plan(source)
    discovery_folder = _folder(plan['source_discovery_folder'])
    discovery_plan = read_private_report(discovery_folder / 'plan.json')
    history_codes = {candidate['ins_code'] for candidate in plan['candidates']}
    parent_codes = {parent['parent_code'] for parent in discovery_plan['parents']}
    checked_at = now_tehran_iso()
    before = _source_receipt(source, history_codes, plan['registered_at'], checked_at)
    discovery_before = _source_receipt(discovery_folder, parent_codes, discovery_plan['registered_at'], checked_at)
    if receipt_time(discovery_before['latest_source_at']) > receipt_time(plan['registered_at']):
        raise ValueError('History registration precedes discovery evidence')
    code_before = _code_fingerprints(CODE_FILES)
    discovery = review_discovery(discovery_folder)
    reviewed = review_history(source)
    candidates = {}
    for row in discovery['rows']:
        if row['status'] == 'observed_matches':
            for code in row['candidate_codes']:
                candidates.setdefault(code, []).append(row['parent'])
    frozen = [{'ins_code': code, 'parents': sorted(parents, key=lambda x: x['parent_code'])}
              for code, parents in sorted(candidates.items())]
    if (frozen != plan['candidates'] or discovery['parent_count'] != plan['source_parent_count']
            or discovery['status_counts'] != plan['source_parent_status_counts']):
        raise ValueError('Frozen candidates differ from the reviewed discovery')
    after = _source_receipt(source, history_codes, plan['registered_at'], checked_at)
    discovery_after = _source_receipt(discovery_folder, parent_codes, discovery_plan['registered_at'], checked_at)
    if (before != after or discovery_before != discovery_after or plan != _read_plan(source)
            or discovery_plan != read_private_report(discovery_folder / 'plan.json')
            or code_before != _code_fingerprints(CODE_FILES)):
        raise ValueError('Source history or inventory code changed during review')
    legacy_discovery = {key: value for key, value in discovery.items()
                        if key not in ('capture_code_fingerprint_present', 'current_code_matches_capture_plan')}
    receipt = {'history': before, 'discovery': discovery_before,
               'recorded_discovery_review_digest_matches_current':
                   hashlib.sha256(_canonical(discovery)).hexdigest() == plan['source_review_sha256'],
               'recorded_discovery_review_digest_matches_without_code_fields':
                   hashlib.sha256(_canonical(legacy_discovery)).hexdigest() == plan['source_review_sha256']}
    return plan, receipt, code_before, _projection(reviewed, discovery, plan)


def create_inventory(source, *, root=None):
    source = _folder(source)
    target_root = Path(root or settings.DATA_DIR / 'private' / 'rights-cohort-inventories').resolve()
    private_report_path(target_root / 'sentinel.json', forbidden_paths=(settings.DB_PATH,))
    discovery_folder = _folder(_read_plan(source)['source_discovery_folder'])
    if any(target_root.is_relative_to(input_folder) or input_folder.is_relative_to(target_root)
           for input_folder in (source, discovery_folder)):
        raise ValueError('Inventory destination must not contain or overwrite the source history')
    plan, receipt, code, projection = _recompute(source)
    study_id = 'COHORT-' + uuid.uuid4().hex
    created_at = now_tehran_iso()
    if receipt_time(created_at) < max(receipt_time(receipt['history']['latest_source_at']),
                                      receipt_time(receipt['discovery']['latest_source_at'])):
        raise ValueError('Inventory cannot predate its source evidence')
    output = {'version': VERSION, 'study_id': study_id, 'created_at': created_at,
              'source_history_folder': str(source), 'source_plan_sha256': receipt['history']['plan_sha256'],
              'source_receipt': receipt, 'code_sha256': code, 'inventory': projection}
    target_root.mkdir(parents=True, exist_ok=True)
    stage = target_root / ('.stage-' + study_id)
    folder = target_root / study_id
    stage.mkdir(exist_ok=False)
    inventory = write_private_report(stage / 'inventory.json', output, forbidden_paths=(settings.DB_PATH,))
    manifest = {'version': VERSION, 'study_id': study_id, 'inventory_sha256': _sha(inventory)}
    write_private_report(stage / 'manifest.json', manifest, forbidden_paths=(settings.DB_PATH,))
    discovery = _folder(plan['source_discovery_folder'])
    parent_codes = {row['parent']['parent_code'] for row in projection['source_parent_rows']}
    discovery_plan = read_private_report(discovery / 'plan.json')
    if (_source_receipt(source, {row['ins_code'] for row in plan['candidates']}, plan['registered_at'], created_at)
            != receipt['history']
            or _source_receipt(discovery, parent_codes, discovery_plan['registered_at'], created_at)
            != receipt['discovery']
            or _code_fingerprints(CODE_FILES) != code):
        raise ValueError('Source history, discovery or code changed before publication')
    os.replace(stage, folder)
    return {'status': 'created', 'folder': str(folder), 'study_id': study_id,
            'candidate_count': projection['candidate_count'],
            'observed_candidate_start_count': projection['observed_candidate_start_count'],
            'historical_universe_complete': False, 'accepted_for_signals': False}


def review_inventory(folder):
    folder = Path(folder).resolve()
    private_report_path(folder / 'manifest.json', forbidden_paths=(settings.DB_PATH,))
    manifest = read_private_report(folder / 'manifest.json')
    if (not re.fullmatch(r'COHORT-[a-f0-9]{32}', folder.name) or manifest.get('version') != VERSION
            or manifest.get('study_id') != folder.name or _sha(folder / 'inventory.json') != manifest.get('inventory_sha256')):
        raise ValueError('Inventory identity or digest differs')
    saved = read_private_report(folder / 'inventory.json')
    if (set(saved) != {'version', 'study_id', 'created_at', 'source_history_folder', 'source_plan_sha256',
                       'source_receipt', 'code_sha256', 'inventory'}
            or saved.get('version') != VERSION or saved.get('study_id') != folder.name):
        raise ValueError('Inventory contract differs')
    if saved.get('code_sha256') != _code_fingerprints(CODE_FILES):
        raise ValueError('Inventory code fingerprint differs; explicit reassessment is required')
    source = _folder(saved['source_history_folder'])
    if _sha(source / 'plan.json') != saved.get('source_plan_sha256'):
        raise ValueError('Source history plan changed')
    plan, receipt, code, projection = _recompute(source)
    if not (max(receipt_time(receipt['history']['latest_source_at']),
                receipt_time(receipt['discovery']['latest_source_at']))
            <= receipt_time(saved['created_at']) <= receipt_time(now_tehran_iso())):
        raise ValueError('Inventory timestamp differs from source registration or current time')
    if (saved.get('source_receipt') != receipt or saved.get('code_sha256') != code
            or saved.get('inventory') != projection):
        raise ValueError('Inventory does not replay from the current source and code')
    return {'status': 'reviewed', 'folder': str(folder), 'study_id': folder.name,
            'candidate_count': projection['candidate_count'],
            'observed_candidate_start_count': projection['observed_candidate_start_count'],
            'historical_universe_complete': False, 'accepted_for_signals': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create')
    create.add_argument('--history', required=True)
    inspect = commands.add_parser('review')
    inspect.add_argument('--study', required=True)
    args = parser.parse_args(argv)
    try:
        result = create_inventory(args.history) if args.command == 'create' else review_inventory(args.study)
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
