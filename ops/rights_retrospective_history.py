"""Explicitly opted-in retrospective rights history, frozen and resumable."""
import argparse
import hashlib
import json
import uuid

from config import settings
from core.instrument_history import receipt_time
from core.rights_retrospective_history import VERSION, assess_history
from core.timeutils import now_tehran_iso
from data_collector.source_cooldown import checked_transport_evidence
from data_collector.tsetmc import TsetmcProvider
from ops.report_files import read_private_report, write_private_report
from ops.rights_daily_retrospective import review as review_universe
from ops.rights_daily_universe import review as review_pre_holdout_universe
from ops.rights_discovery import _canonical, _code_fingerprints, _code_matches, _exclusive_capture, _folder
from ops.rights_discovery import review as review_name_search

PLAN_VERSION = 'rights-retrospective-history-plan-v2'
LEGACY_VERSION = 'rights-retrospective-history-plan-v1'
SOURCE_FILES = ('core/rights_retrospective_history.py', 'core/instrument_history.py', 'core/price_revision.py',
                'core/timeutils.py', 'data_collector/base.py', 'data_collector/tsetmc.py',
                'data_collector/daily_history.py', 'data_collector/source_cooldown.py',
                'core/rights_daily_retrospective.py', 'core/rights_daily_universe.py',
                'ops/rights_daily_retrospective.py', 'ops/rights_daily_universe.py',
                'ops/rights_retrospective_history.py', 'ops/rights_discovery.py', 'ops/report_files.py',
                'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')
LEGACY_SOURCE_FILES = tuple(name for name in SOURCE_FILES if name not in {
    'core/rights_daily_retrospective.py', 'core/rights_daily_universe.py',
    'ops/rights_daily_retrospective.py', 'ops/rights_daily_universe.py'})


def _source_files(plan):
    return LEGACY_SOURCE_FILES if plan['version'] == LEGACY_VERSION else SOURCE_FILES


