"""Resumable, bounded capture of dated public instrument-universe observations."""
import argparse
import hashlib
import json
import uuid
from datetime import date, timedelta

from config import settings
from core.instrument_history import receipt_time
from core.rights_daily_retrospective import assess_day, day_request, identity_projection
from core.timeutils import TEHRAN_TZ, now_tehran, now_tehran_iso
from data_collector.source_cooldown import checked_transport_evidence
from data_collector.tsetmc import TsetmcProvider
from ops.report_files import read_private_report, write_private_report
from ops.rights_discovery import _canonical, _code_fingerprints, _code_matches, _exclusive_capture, _folder

PLAN_VERSION = 'rights-daily-retrospective-plan-v1'
SOURCE_FILES = ('core/rights_daily_retrospective.py', 'core/rights_discovery.py', 'core/instrument_history.py',
                'core/text.py', 'core/timeutils.py', 'data_collector/base.py', 'data_collector/tsetmc.py',
                'data_collector/source_cooldown.py', 'ops/rights_daily_retrospective.py', 'ops/report_files.py',
                'ops/rights_discovery.py', 'config/settings.py', 'config/default.yaml', 'requirements.lock.txt')


def create_plan(start, end, folder, *, retrospective_rights_only=False):
    if retrospective_rights_only is not True:
        raise ValueError('Explicit retrospective rights-only opt-in is required')
    day_request(start)
    # end is exclusive and cannot extend beyond tomorrow Tehran.
    if end > (now_tehran().date() + timedelta(days=1)).isoformat():
        raise ValueError('Retrospective plan cannot exceed today Tehran')
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError) as exc:
        raise ValueError('Canonical start and exclusive end dates required') from exc
    if last.isoformat() != end or first >= last or (last-first).days > 22000:
        raise ValueError('Invalid or excessive date interval')
    days = [(first + timedelta(days=i)).isoformat() for i in range((last-first).days)]
    folder = _folder(folder)
    plan = dict(version=PLAN_VERSION, registered_at=now_tehran_iso(), start_date=start, end_date_exclusive=end,
                dates=days, dates_sha256=hashlib.sha256(_canonical(days)).hexdigest(),
                capture_code_sha256=_code_fingerprints(SOURCE_FILES), historical_universe_complete=False,
                source_coverage_start_unknown=True, accepted_for_signals=False,
                retrospective_rights_only=True, holdout_contaminated_for_rights_hypothesis=True)
    folder.mkdir(parents=True, exist_ok=False)
    write_private_report(folder / 'plan.json', plan, forbidden_paths=(settings.DB_PATH,))
    return folder


def _read_plan(folder):
    p = read_private_report(folder / 'plan.json')
    days = p.get('dates')
    if (p.get('version') != PLAN_VERSION or not isinstance(days, list) or not 0 < len(days) <= 22000
            or p.get('dates_sha256') != hashlib.sha256(_canonical(days)).hexdigest()
            or p.get('start_date') != days[0]
            or p.get('end_date_exclusive') > (now_tehran().date() + timedelta(days=1)).isoformat()
            or p.get('retrospective_rights_only') is not True
            or p.get('holdout_contaminated_for_rights_hypothesis') is not True):
        raise ValueError('Frozen retrospective plan differs or exceeds today')
    for i, day in enumerate(days):
        day_request(day)
        if i and date.fromisoformat(day) != date.fromisoformat(days[i-1]) + timedelta(days=1):
            raise ValueError('Frozen days must be contiguous')
    if date.fromisoformat(p['end_date_exclusive']) != date.fromisoformat(days[-1])+timedelta(days=1):
        raise ValueError('Frozen end date differs')
    return p


