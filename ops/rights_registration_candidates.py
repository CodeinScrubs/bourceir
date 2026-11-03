"""Review local sources and index potential notice/start links without outcomes."""

import argparse
import hashlib
import json
import re
import sys
import uuid
from pathlib import Path

from config import settings
from core.codal_universe import assess_registration
from core.timeutils import now_tehran_iso
from ops.codal_universe import review as review_codal
from ops.report_files import read_private_report, write_private_report
from ops.rights_cohort_inventory import review_inventory
from research.rights_registration_candidates import VERSION, link_notices

CODE_FILES = ('research/rights_registration_candidates.py', 'ops/rights_registration_candidates.py',
              'ops/rights_cohort_inventory.py', 'ops/codal_universe.py', 'core/codal_universe.py',
              'core/codal_search.py', 'core/codal_documents.py', 'core/text.py', 'core/timeutils.py',
              'ops/report_files.py')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_files(folder):
    """Snapshot every source file, refusing a concurrent capture or unknown path."""
    if (folder / '.lock').exists():
        raise ValueError('A source capture is still active')
    files = {}
    for path in folder.iterdir():
        if path.is_symlink() or not path.is_file():
            raise ValueError('Unexpected source path')
        if path.name != 'plan.json' and not re.fullmatch(r'page-[0-9]{3}\.(?:json|bin)', path.name):
            raise ValueError('Unexpected source file')
        files[path.name] = _sha(path)
    return files


def build_index(inventory_folder, universe_folders):
    """Verify every source, then link same symbols without interpreting them as events."""
    code_hashes = {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
                   for name in CODE_FILES}
    inventory_folder = Path(inventory_folder).resolve()
    inventory_path = inventory_folder / 'inventory.json'
    before_inventory = _sha(inventory_path)
    review_inventory(inventory_folder)
    saved = read_private_report(inventory_path)
    if _sha(inventory_path) != before_inventory:
        raise ValueError('Inventory changed during review')
    if (not isinstance(universe_folders, list) or not universe_folders
            or len(universe_folders) > 100):
        raise ValueError('A bounded list of source studies is required')
    paths = [Path(source).resolve() for source in universe_folders]
    if len(paths) != len(set(paths)):
        raise ValueError('Duplicate source study')
    notices, sources = [], []
    for folder in paths:
        before = _source_files(folder)
        checked = review_codal(folder)
        plan = checked['plan']
        for page in range(1, checked['pages_attempted'] + 1):
            prefix = folder / f'page-{page:03d}'
            receipt = read_private_report(str(prefix) + '.json')
            if receipt['status'] != 'observed':
                continue
            result = assess_registration(Path(str(prefix) + '.bin').read_bytes(), receipt['observation'],
                start=plan['start'], end_exclusive=plan['end_exclusive'], cutoff=plan['cutoff'], page=page)
            notices.extend({**item, 'source_folder': str(folder)} for item in result['letters'])
        if before != _source_files(folder):
            raise ValueError('Notice source changed during review')
        sources.append(dict(folder=str(folder), start=plan['start'], end_exclusive=plan['end_exclusive'],
            mode=plan['mode'], holdout_start=plan['holdout_start'],
            stop_reason=checked['stop_reason'], rows_observed=checked['document_rows_observed'],
            files_sha256=before))
    if _sha(inventory_path) != before_inventory:
        raise ValueError('Inventory changed while notices were examined')
    if any(_source_files(folder) != item['files_sha256'] for folder, item in zip(paths, sources)):
        raise ValueError('Notice source changed after it was examined')
    if any(hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != value
           for name, value in code_hashes.items()):
        raise ValueError('Indexing code changed during review')
    holdout = settings.RESEARCH_CONFIG['holdout_start']
    uses_holdout = (saved['inventory']['end_date_exclusive'] > holdout
                    or any(source['end_exclusive'] > holdout for source in sources))
    return dict(version=VERSION, created_at=now_tehran_iso(), inventory_folder=str(inventory_folder),
        inventory_sha256=before_inventory, notice_sources=sources, code_sha256=code_hashes,
        index=link_notices(saved['inventory'], notices), network_requested=False,
        account_data_read=False, original_source_authenticity_verified=False,
        holdout_contaminated_for_rights_hypothesis=uses_holdout,
        independent_holdout_for_rights_hypothesis=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--universe', required=True, action='append')
    args = parser.parse_args(argv)
    result = build_index(args.inventory, args.universe)
    folder = settings.DATA_DIR / 'private' / 'rights-registration-candidates' / ('INDEX-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    target = write_private_report(folder / 'index.json', result, forbidden_paths=(settings.DB_PATH,))
    summary = {key: value for key, value in result['index'].items() if key != 'notices'}
    print(json.dumps({'folder': str(folder), 'report': str(target),
                     'holdout_contaminated_for_rights_hypothesis': result['holdout_contaminated_for_rights_hypothesis'],
                     'independent_holdout_for_rights_hypothesis': False, **summary}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    raise SystemExit(main())