def create_plan(universe_folder, folder, start, end, *, codes=None, retrospective_rights_only=False,
                pre_holdout_universe_folders=(), discovery_folders=()):
    if retrospective_rights_only is not True:
        raise ValueError('Explicit retrospective rights-only opt-in required')
    assess_history('1', start, end, None)
    if not isinstance(pre_holdout_universe_folders, (tuple, list)) or not isinstance(discovery_folders, (tuple, list)):
        raise ValueError('Candidate sources must be explicit lists')
    sources = []
    if universe_folder is not None:
        sources.append(('retrospective', _folder(universe_folder), review_universe))
    sources.extend(('pre_holdout', _folder(path), review_pre_holdout_universe)
                   for path in pre_holdout_universe_folders)
    sources.extend(('name_search', _folder(path), review_name_search) for path in discovery_folders)
    if not sources or len(sources) > 100 or len({str(path) for _, path, _ in sources}) != len(sources):
        raise ValueError('Distinct reviewed universe sources are required')
    existing, source_receipts = {}, []
    for kind, source_path, reviewer in sources:
        source = reviewer(source_path)
        if kind == 'retrospective' and source.get('holdout_contaminated_for_rights_hypothesis') is not True:
            raise ValueError('Retrospective universe review required')
        source_ref = str(source_path)
        source_receipts.append(dict(kind=kind, folder=source_ref,
            review_sha256=hashlib.sha256(_canonical(source)).hexdigest(),
            current_code_matches_capture_plan=source.get('current_code_matches_capture_plan')))
        if kind == 'name_search':
            source_candidates = {}
            if not isinstance(source.get('rows'), list):
                raise ValueError('Reviewed name search rows are required')
            for row in source['rows']:
                if not isinstance(row, dict) or not isinstance(row.get('candidate_codes'), list):
                    raise ValueError('Reviewed search candidates are required')
                parent = row.get('parent')
                if (not isinstance(parent, dict) or not isinstance(parent.get('parent_symbol'), str)
                        or not parent['parent_symbol'].strip()):
                    raise ValueError('Explicit search parent is required')
                # Search matches the normalized exact query; it does not prove issuer or ISIN.
                name = parent['parent_symbol'] + 'ح'
                for code in row['candidate_codes']:
                    source_candidates.setdefault(code, {'names': []})['names'].append(
                        {'symbol': name, 'instrument_id': None})
        else:
            source_candidates = source.get('candidates')
        if not isinstance(source_candidates, dict):
            raise ValueError('Reviewed universe candidates must be a mapping')
        for code, row in source_candidates.items():
            if not isinstance(row, dict) or not isinstance(row.get('names'), list):
                raise ValueError('Source names must be an explicit list')
            entry = existing.setdefault(code, {'identities': [], 'sources': []})
            entry['sources'].append(source_ref)
            for identity in row['names']:
                if (not isinstance(identity, dict) or set(identity) != {'symbol', 'instrument_id'}
                        or not isinstance(identity['symbol'], str) or not identity['symbol']
                        or identity['instrument_id'] is not None and not isinstance(identity['instrument_id'], str)):
                    raise ValueError('Invalid observed identity')
                observed = {**identity, 'source_folder': source_ref}
                if observed not in entry['identities']:
                    entry['identities'].append(observed)
    if codes is not None and (not isinstance(codes, list) or not codes or len(codes) != len(set(codes))):
        raise ValueError('Selected codes must be a nonempty unique list')
    selected = sorted(existing if codes is None else codes)
    if not selected or len(selected) > 10000 or any(code not in existing for code in selected):
        raise ValueError('Selected codes must be observed candidate identities')
    candidates = []
    for code in selected:
        entry = existing[code]
        identities = sorted(entry['identities'], key=lambda x: (x['symbol'], x['instrument_id'] or '', x['source_folder']))
        variants = {(item['symbol'], item['instrument_id']) for item in identities}
        ambiguous = len(variants) != 1 or any(item['instrument_id'] is None for item in identities)
        candidates.append(dict(ins_code=code, identities=identities, sources=sorted(entry['sources']),
                               identity_ambiguous=ambiguous))
    folder = _folder(folder)
    plan = dict(version=PLAN_VERSION, history_contract=VERSION, registered_at=now_tehran_iso(),
                source_universes=source_receipts,
                candidates=candidates, candidate_snapshot_sha256=hashlib.sha256(_canonical(candidates)).hexdigest(),
                capture_code_sha256=_code_fingerprints(SOURCE_FILES), start_date=start, end_date_exclusive=end,
                selected_subset=codes is not None, retrospective_rights_only=True,
                holdout_contaminated_for_rights_hypothesis=True, historical_universe_complete=False,
                historical_instrument_identity_verified=False, accepted_for_signals=False)
    folder.mkdir(parents=True, exist_ok=False)
    write_private_report(folder / 'plan.json', plan, forbidden_paths=(settings.DB_PATH,))
    return folder


def _read_plan(folder):
    p = read_private_report(folder / 'plan.json')
    candidates = p.get('candidates')
    if (p.get('version') not in (PLAN_VERSION, LEGACY_VERSION) or p.get('history_contract') != VERSION
            or p.get('retrospective_rights_only') is not True
            or p.get('holdout_contaminated_for_rights_hypothesis') is not True
            or not isinstance(candidates, list) or not 0 < len(candidates) <= 10000
            or p.get('candidate_snapshot_sha256') != hashlib.sha256(_canonical(candidates)).hexdigest()):
        raise ValueError('Frozen retrospective plan differs')
    codes = []
    for row in candidates:
        expected = {'ins_code', 'identities'} if p['version'] == LEGACY_VERSION else {
            'ins_code', 'identities', 'sources', 'identity_ambiguous'}
        if not isinstance(row, dict) or set(row) != expected or not isinstance(row['identities'], list):
            raise ValueError('Invalid candidate snapshot')
        if p['version'] == PLAN_VERSION and (not isinstance(row['sources'], list) or not row['sources']
                or type(row['identity_ambiguous']) is not bool):
            raise ValueError('Explicit candidate provenance and ambiguity are required')
        code = assess_history(row['ins_code'], p['start_date'], p['end_date_exclusive'], None)['ins_code']
        if type(row['ins_code']) is not str or code != row['ins_code']:
            raise ValueError('Noncanonical candidate code')
        codes.append(code)
    if len(set(codes)) != len(codes):
        raise ValueError('Duplicate candidate code')
    return p