def _records(folder, day, plan_sha, registered_at):
    directory = folder / 'observations' / day
    paths = sorted(directory.glob('*.json')) if directory.exists() else []
    if [p.name for p in paths] != [f'{i:04d}.json' for i in range(1, len(paths)+1)]:
        raise ValueError('Observation sequence has a gap')
    records = []
    for path in paths:
        record = read_private_report(path)
        if (record.get('version') != PLAN_VERSION or record.get('requested_date') != day
                or record.get('plan_sha256') != plan_sha
                or record.get('retrospective_rights_only') is not True
                or record.get('holdout_contaminated_for_rights_hypothesis') is not True):
            raise ValueError('Record belongs to another plan or day')
        checked_transport_evidence(record.get('transport_evidence'))
        attempted = receipt_time(record.get('attempted_at'))
        if not receipt_time(registered_at) <= attempted <= receipt_time(now_tehran_iso()):
            raise ValueError('Attempt is outside the plan and current clock')
        saved = record.get('observation')
        projection = record.get('identity_projection')
        if saved is None:
            assessment = ({'status': 'invalid_response'} if record.get('invalid_response') else assess_day(day, None))
        else:
            if (not isinstance(saved, dict) or not isinstance(projection, dict)
                    or saved.get('source') != 'tsetmc' or saved.get('url') != day_request(day)
                    or saved.get('requested_date') != day or saved.get('http_status') != 200
                    or type(saved.get('response_bytes')) is not int or not 0 < saved['response_bytes'] <= 2*1024*1024
                    or not isinstance(saved.get('body_sha256'), str) or len(saved['body_sha256']) != 64
                    or any(c not in '0123456789abcdef' for c in saved['body_sha256'])
                    or record.get('projection_sha256') != hashlib.sha256(_canonical(projection)).hexdigest()
                    or record.get('source_body_archived') is not False):
                raise ValueError('Identity projection or source receipt differs')
            requested, received = receipt_time(saved.get('requested_at')), receipt_time(saved.get('received_at'))
            if not receipt_time(registered_at) <= requested <= received <= attempted:
                raise ValueError('Source timestamps differ from registered attempt')
            if date.fromisoformat(day) > requested.astimezone(TEHRAN_TZ).date():
                raise ValueError('A future day cannot be observed')
            content = _canonical(projection)
            obs = saved | {'content': content, 'response_bytes': len(content),
                           'body_sha256': hashlib.sha256(content).hexdigest()}
            assessment = assess_day(day, obs)
        if assessment != record.get('assessment'):
            raise ValueError('Source response does not match recorded assessment')
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
        for day in plan['dates']:
            previous = _records(folder, day, plan_sha, plan['registered_at'])
            if previous:
                if previous[-1]['assessment']['status'] in ('observed', 'observed_empty_unverified'):
                    continue
                if not retry_failed:
                    stop = 'prior_failure_requires_explicit_retry'; break
            if count >= max_new:
                stop = 'batch_complete'; break
            if _code_matches(plan, SOURCE_FILES) is not True:
                stop = 'capture_code_changed'; break
            obs = provider.get_retrospective_day_universe_observation(day, retrospective_rights_only=True)
            try:
                assessment = assess_day(day, obs)
                if obs is not None:
                    requested, received = receipt_time(obs.get('requested_at')), receipt_time(obs.get('received_at'))
                    if not receipt_time(plan['registered_at']) <= requested <= received <= receipt_time(now_tehran_iso()):
                        raise ValueError('Source timestamps outside this attempt')
                    if date.fromisoformat(day) > requested.astimezone(TEHRAN_TZ).date():
                        raise ValueError('Source day is in the future')
            except (ValueError, TypeError):
                assessment = {'status': 'invalid_response'}
            saved, projection = None, None
            if assessment['status'] in ('observed', 'observed_empty_unverified'):
                projection = identity_projection(obs['content'])
                saved = {k: v for k, v in obs.items() if k != 'content'}
            record = dict(version=PLAN_VERSION, requested_date=day,
                          plan_sha256=plan_sha, attempted_at=now_tehran_iso(),
                          transport_evidence=checked_transport_evidence(getattr(provider, 'last_transport_evidence', None)),
                          observation=saved, assessment=assessment, identity_projection=projection,
                          projection_sha256=hashlib.sha256(_canonical(projection)).hexdigest() if projection else None,
                          invalid_response=assessment['status'] == 'invalid_response', source_body_archived=False,
                          retrospective_rights_only=True, holdout_contaminated_for_rights_hypothesis=True)
            directory = folder / 'observations' / day
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / f'{len(previous)+1:04d}.json'
            if path.exists():
                raise ValueError('Refusing to overwrite an observation')
            write_private_report(path, record, forbidden_paths=(settings.DB_PATH,))
            _records(folder, day, plan_sha, plan['registered_at'])
            count += 1
            if assessment['status'] not in ('observed', 'observed_empty_unverified'):
                stop = 'source_unavailable_or_invalid'; break
    return dict(folder=str(folder), new_attempts=count, stop_reason=stop or 'plan_processed',
                historical_universe_complete=False, accepted_for_signals=False,
                holdout_contaminated_for_rights_hypothesis=True)


