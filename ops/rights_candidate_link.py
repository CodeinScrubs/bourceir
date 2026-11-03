"""Review archives, then list possible same-symbol links without outcomes or network."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

from ops.report_files import read_private_report
from ops.rights_cohort_inventory import review_inventory
from ops.rights_registration import review_rights_study
from research.rights_candidate_link import candidate_links


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def review_links(inventory_folder, registration_folders):
    folder = Path(inventory_folder).resolve()
    inventory_path = folder / 'inventory.json'
    before = _sha(inventory_path)
    review_inventory(folder)
    saved = read_private_report(inventory_path)
    if _sha(inventory_path) != before:
        raise ValueError('Inventory changed during review')
    notices = []
    for source in registration_folders:
        source = Path(source).resolve()
        manifest_path, notice_path = source / 'manifest.json', source / 'notice.json'
        manifest_before, notice_before = _sha(manifest_path), _sha(notice_path)
        result = review_rights_study(source)
        notice = read_private_report(notice_path)
        if _sha(manifest_path) != manifest_before or _sha(notice_path) != notice_before:
            raise ValueError('Registration manifest or notice changed during review')
        facts = result['facts']
        if facts.get('status') != 'supported_primary_fact':
            raise ValueError('Registration facts are not supported by the declared primary text')
        notices.append({'tracing_no': notice['tracing_no'], 'symbol': facts['symbol'],
                        'meeting_date': facts['meeting_date'], 'study_id': source.name})
    if _sha(inventory_path) != before:
        raise ValueError('Inventory changed while reviewing registrations')
    return candidate_links(saved['inventory'], notices)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', required=True)
    parser.add_argument('--registration', required=True, action='append')
    args = parser.parse_args(argv)
    try:
        result = review_links(args.inventory, args.registration)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
