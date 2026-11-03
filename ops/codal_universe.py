"""Bounded, resumable public CODAL registration-notice metadata collection."""

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

from config import settings
from core.codal_search import stamp
from core.codal_universe import VERSION, assess_registration, registration_request
from core.timeutils import now_tehran_iso
from ops.report_files import read_private_report, write_private_report

SOURCE_FILES = ('core/codal_universe.py', 'core/codal_search.py', 'core/codal_documents.py',
                'core/text.py', 'core/timeutils.py', 'data_collector/codal.py',
                'ops/codal_universe.py', 'ops/report_files.py', 'config/settings.py',
                'config/default.yaml', 'requirements.lock.txt', 'pyproject.toml')


def code_hashes():
    return {name: hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            for name in SOURCE_FILES}


def create(*, start, end_exclusive, max_pages=100, descriptive=False):
    if type(descriptive) is not bool:
        raise ValueError('descriptive opt-in must be explicit')
    holdout = settings.RESEARCH_CONFIG['holdout_start']
    cutoff = end_exclusive if descriptive else holdout
    if type(max_pages) is not int or not 1 <= max_pages <= 100:
        raise ValueError('page capacity must be between 1 and 100')
    registration_request(start=start, end_exclusive=end_exclusive, cutoff=cutoff, page=max_pages)
    limit = (date.fromisoformat(now_tehran_iso()[:10])+timedelta(days=int(descriptive))).isoformat()
    if end_exclusive > limit:
        raise ValueError('future window is not supported')
    folder = (settings.DATA_DIR/'private/codal-registration-universe'/('UNIVERSE-'+uuid.uuid4().hex)).resolve()
    folder.mkdir(parents=True, exist_ok=False)
    plan = dict(version=VERSION, study_id=folder.name, registered_at=now_tehran_iso(), start=start,
                end_exclusive=end_exclusive, cutoff=cutoff, max_pages=max_pages, letter_type=28,
                mode='descriptive_retrospective' if descriptive else 'pre_holdout', holdout_start=holdout,
                code_sha256=code_hashes())
    write_private_report(folder/'plan.json', plan)
    return folder


def review(folder):
    """Recompute all successful pages; an incomplete page blocks continuation."""
    folder = Path(folder).resolve()
    root = (settings.DATA_DIR/'private/codal-registration-universe').resolve()
    if not folder.is_relative_to(root) or not re.fullmatch(r'UNIVERSE-[a-f0-9]{32}', folder.name):
        raise ValueError('invalid archive path')
    plan = read_private_report(folder/'plan.json')
    if (set(plan) != {'version', 'study_id', 'registered_at', 'start', 'end_exclusive', 'cutoff',
                     'max_pages', 'letter_type', 'mode', 'holdout_start', 'code_sha256'} or plan['version'] != VERSION
            or plan['study_id'] != folder.name or plan['letter_type'] != 28
            or plan['holdout_start'] != settings.RESEARCH_CONFIG['holdout_start']
            or plan['mode'] not in ('descriptive_retrospective', 'pre_holdout')
            or plan['cutoff'] != (plan['end_exclusive'] if plan['mode'] == 'descriptive_retrospective'
                                  else plan['holdout_start'])
            or plan['code_sha256'] != code_hashes()
            or type(plan['max_pages']) is not int or not 1 <= plan['max_pages'] <= 100
            or stamp(plan['registered_at']) > stamp(now_tehran_iso())):
        raise ValueError('invalid study plan')
    registration_request(start=plan['start'], end_exclusive=plan['end_exclusive'],
                         cutoff=plan['cutoff'], page=plan['max_pages'])
    limit = (date.fromisoformat(now_tehran_iso()[:10])
             + timedelta(days=int(plan['mode'] == 'descriptive_retrospective'))).isoformat()
    if plan['end_exclusive'] > limit:
        raise ValueError('future study window')
    expected = {'plan.json', '.lock'}
    seen = set()
    count = 0
    native_totals, native_pages = set(), set()
    stop = None
    for page in range(1, plan['max_pages']+1):
        name = f'page-{page:03d}'
        receipt_path, body_path = folder/(name+'.json'), folder/(name+'.bin')
        if not receipt_path.exists():
            if body_path.exists():
                raise ValueError('orphan body after interrupted write')
            break
        expected.add(name+'.json')
        receipt = read_private_report(receipt_path)
        if set(receipt) != {'page', 'status', 'recorded_at', 'observation'} or receipt['page'] != page:
            raise ValueError('invalid page receipt')
        if not stamp(plan['registered_at']) <= stamp(receipt['recorded_at']) <= stamp(now_tehran_iso()):
            raise ValueError('invalid receipt timestamp')
        if receipt['status'] not in ('observed', 'pagination_conflict'):
            if receipt['status'] not in ('unavailable', 'invalid_response', 'verification_required'):
                raise ValueError('invalid failure state')
            if receipt['observation'] is not None or body_path.exists():
                raise ValueError('failure must not masquerade as response')
            stop = receipt['status']
            break
        if not body_path.is_file():
            raise ValueError('receipt body missing')
        expected.add(name+'.bin')
        obs = receipt['observation']
        if not stamp(plan['registered_at']) <= stamp(obs['requested_at']) <= stamp(obs['received_at']) <= stamp(receipt['recorded_at']):
            raise ValueError('observation timestamps outside plan')
        result = assess_registration(body_path.read_bytes(), obs, start=plan['start'],
                                     end_exclusive=plan['end_exclusive'], cutoff=plan['cutoff'], page=page)
        if result['status'] == 'verification_required':
            raise ValueError('invalid stored verification response')
        ids = {x['tracing_no'] for x in result['letters']}
        conflict = bool(seen & ids or (native_totals and result['total_claim'] not in native_totals)
                        or (native_pages and result['page_claim'] not in native_pages)
                        or count + len(ids) > result['total_claim']
                        or (result['status'] == 'empty_page' and count != result['total_claim']))
        if conflict != (receipt['status'] == 'pagination_conflict'):
            raise ValueError('pagination conflict state inconsistent')
        if conflict:
            stop = 'pagination_conflict'
            break
        seen.update(ids)
        count += len(result['letters'])
        native_totals.add(result['total_claim'])
        native_pages.add(result['page_claim'])
        if len(native_totals) > 1 or len(native_pages) > 1 or count > result['total_claim']:
            raise ValueError('native pagination claim changed or contradicts observations')
        if result['status'] == 'empty_page':
            stop = 'observed_empty_page'
            break
    actual = {x.name for x in folder.iterdir()}
    if not actual <= expected:
        raise ValueError('unexpected file or non-contiguous pages')
    pages = sum(1 for x in actual if re.fullmatch(r'page-\d{3}\.json', x))
    if stop is None and pages == plan['max_pages']:
        stop = 'page_capacity_reached'
    return dict(plan=plan, pages_attempted=pages, document_rows_observed=count,
                distinct_tracing_numbers=len(seen), native_total_claims=sorted(native_totals),
                native_page_claims=sorted(native_pages), tracing_numbers=sorted(seen), stop_reason=stop,
                historical_coverage_verified=False, legal_event_count=None,
                historical_identity_verified=False, source_filter_semantics_verified=False,
                independent_holdout_for_this_hypothesis=False)


