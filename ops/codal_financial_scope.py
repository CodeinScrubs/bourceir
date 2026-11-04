"""Offline scope census from bound native Codal search bytes; no financial cells."""

import argparse
import hashlib
import json
import os
import re
import uuid

from config import settings
from core.codal_financial_scope import financial_scope_catalog
from core.opportunity_radar import digest, stamp
from core.timeutils import now_tehran_iso
from ops.codal_monthly_vintage import CODE_FILES as SEARCH_REPLAY_FILES
from ops.codal_monthly_vintage import MAX_BYTES, _reassess_search, _snapshot
from ops.codal_operating import _folder, _read
from ops.report_files import read_private_report, write_private_report

VERSION = 'codal-financial-scope-report-v2'
CODE_FILES = tuple(sorted(set(SEARCH_REPLAY_FILES) | {
    'core/codal_financial_scope.py', 'ops/codal_financial_scope.py'}))


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _derive(folder, root, *, calculated_at):
    reassessed, bodies = _reassess_search(folder, root)
    plan = read_private_report(folder/'plan.json')
    if (plan['instrument']['instrument_type_code'] not in (300, 303)
            or reassessed['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or not stamp(reassessed['completed_at']) <= stamp(calculated_at) <= stamp(now_tehran_iso())):
        raise ValueError('Financial scope, cutoff or calculation time differs')
    catalog = financial_scope_catalog(reassessed['metadata_summary']['letters'],
        expected_symbol=plan['instrument']['symbol_fa'], cutoff=reassessed['cutoff'])
    return dict(version=VERSION, available_at=calculated_at, calculated_at=calculated_at,
        search_reassessment=reassessed, catalog=catalog, network_requests=0,
        database_read=False, database_writes=False, holdout_opened=False,
        financial_values_read=False, accepted_for_features=False,
        accepted_for_signals=False, profitability_proven=False), bodies


def capture_financial_scope(search_archive):
    source = _folder(search_archive, 'codal-search', 'SEARCH-')
    code = {'code/'+n: (settings.BASE_DIR/n).read_bytes() for n in CODE_FILES}
    calculated = now_tehran_iso()
    result, originals = _derive(source, source.parent.parent, calculated_at=calculated)
    saved = {'input/'+source.name+'/'+n: b for n, b in originals.items()} | code
    if any(b != (settings.BASE_DIR/n.removeprefix('code/')).read_bytes() for n, b in code.items()):
        raise ValueError('Financial scope code changed during calculation')
    body = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False).encode()
    if len(body) > 16*1024*1024 or len(saved)+2 > 256 or sum(map(len, saved.values()))+len(body) > MAX_BYTES:
        raise ValueError('Financial scope evidence exceeds its byte or file budget')
    folder = _folder(settings.DATA_DIR/'private/codal-financial-scope'/('FINSCOPE-'+uuid.uuid4().hex),
        'codal-financial-scope', 'FINSCOPE-')
    folder.mkdir(parents=True, exist_ok=False)
    for name, content in saved.items():
        path = folder/name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    write_private_report(folder/'result.json', result)
    files = {n: _sha(b) for n, b in saved.items()}
    files['result.json'] = _sha((folder/'result.json').read_bytes())
    write_private_report(folder/'manifest.json', dict(version=VERSION, id=folder.name,
        search_archive='input/'+source.name, files_sha256=files))
    return dict(folder=str(folder), **review_financial_scope(folder))


def review_financial_scope(folder):
    folder = _folder(folder, 'codal-financial-scope', 'FINSCOPE-')
    _read(folder/'manifest.json', folder, limit=1024*1024)
    manifest = read_private_report(folder/'manifest.json')
    if (set(manifest) != {'version', 'id', 'search_archive', 'files_sha256'}
            or manifest['version'] != VERSION or manifest['id'] != folder.name
            or not isinstance(manifest['search_archive'], str)
            or not re.fullmatch('input/SEARCH-[0-9a-f]{32}', manifest['search_archive'])):
        raise ValueError('Financial scope archive identity or input differs')
    bodies = _snapshot(folder, folder)
    if manifest['files_sha256'] != {n: _sha(b) for n, b in bodies.items() if n != 'manifest.json'}:
        raise ValueError('Financial scope archive fingerprint differs')
    if any(bodies.get('code/'+n) != (settings.BASE_DIR/n).read_bytes() for n in CODE_FILES):
        raise ValueError('Financial scope decoder differs from the preserved code')
    recorded = read_private_report(folder/'result.json')
    expected, originals = _derive(folder/manifest['search_archive'], folder,
        calculated_at=recorded.get('calculated_at'))
    names = {'manifest.json', 'result.json', *('code/'+n for n in CODE_FILES)}
    names.update(manifest['search_archive']+'/'+n for n in originals)
    if names != set(bodies) or digest(recorded) != digest(expected):
        raise ValueError('Financial scope result differs from exact source replay')
    return dict(result=recorded, exact_source_replay=True, network_requests=0,
        accepted_for_features=False, accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('capture').add_argument('--search', required=True)
    sub.add_parser('review').add_argument('--archive', required=True)
    args = parser.parse_args(argv)
    result = (capture_financial_scope(args.search) if args.command == 'capture'
        else review_financial_scope(args.archive))
    print(json.dumps(result, ensure_ascii=True, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