def _records(folder, code, plan, plan_sha):
    directory = folder / 'observations' / code
    paths = sorted(directory.glob('*.json')) if directory.exists() else []
    if [p.name for p in paths] != [f'{i:04d}.json' for i in range(1, len(paths)+1)]:
        raise ValueError('Observation sequence has a gap')
    records = []
    for path in paths:
        record = read_private_report(path)
        if (record.get('version') != plan['version'] or record.get('ins_code') != code
                or record.get('plan_sha256') != plan_sha
                or record.get('retrospective_rights_only') is not True
                or record.get('holdout_contaminated_for_rights_hypothesis') is not True):
            raise ValueError('Record differs from frozen plan')
        checked_transport_evidence(record.get('transport_evidence'))
        attempted = receipt_time(record.get('attempted_at'))
        if not receipt_time(plan['registered_at']) <= attempted <= receipt_time(now_tehran_iso()):
            raise ValueError('Attempt time differs')
        observation = record.get('observation')
        if observation is not None:
            request = receipt_time(observation.get('requested_at'))
            received = receipt_time(observation.get('received_at'))
            if not receipt_time(plan['registered_at']) <= request <= received <= attempted:
                raise ValueError('Receipt time differs')
        try:
            assessment = ({'status': 'invalid_response'} if observation is None and record.get('invalid_response')
                          else assess_history(code, plan['start_date'], plan['end_date_exclusive'], observation))
        except ValueError:
            assessment = {'status': 'invalid_response'}
        if assessment != record.get('assessment'):
            raise ValueError('Selected history projection differs from assessment')
        records.append(record)
    return records


def capture(provider, folder, *, max_new=20, retry_failed=False):
    folder = _folder(folder)
    plan = _read_plan(folder)
    plan_sha = hashlib.sha256(_canonical(plan)).hexdigest()
    if type(max_new) is not int or not 1 <= max_new <= 1000 or type(retry_failed) is not bool:
        raise ValueError('A bounded batch and explicit retry policy are required')
    count, stop = 0, None
    with _exclusive_capture(folder):
        if plan != _read_plan(folder):
            raise ValueError('Frozen plan changed')
        for item in plan['candidates']:
            code = item['ins_code']
            previous = _records(folder, code, plan, plan_sha)
            if previous:
                if previous[-1]['assessment']['status'] in ('observed', 'observed_with_gaps', 'observed_empty_unverified'):
                    continue
                if not retry_failed:
                    stop = 'prior_failure_requires_explicit_retry'; break
            if count >= max_new:
                stop = 'batch_complete'; break
            if _code_matches(plan, _source_files(plan)) is not True:
                stop = 'capture_code_changed'; break
            obs = provider.get_retrospective_rights_history_observation(code, plan['start_date'],
                   plan['end_date_exclusive'], retrospective_rights_only=True)
            try:
                assessment = assess_history(code, plan['start_date'], plan['end_date_exclusive'], obs)
                if obs is not None:
                    requested, received = receipt_time(obs.get('requested_at')), receipt_time(obs.get('received_at'))
                    if not receipt_time(plan['registered_at']) <= requested <= received <= receipt_time(now_tehran_iso()):
                        raise ValueError('Receipt timestamps differ')
            except (TypeError, ValueError):
                assessment = {'status': 'invalid_response'}
                obs = None
            record = dict(version=plan['version'], ins_code=code, plan_sha256=plan_sha, attempted_at=now_tehran_iso(),
                          transport_evidence=checked_transport_evidence(getattr(provider, 'last_transport_evidence', None)),
                          observation=obs, assessment=assessment, retrospective_rights_only=True,
                          holdout_contaminated_for_rights_hypothesis=True,
                          invalid_response=assessment['status'] == 'invalid_response')
            directory = folder / 'observations' / code
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{len(previous)+1:04d}.json'
            if path.exists():
                raise ValueError('Refusing to overwrite an observation')
            write_private_report(path, record, forbidden_paths=(settings.DB_PATH,))
            _records(folder, code, plan, plan_sha)
            count += 1
            if assessment['status'] not in ('observed', 'observed_with_gaps', 'observed_empty_unverified'):
                stop = 'source_unavailable_or_invalid'; break
    return dict(folder=str(folder), new_attempts=count, stop_reason=stop or 'plan_processed',
                holdout_contaminated_for_rights_hypothesis=True, accepted_for_signals=False)