def review(folder):
    folder = _folder(folder)
    plan = _read_plan(folder)
    plan_sha = hashlib.sha256(_canonical(plan)).hexdigest()
    counts, candidates, days = {}, {}, []
    for day in plan['dates']:
        records = _records(folder, day, plan_sha, plan['registered_at'])
        assessment = records[-1]['assessment'] if records else None
        status = assessment['status'] if assessment else 'not_attempted'
        counts[status] = counts.get(status, 0) + 1
        if assessment and status == 'observed':
            for candidate in assessment['candidates']:
                key = candidate['ins_code']
                item = candidates.setdefault(key, dict(first_seen=day, last_seen=day, sightings=0, names=[]))
                item['last_seen'] = day
                item['sightings'] += 1
                identity = dict(symbol=candidate['symbol'], instrument_id=candidate['instrument_id'])
                if identity not in item['names']:
                    item['names'].append(identity)
        summary = ({key: assessment.get(key) for key in ('source_row_count', 'candidate_count',
                                                        'native_date_verified')}
                   if assessment else None)
        days.append(dict(requested_date=day, status=status, attempts=len(records), assessment=summary))
    return dict(version=PLAN_VERSION, folder=str(folder), planned_days=len(days), status_counts=counts,
                candidate_codes=len(candidates), candidates=candidates, days=days,
                source_coverage_start_unknown=True, historical_universe_complete=False,
                native_day_identity_may_be_missing=True, source_completeness_verified=False,
                original_source_bodies_archived=False, native_body_hash_independently_replayable=False,
                current_code_matches_capture_plan=_code_matches(plan, SOURCE_FILES), accepted_for_signals=False,
                holdout_contaminated_for_rights_hypothesis=True)


def main():
    parser = argparse.ArgumentParser(description='Capture explicitly opted-in retrospective rights-only daily universe')
    parser.add_argument('--start'); parser.add_argument('--end', help='exclusive date')
    parser.add_argument('--retrospective-rights-only', action='store_true', required=True)
    parser.add_argument('--folder'); parser.add_argument('--max-new', type=int, default=20)
    parser.add_argument('--retry-failed', action='store_true'); parser.add_argument('--review-only', action='store_true')
    args = parser.parse_args()
    if args.folder:
        if args.start or args.end:
            parser.error('Resume with --folder, without start or end')
        folder = _folder(args.folder)
    else:
        if not args.start or not args.end or args.review_only:
            parser.error('New capture requires --start and --end')
        folder = create_plan(args.start, args.end,
                             settings.DATA_DIR / 'private' / 'rights-daily-retrospective' / ('RETRO-' + uuid.uuid4().hex),
                             retrospective_rights_only=args.retrospective_rights_only)
    print('Frozen plan:', folder, flush=True)
    if args.review_only:
        result = review(folder)
        print(json.dumps({k: v for k, v in result.items() if k not in ('days', 'candidates')}, ensure_ascii=False))
    else:
        provider = TsetmcProvider(realtime=True)
        try:
            print(json.dumps(capture(provider, folder, max_new=args.max_new, retry_failed=args.retry_failed),
                             ensure_ascii=False), flush=True)
        finally:
            provider.close()


if __name__ == '__main__':
    main()
