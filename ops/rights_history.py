"""Frozen, resumable pre-holdout history capture for discovered right candidates."""

import argparse
import hashlib
import json
import uuid

from config import settings
from core.rights_history import VERSION, assess_history, candidate_starts
from core.timeutils import now_tehran_iso
from data_collector.source_cooldown import checked_transport_evidence
from data_collector.tsetmc import TsetmcProvider
from ops.report_files import read_private_report, write_private_report
from ops.rights_discovery import _canonical, _code_fingerprints, _code_matches, _exclusive_capture, _folder
from ops.rights_discovery import review as review_discovery

PLAN_VERSION = 'rights-selected-history-plan-v1'
SOURCE_FILES = ('core/rights_history.py', 'core/instrument_history.py', 'core/price_revision.py',
                'core/timeutils.py', 'data_collector/base.py', 'data_collector/tsetmc.py',
                'data_collector/daily_history.py', 'data_collector/source_cooldown.py',
                'ops/rights_history.py', 'ops/rights_discovery.py', 'ops/report_files.py',
                'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')


def create_plan(discovery_folder, folder):
    """Freeze all discovered candidates and ambiguity before fetching their prices."""
    reviewed = review_discovery(discovery_folder)
    candidates = {}
    for row in reviewed['rows']:
        if row['status'] != 'observed_matches':
            continue
        for code in row['candidate_codes']:
            candidates.setdefault(code, []).append(row['parent'])
    if not candidates:
        raise ValueError('There are no observed exact-name candidates to capture')
    frozen = [{'ins_code': code, 'parents': sorted(parents, key=lambda x: x['parent_code'])}
              for code, parents in sorted(candidates.items())]
    if len(frozen) > 10000:
        raise ValueError('Candidate snapshot exceeds the capture budget')
    folder = _folder(folder)
    plan = {'version': PLAN_VERSION, 'registered_at': now_tehran_iso(), 'history_contract': VERSION,
            'source_discovery_folder': str(_folder(discovery_folder)),
            'source_parent_count': reviewed['parent_count'], 'source_parent_status_counts': reviewed['status_counts'],
            'source_review_sha256': hashlib.sha256(_canonical(reviewed)).hexdigest(),
            'candidates': frozen, 'candidate_snapshot_sha256': hashlib.sha256(_canonical(frozen)).hexdigest(),
            'capture_code_sha256': _code_fingerprints(SOURCE_FILES),
            'start_date': settings.RESEARCH_CONFIG['dev_start'],
            'end_date_exclusive': settings.RESEARCH_CONFIG['holdout_start'],
            'source_snapshot_is_present_day': True, 'historical_universe_complete': False,
            'historical_instrument_identity_verified': False, 'accepted_for_signals': False}
    folder.mkdir(parents=True, exist_ok=False)
    write_private_report(folder / 'plan.json', plan, forbidden_paths=(settings.DB_PATH,))
    return folder


def _read_plan(folder):
    plan = read_private_report(folder / 'plan.json')
    items = plan.get('candidates')
    if (plan.get('version') != PLAN_VERSION or plan.get('history_contract') != VERSION
            or plan.get('start_date') != settings.RESEARCH_CONFIG['dev_start']
            or plan.get('end_date_exclusive') != settings.RESEARCH_CONFIG['holdout_start']
            or not isinstance(items, list) or not 0 < len(items) <= 10000
            or plan.get('candidate_snapshot_sha256') != hashlib.sha256(_canonical(items)).hexdigest()):
        raise ValueError('Frozen historical plan has changed or is incompatible')
    codes = []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {'ins_code', 'parents'}
                or not isinstance(item['parents'], list) or not item['parents']):
            raise ValueError('Frozen candidate metadata are invalid')
        code = assess_history(item['ins_code'], plan['start_date'], plan['end_date_exclusive'], None)['ins_code']
        if code != item['ins_code']:
            raise ValueError('Candidate code is not canonical text')
        codes.append(code)
    if len(codes) != len(set(codes)):
        raise ValueError('Duplicate candidate codes are not allowed')
    return plan


def _attempts(folder, code, plan):
    directory = folder / 'observations' / code
    paths = sorted(directory.glob('*.json')) if directory.exists() else []
    if [path.name for path in paths] != [f'{number:04d}.json' for number in range(1, len(paths)+1)]:
        raise ValueError('Saved historical attempt sequence is incomplete')
    plan_sha = hashlib.sha256(_canonical(plan)).hexdigest()
    records = []
    for path in paths:
        item = read_private_report(path, max_bytes=16 * 1024 * 1024)
        if item.get('version') != PLAN_VERSION or item.get('plan_sha256') != plan_sha or item.get('ins_code') != code:
            raise ValueError('Historical attempt identity or plan differs')
        checked_transport_evidence(item.get('transport_evidence'))
        try:
            replay = assess_history(code, plan['start_date'], plan['end_date_exclusive'], item.get('observation'))
        except ValueError:
            replay = {'status': 'invalid_response'}
        if replay != item.get('assessment'):
            raise ValueError('Historical attempt differs from selected rows')
        records.append(item)
    return records