def collect(folder, provider, *, max_new=1):
    if type(max_new) is not int or not 1 <= max_new <= 100:
        raise ValueError('invalid request budget')
    folder = Path(folder).resolve()
    # Validate before creating a lock outside the allowed archive root.
    review(folder)
    lock = folder/'.lock'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    try:
        for _ in range(max_new):
            state = review(folder)
            if state['stop_reason']:
                break
            plan = state['plan']
            page = state['pages_attempted']+1
            try:
                observed = provider.get_registration_universe_observation(
                    start=plan['start'], end_exclusive=plan['end_exclusive'], page=page,
                    descriptive=plan['mode'] == 'descriptive_retrospective')
                if observed is None:
                    status, observation, body = 'unavailable', None, None
                else:
                    body = observed['content']
                    observation = {k: v for k, v in observed.items() if k != 'content'}
                    result = assess_registration(body, observation, start=plan['start'],
                                                 end_exclusive=plan['end_exclusive'],
                                                 cutoff=plan['cutoff'], page=page)
                    status = 'verification_required' if result['status'] == 'verification_required' else 'observed'
                    if status != 'observed':
                        observation, body = None, None
                    else:
                        ids = {x['tracing_no'] for x in result['letters']}
                        if (ids.intersection(state['tracing_numbers'])
                                or (state['native_total_claims'] and result['total_claim'] not in state['native_total_claims'])
                                or (state['native_page_claims'] and result['page_claim'] not in state['native_page_claims'])
                                or state['document_rows_observed'] + len(ids) > result['total_claim']
                                or (result['status'] == 'empty_page'
                                    and state['document_rows_observed'] != result['total_claim'])):
                            status = 'pagination_conflict'
            except (ValueError, TypeError, KeyError, OSError):
                status, observation, body = 'invalid_response', None, None
            prefix = folder/f'page-{page:03d}'
            if body is not None:
                with open(str(prefix)+'.bin', 'xb') as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
            write_private_report(str(prefix)+'.json', dict(page=page, status=status, recorded_at=now_tehran_iso(),
                                                          observation=observation))
            state = review(folder)
            if state['stop_reason']:
                break
            time.sleep(settings.RATE_LIMIT_DELAY)
        return review(folder)
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    new = sub.add_parser('create')
    new.add_argument('--start', required=True)
    new.add_argument('--end-exclusive', required=True)
    new.add_argument('--max-pages', type=int, default=100)
    new.add_argument('--include-post-holdout-descriptive', action='store_true',
                     help='explicit retrospective descriptive use; not independent holdout evidence')
    run = sub.add_parser('collect')
    run.add_argument('--study', required=True)
    run.add_argument('--max-new', type=int, default=1)
    check = sub.add_parser('review')
    check.add_argument('--study', required=True)
    args = parser.parse_args(argv)
    if args.command == 'create':
        result = {'folder': str(create(start=args.start, end_exclusive=args.end_exclusive, max_pages=args.max_pages,
                                       descriptive=args.include_post_holdout_descriptive))}
    elif args.command == 'review':
        result = review(args.study)
    else:
        from data_collector.codal import CodalProvider
        provider = CodalProvider()
        try:
            result = collect(args.study, provider, max_new=args.max_new)
        finally:
            provider.session.close()
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
