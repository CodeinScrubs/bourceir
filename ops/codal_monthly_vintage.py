"""Offline monthly-revision receipts, preserving raw search and document archives.

Reassessment executes current code only. Changed archived decoder bytes are
reported, not silently relabeled as an original successful review. No HTTP,
broker/account reads, model fitting or feature-store writes occur here.
"""

import argparse
import hashlib
import json
import os
import re
import uuid

from config import settings
from core.codal_monthly_sales import VERSION as SALES_VERSION
from core.codal_monthly_vintage import FALSE_FLAGS, monthly_version, select_monthly_version
from core.codal_monthly_vintage import VERSION as VINTAGE_VERSION
from core.codal_search import VERSION as SEARCH_VERSION
from core.opportunity_radar import digest, stamp
from core.text import normalize_symbol, to_en_digits
from core.timeutils import jalali_to_iso, now_tehran_iso
from ops import codal_search as search
from ops.codal_operating import _folder, _input, _inventory, _read
from ops.report_files import read_private_report, write_private_report

VERSION = 'codal-monthly-vintage-report-v2'
MAX_BYTES = 64*1024*1024
MAX_RESULT_BYTES = 16*1024*1024
MAX_TABLES = 8
CODE_FILES = tuple(sorted(set(search.SOURCE_FILES) | {
    'core/codal_monthly_sales.py', 'core/codal_monthly_vintage.py', 'core/codal_revenue.py',
    'core/codal_operating.py', 'core/codal_tables.py', 'core/opportunity_radar.py',
    'ops/codal_monthly_vintage.py', 'ops/codal_operating.py', 'ops/codal_tables.py', 'ops/codal_documents.py'}))


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _snapshot(folder, root):
    names = _inventory(folder)
    bodies, total = {}, 0
    for name in sorted(names):
        bodies[name] = _read(folder/name, root)
        total += len(bodies[name])
        if total > MAX_BYTES:
            raise ValueError('Monthly evidence exceeds its total byte budget')
    return bodies


def _json(folder, name):
    return read_private_report(folder/name)


def _reassess_search(folder, root):
    """Check every preserved receipt and denominator under an explicit new review."""
    bodies = _snapshot(folder, root)
    manifest, plan, result = (_json(folder, n) for n in ('manifest.json', 'plan.json', 'result.json'))
    if (set(manifest) != {'version', 'files'} or manifest['version'] != SEARCH_VERSION
            or manifest['files'] != {n: _sha(b) for n, b in bodies.items() if n != 'manifest.json'}):
        raise ValueError('Search archive fingerprint differs')
    keys = {'version', 'study_id', 'registered_at', 'instrument', 'start', 'end_exclusive', 'cutoff',
        'max_pages', 'letter_type', 'requests', 'code_sha256', 'metadata_source', *search.FALSE_FLAGS, *search.EXTRA_FALSE}
    if (set(plan) != keys or plan['version'] != SEARCH_VERSION or plan['study_id'] != folder.name
            or plan['metadata_source'] != 'caller_supplied_current_ordinary_metadata'
            or any(plan[k] is not False for k in (*search.FALSE_FLAGS, *search.EXTRA_FALSE))):
        raise ValueError('Search plan identity or evidence boundary differs')
    requests = search._requests(plan['instrument'], plan['start'], plan['end_exclusive'], plan['cutoff'],
        plan['max_pages'], plan['letter_type'])
    if (digest(requests) != digest(plan['requests']) or set(plan['code_sha256']) != set(search.SOURCE_FILES)
            or set(result) != {'version', 'study_id', 'completed_at', 'cases', 'summary'}
            or result['version'] != SEARCH_VERSION or result['study_id'] != folder.name
            or len(result['cases']) != len(requests)
            or not stamp(plan['registered_at']) <= stamp(result['completed_at']) <= stamp(now_tehran_iso())):
        raise ValueError('Search scope, completion time or denominator differs')
    changed = []
    for name, wanted in plan['code_sha256'].items():
        preserved = bodies['code/'+name]
        if _sha(preserved) != wanted:
            raise ValueError('Preserved search decoder fingerprint differs')
        if preserved != search._code(name):
            changed.append(name)
    expected = {'manifest.json', 'plan.json', 'result.json', *('code/'+n for n in search.SOURCE_FILES)}
    cases = []
    for index, (request, case) in enumerate(zip(requests, result['cases'])):
        prefix = f'page-{index+1:03d}'
        name, assessment_name = prefix+'.json', prefix+'-assessment.json'
        expected.update((name, assessment_name))
        saved = _json(folder, name)
        observation, body_name, body = saved.get('observation'), None, None
        if observation is not None:
            body_name = 'raw/'+prefix+'.json'
            expected.add(body_name)
            body = bodies[body_name]
            if not stamp(plan['registered_at']) <= stamp(observation['requested_at']) <= stamp(observation['received_at']) <= stamp(result['completed_at']):
                raise ValueError('Search attempt time escapes its preserved registration')
        assessed = search._assessment(saved, body, request)
        actual = dict(request=request, requested=saved['requested'], observation_file=name,
            body_file=body_name, assessment_file=assessment_name)
        if digest(actual) != digest(case) or digest(assessed) != digest(_json(folder, assessment_name)):
            raise ValueError('Current raw search reassessment differs from preserved metadata')
        cases.append(dict(actual, assessment=assessed))
    summary = search._summary(plan, cases)
    if expected != set(bodies) or digest(summary) != digest(result['summary']):
        raise ValueError('Search inventory or recomputed denominator differs')
    if any(_read(folder/n, root) != b for n, b in bodies.items()):
        raise ValueError('Search evidence changed during reassessment')
    return dict(search_id=folder.name, cutoff=plan['cutoff'], metadata_summary=summary,
        registered_at=plan['registered_at'], completed_at=result['completed_at'],
        reassessment_mode='preserved_raw_exact_reassessment', original_code_matches_current=not changed,
        changed_code_files=sorted(changed), original_review_rerun=False, archived_code_executed=False,
        all_page_assessments_and_summary_equal_preserved=True), bodies