def capture(provider, folder, *, max_new=100, retry_failed=False, progress=None):
    folder = _folder(folder)
    plan = _read_plan(folder)
    if type(max_new) is not int or not 1 <= max_new <= 10000 or type(retry_failed) is not bool:
        raise ValueError('Explicit bounded batch size and retry policy are required')
    attempted, stop = 0, None
    with _exclusive_capture(folder):
        if plan != _read_plan(folder):
            raise ValueError('Frozen historical plan changed before capture')
        for index, candidate in enumerate(plan['candidates'], 1):
            if _code_matches(plan, SOURCE_FILES) is False:
                stop = 'capture_code_changed'
                break
            code = candidate['ins_code']
            previous = _attempts(folder, code, plan)
            if previous:
                if previous[-1]['assessment']['status'] in ('observed', 'observed_with_gaps'):
                    continue
                if not retry_failed:
                    stop = 'prior_failed_attempt_requires_explicit_retry'
                    break
            if attempted >= max_new:
                stop = 'batch_complete'
                break
            observed = provider.get_development_history_observation(code, plan['start_date'], plan['end_date_exclusive'])
            try:
                assessment = assess_history(code, plan['start_date'], plan['end_date_exclusive'], observed)
            except ValueError:
                assessment = {'status': 'invalid_response'}
            record = {'version': PLAN_VERSION, 'plan_sha256': hashlib.sha256(_canonical(plan)).hexdigest(),
                      'attempted_at': now_tehran_iso(), 'ins_code': code,
                      'transport_evidence': checked_transport_evidence(getattr(provider, 'last_transport_evidence', None)),
                      'observation': observed, 'assessment': assessment}
            directory = folder / 'observations' / code
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{len(previous) + 1:04d}.json'
            if path.exists():
                raise ValueError('Existing historical attempt cannot be overwritten')
            write_private_report(path, record, forbidden_paths=(settings.DB_PATH,))
            _attempts(folder, code, plan)
            attempted += 1
            if progress is not None:
                progress(index, len(plan['candidates']), candidate, assessment)
            if assessment['status'] not in ('observed', 'observed_with_gaps'):
                stop = 'source_unavailable_or_invalid'
                break
    return {'folder': str(folder), 'new_attempts': attempted, 'stop_reason': stop or 'plan_processed',
            'historical_universe_complete': False, 'accepted_for_signals': False}


def review(folder):
    folder = _folder(folder)
    plan = _read_plan(folder)
    rows, counts = [], {}
    for candidate in plan['candidates']:
        records = _attempts(folder, candidate['ins_code'], plan)
        assessment = records[-1]['assessment'] if records else None
        status = assessment['status'] if assessment else 'not_attempted'
        counts[status] = counts.get(status, 0) + 1
        gaps = None
        if status in ('observed', 'observed_with_gaps'):
            gaps = candidate_starts(candidate['ins_code'], plan['start_date'], plan['end_date_exclusive'],
                                    records[-1]['observation'])
        rows.append({'candidate': candidate, 'attempt_count': len(records), 'status': status,
                     'assessment': assessment, 'gap_discovery': gaps})
    candidates = [entry for row in rows if row['gap_discovery'] is not None
                  for entry in row['gap_discovery']['candidate_starts']]
    return {'version': PLAN_VERSION, 'folder': str(folder), 'candidate_count': len(rows), 'status_counts': counts,
            'candidate_observed_starts': len(candidates),
            'candidate_starts_after_prior_gap': sum(not item['left_censored'] for item in candidates),
            'left_censored_first_starts': sum(item['left_censored'] for item in candidates),
            'source_parent_count': plan['source_parent_count'],
            'source_parent_status_counts': plan['source_parent_status_counts'], 'rows': rows,
            'capture_code_fingerprint_present': plan.get('capture_code_sha256') is not None,
            'current_code_matches_capture_plan': _code_matches(plan, SOURCE_FILES),
            'historical_universe_complete': False, 'historical_instrument_identity_verified': False,
            'original_source_bodies_archived': False, 'accepted_for_signals': False}


def main():
    parser = argparse.ArgumentParser(description='Capture selected pre-holdout rights histories for research')
    parser.add_argument('--discovery-folder', help='Freeze candidates from a reviewed discovery plan')
    parser.add_argument('--folder', help='Resume or review an existing historical plan')
    parser.add_argument('--max-new', type=int, default=100)
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--review-only', action='store_true')
    args = parser.parse_args()
    if bool(args.folder) == bool(args.discovery_folder) or args.review_only and not args.folder:
        parser.error('Supply one --discovery-folder or --folder; --review-only requires --folder')
    folder = (_folder(args.folder) if args.folder else create_plan(args.discovery_folder,
              settings.DATA_DIR / 'private' / 'rights-history' / ('HIST-' + uuid.uuid4().hex)))
    print('Frozen plan:', folder, flush=True)
    if args.review_only:
        report = review(folder)
        print(json.dumps({key: value for key, value in report.items() if key != 'rows'}, ensure_ascii=False), flush=True)
        return
    provider = TsetmcProvider(realtime=True)
    try:
        def progress(index, total, candidate, assessment):
            if index % 20 == 0 or assessment['status'] not in ('observed', 'observed_with_gaps'):
                print(index, '/', total, candidate['ins_code'], assessment['status'], flush=True)
        print(json.dumps(capture(provider, folder, max_new=args.max_new, retry_failed=args.retry_failed,
                                 progress=progress), ensure_ascii=False), flush=True)
    finally:
        provider.close()


if __name__ == '__main__':
    main()