def review(folder):
    folder = _folder(folder)
    plan = _read_plan(folder)
    plan_sha = hashlib.sha256(_canonical(plan)).hexdigest()
    counts, rows = {}, []
    for item in plan['candidates']:
        records = _records(folder, item['ins_code'], plan, plan_sha)
        assessment = records[-1]['assessment'] if records else None
        status = assessment['status'] if assessment else 'not_attempted'
        counts[status] = counts.get(status, 0) + 1
        rows.append(dict(candidate=item, status=status, attempts=len(records), assessment=assessment))
    return dict(version=plan['version'], folder=str(folder), start_date=plan['start_date'],
                end_date_exclusive=plan['end_date_exclusive'], status_counts=counts, rows=rows,
                candidate_count=len(rows), selected_subset=plan['selected_subset'],
                source_universe_code_matches_at_plan=plan.get('source_universe_code_matches_at_plan'),
                source_universes=plan.get('source_universes'),
                historical_universe_complete=False, historical_instrument_identity_verified=False,
                original_source_bodies_archived=False, native_body_hash_independently_replayable=False,
                holdout_contaminated_for_rights_hypothesis=True, accepted_for_signals=False,
                current_code_matches_capture_plan=_code_matches(plan, _source_files(plan)))


def main():
    parser = argparse.ArgumentParser(description='Opted-in retrospective rights-only historical price observation')
    parser.add_argument('--retrospective-rights-only', action='store_true', required=True)
    parser.add_argument('--universe-folder'); parser.add_argument('--folder')
    parser.add_argument('--pre-holdout-universe-folder', action='append', default=[])
    parser.add_argument('--discovery-folder', action='append', default=[], help='reviewed exact-name search archive')
    parser.add_argument('--start'); parser.add_argument('--end'); parser.add_argument('--ins-code', action='append')
    parser.add_argument('--max-new', type=int, default=20); parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--review-only', action='store_true')
    args = parser.parse_args()
    if args.folder:
        if args.universe_folder or args.pre_holdout_universe_folder or args.discovery_folder or args.start or args.end or args.ins_code:
            parser.error('Resume with --folder alone')
        folder = _folder(args.folder)
    else:
        if not (args.universe_folder or args.pre_holdout_universe_folder or args.discovery_folder) or not args.start or not args.end or args.review_only:
            parser.error('New capture needs at least one universe, --start and --end')
        folder = create_plan(args.universe_folder,
                 settings.DATA_DIR / 'private' / 'rights-retrospective-history' / ('RHIST-' + uuid.uuid4().hex),
                 args.start, args.end, codes=args.ins_code, retrospective_rights_only=args.retrospective_rights_only,
                 pre_holdout_universe_folders=args.pre_holdout_universe_folder, discovery_folders=args.discovery_folder)
    print('Frozen plan:', folder, flush=True)
    if args.review_only:
        out = review(folder)
        print(json.dumps({k: v for k, v in out.items() if k != 'rows'}, ensure_ascii=False))
    else:
        provider = TsetmcProvider(realtime=True)
        try:
            print(json.dumps(capture(provider, folder, max_new=args.max_new, retry_failed=args.retry_failed),
                             ensure_ascii=False), flush=True)
        finally:
            provider.close()


if __name__ == '__main__':
    main()