def _decisions(values):
    if (not isinstance(values, list) or not 1 <= len(values) <= 32
            or any(not isinstance(v, str) or stamp(v) > stamp(now_tehran_iso()) for v in values)
            or len(set(values)) != len(values)):
        raise ValueError('One to 32 unique nonfuture aware decision times required')
    return sorted(values, key=stamp)


def _monthly_identity(letter, cutoff):
    title = to_en_digits(normalize_symbol(letter['title']))
    periods = re.findall(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', title)
    if (letter['letter_code'] != 'ن-30' or letter['source_estimate'] is not False
            or 'گزارش فعالیت ماهانه دوره 1 ماهه منتهی به' not in title or len(periods) != 1
            or not jalali_to_iso(periods[0])[:10] <= letter['source_published_at'][:10] < cutoff):
        raise ValueError('Monthly native period must precede publication and sealed cutoff')
    return (normalize_symbol(letter['symbol_fa']), normalize_symbol(letter['company_name']), periods[0])


def _bound_revision_inventory(tables, letters, root, cutoff):
    """Require every known same-period version BEFORE interpreting any cells."""
    bound, identities, supplied = [], set(), set()
    for folder in tables:
        _read(folder/'manifest.json', root)
        manifest = _json(folder, 'manifest.json')
        letter = letters.get(manifest.get('expected_tracing_no'))
        if letter is None or not letter['source_published_at'][:10] < cutoff:
            raise ValueError('Monthly document lacks unsealed native search metadata')
        identity = _monthly_identity(letter, cutoff)
        if letter['tracing_no'] in supplied:
            raise ValueError('Duplicate monthly tracing number across source archives')
        identities.add(identity)
        supplied.add(letter['tracing_no'])
        bound.append((folder, letter))
    if len(identities) != 1:
        raise ValueError('One issuer and period per revision selection required')
    symbol, company, period = next(iter(identities))
    known = set()
    for letter in letters.values():
        title = to_en_digits(normalize_symbol(letter['title']))
        if (letter['letter_code'] == 'ن-30'
                and normalize_symbol(letter['symbol_fa']) == symbol
                and normalize_symbol(letter['company_name']) == company
                and period in re.findall(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', title)):
            if _monthly_identity(letter, cutoff) != (symbol, company, period):
                raise ValueError('Ambiguous same-period monthly metadata')
            known.add(letter['tracing_no'])
    if known != supplied:
        raise ValueError('Known monthly revision source missing: '+','.join(map(str, sorted(known-supplied))))
    return bound, sorted(known)


def _revision_links(versions):
    by_trace = {v['tracing_no']: v for v in versions}
    links = set()
    for version in versions:
        for key in ('corrects_tracing_no', 'later_revision_banner_tracing_no'):
            trace = version[key]
            if trace is None:
                continue
            if trace not in by_trace:
                raise ValueError('Explicit monthly revision source missing: '+str(trace))
            older, newer = (trace, version['tracing_no']) if key == 'corrects_tracing_no' else (version['tracing_no'], trace)
            if not stamp(by_trace[older]['source_published_at_claim']) < stamp(by_trace[newer]['source_published_at_claim']):
                raise ValueError('Explicit monthly revision publication chronology contradicts its link')
            links.add((older, newer))
    return [dict(older_tracing_no=a, newer_tracing_no=b) for a, b in sorted(links)]


def _calculate(search_folder, tables, root, *, decisions, calculated):
    reassessed, search_bodies = _reassess_search(search_folder, root)
    cutoff = reassessed['cutoff']
    letters = {v['tracing_no']: v for v in reassessed['metadata_summary']['letters']}
    bound, known = _bound_revision_inventory(tables, letters, root, cutoff)
    versions, originals = [], {search_folder.name: search_bodies}
    for folder, letter in bound:
        loaded, bodies = _input(folder, root)
        if set(bodies) != _inventory(folder):
            raise ValueError('Monthly source contains an unbound or missing member')
        if stamp(calculated) < stamp(loaded['manifest']['available_at']):
            raise ValueError('Monthly calculation precedes source availability')
        version = monthly_version(loaded, letter, source_bytes=bodies['tables.json'],
            visible_text=bodies['report.txt'].decode('utf-8'))
        if not version['period_end_iso'] < cutoff:
            raise ValueError('Monthly period crosses the sealed boundary')
        versions.append(version)
        originals[folder.name] = bodies
    if stamp(calculated) < stamp(reassessed['completed_at']):
        raise ValueError('Monthly calculation precedes search availability')
    # Also enforces one issuer/period and unique tracing numbers for this
    # deliberately bounded revision study; no data-dependent period selection.
    selections = [select_monthly_version(versions, decision_at=d, clock=c)
        for d in decisions for c in ('observed', 'source_claim')]
    links = _revision_links(versions)
    result = dict(version=VERSION, sales_version=SALES_VERSION, vintage_version=VINTAGE_VERSION,
        calculated_at=calculated, available_at=calculated, search_reassessment=reassessed,
        versions=sorted(versions, key=lambda v: (stamp(v['source_published_at_claim']), v['tracing_no'])),
        selections=selections, source_archives=sorted(originals),
        known_revision_inventory=dict(tracing_numbers=known, all_known_sources_captured=True,
            explicit_revision_links=links, bounded_search_is_complete_history=False),
        source_claim_clock_is_reconstruction_only=True, model_fitting_permitted=False,
        holdout_opened=False, database_read=False, database_writes=False, network_requests=0,
        **dict.fromkeys(FALSE_FLAGS, False))
    return result, originals


def capture_monthly_vintage(search_archive, table_archives, *, decisions):
    decisions = _decisions(decisions)
    source = _folder(search_archive, 'codal-search', 'SEARCH-')
    if (not isinstance(table_archives, list) or not 1 <= len(table_archives) <= MAX_TABLES
            or len(set(map(str, table_archives))) != len(table_archives)):
        raise ValueError('One to eight unique monthly table archives required')
    tables = [_folder(f, 'codal-tables', 'CODALTABLE-') for f in table_archives]
    code = {'code/'+name: (settings.BASE_DIR/name).read_bytes() for name in CODE_FILES}
    calculated = now_tehran_iso()
    result, originals = _calculate(source, tables, source.parent.parent, decisions=decisions, calculated=calculated)
    saved = {'input/'+name+'/'+n: body for name, files in originals.items() for n, body in files.items()}
    if any(body != (settings.BASE_DIR/name.removeprefix('code/')).read_bytes() for name, body in code.items()):
        raise ValueError('Monthly replay code changed during calculation')
    saved.update(code)
    body = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(body) > MAX_RESULT_BYTES or sum(map(len, saved.values()))+len(body) > MAX_BYTES or len(saved)+2 > 256:
        raise ValueError('Monthly evidence exceeds its result, file or total byte budget')
    target = _folder(settings.DATA_DIR/'private/codal-monthly-vintage'/('VINTAGE-'+uuid.uuid4().hex),
        'codal-monthly-vintage', 'VINTAGE-')
    target.mkdir(parents=True, exist_ok=False)
    for name, content in saved.items():
        path = target/name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    write_private_report(target/'result.json', result)
    files = {n: _sha(b) for n, b in saved.items()}
    files['result.json'] = _sha((target/'result.json').read_bytes())
    write_private_report(target/'manifest.json', dict(version=VERSION, id=target.name,
        search_archive='input/'+source.name, table_archives=['input/'+f.name for f in tables],
        decisions=decisions, files_sha256=files))
    return dict(folder=str(target), **review_monthly_vintage(target))


def review_monthly_vintage(folder):
    folder = _folder(folder, 'codal-monthly-vintage', 'VINTAGE-')
    _read(folder/'manifest.json', folder, limit=1024*1024)
    manifest = _json(folder, 'manifest.json')
    if (set(manifest) != {'version', 'id', 'search_archive', 'table_archives', 'decisions', 'files_sha256'}
            or manifest['version'] != VERSION or manifest['id'] != folder.name
            or not isinstance(manifest['table_archives'], list)
            or not 1 <= len(manifest['table_archives']) <= MAX_TABLES
            or len(set(manifest['table_archives'])) != len(manifest['table_archives'])
            or not isinstance(manifest['search_archive'], str)
            or not re.fullmatch('input/SEARCH-[a-f0-9]{32}', manifest['search_archive'])
            or any(not isinstance(n, str) or not re.fullmatch('input/CODALTABLE-[a-f0-9]{32}', n) for n in manifest['table_archives'])):
        raise ValueError('Monthly archive identity or input inventory differs')
    if _decisions(manifest['decisions']) != manifest['decisions']:
        raise ValueError('Monthly decision chronology differs')
    bodies = _snapshot(folder, folder)
    if manifest['files_sha256'] != {n: _sha(b) for n, b in bodies.items() if n != 'manifest.json'}:
        raise ValueError('Monthly archive fingerprint differs')
    if any(bodies['code/'+n] != (settings.BASE_DIR/n).read_bytes() for n in CODE_FILES):
        raise ValueError('Current monthly replay code differs from the preserved decoder')
    recorded = _json(folder, 'result.json')
    calculated = recorded.get('calculated_at')
    if stamp(calculated) > stamp(now_tehran_iso()):
        raise ValueError('Monthly calculation cannot be in the future')
    expected, originals = _calculate(folder/manifest['search_archive'],
        [folder/n for n in manifest['table_archives']], folder, decisions=manifest['decisions'], calculated=calculated)
    names = {'manifest.json', 'result.json', *('code/'+n for n in CODE_FILES)}
    names.update('input/'+name+'/'+n for name, files in originals.items() for n in files)
    if set(bodies) != names or digest(recorded) != digest(expected):
        raise ValueError('Monthly result or inventory differs from exact source replay')
    return dict(status='observed', result=recorded, source_files_verified=len(bodies)-1,
        exact_source_replay=True, network_requests=0, accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    capture = sub.add_parser('capture')
    capture.add_argument('--search', required=True)
    capture.add_argument('--table', action='append', required=True)
    capture.add_argument('--decision-at', action='append', required=True)
    review = sub.add_parser('review')
    review.add_argument('--archive', required=True)
    args = parser.parse_args(argv)
    result = review_monthly_vintage(args.archive) if args.command == 'review' else capture_monthly_vintage(
        args.search, args.table, decisions=args.decision_at)
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
