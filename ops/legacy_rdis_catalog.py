"""Build an offline, source-hashed catalogue from an archived public form."""

import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path

from config import settings
from core.instrument_history import receipt_time
from core.legacy_rdis_catalog import MAX_BYTES, VERSION, parse_legacy_catalog
from core.timeutils import now_tehran_iso
from ops.report_files import private_report_path, read_private_report, write_private_report

CODE_FILES = ('core/legacy_rdis_catalog.py', 'core/text.py', 'ops/legacy_rdis_catalog.py',
              'ops/report_files.py', 'core/instrument_history.py', 'core/timeutils.py')


def build_catalog(source_folder):
    source = Path(source_folder).resolve()
    receipt_path = private_report_path(source/'receipt.json', forbidden_paths=(settings.DB_PATH,))
    page_path = source/'page.html'
    if page_path.is_symlink() or not page_path.is_file():
        raise ValueError('A local archived page is required')
    receipt_bytes = receipt_path.read_bytes()
    receipt = read_private_report(receipt_path)
    if (set(receipt) != {'archive_url', 'retrieved_at', 'status', 'sha256', 'bytes',
                         'historical_completeness_verified'}
            or not isinstance(receipt['archive_url'], str)
            or not re.fullmatch(r'https://web\.archive\.org/web/[0-9]{14}id_/'
                                r'http://www\.rdis\.ir/CMPAnnouncements\.asp', receipt['archive_url'])
            or type(receipt['status']) is not int or receipt['status'] != 200
            or type(receipt['bytes']) is not int or not 0 < receipt['bytes'] <= MAX_BYTES
            or receipt['historical_completeness_verified'] is not False
            or receipt_time(receipt['retrieved_at']) > receipt_time(now_tehran_iso())):
        raise ValueError('Invalid archived-source receipt')
    with page_path.open('rb') as stream:
        body = stream.read(MAX_BYTES+1)
    digest = hashlib.sha256(body).hexdigest()
    if len(body) != receipt['bytes'] or digest != receipt['sha256']:
        raise ValueError('Archived page and receipt differ')
    before_code = {name: hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
                   for name in CODE_FILES}
    result = parse_legacy_catalog(body)
    if (hashlib.sha256(page_path.read_bytes()).hexdigest() != digest or receipt_path.read_bytes() != receipt_bytes
            or any(hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != val
                   for name, val in before_code.items())):
        raise ValueError('Source or code changed while parsing')
    return dict(version=VERSION, created_at=now_tehran_iso(), source_folder=str(source),
                archive_url=receipt['archive_url'], retrieved_at=receipt['retrieved_at'],
                source_page_sha256=digest, source_receipt_sha256=hashlib.sha256(receipt_bytes).hexdigest(),
                code_sha256=before_code, catalogue=result, network_requested=False,
                account_data_read=False, original_source_authenticity_verified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    args = parser.parse_args()
    result = build_catalog(args.source)
    folder = settings.DATA_DIR/'private/legacy-rdis-catalogs'/('CATALOG-'+uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    target = write_private_report(folder/'catalog.json', result, forbidden_paths=(settings.DB_PATH,))
    print(json.dumps({'folder': str(folder), 'report': str(target),
                      **{k: v for k, v in result['catalogue'].items()
                         if k not in ('companies', 'report_types', 'years')}}, ensure_ascii=False))


if __name__ == '__main__':
    main()
