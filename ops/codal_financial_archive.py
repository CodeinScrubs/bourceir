"""Private, append-only snapshots of public, unverified financial statement cells.

This archive is not a point-in-time financial-data feed.  A local hash checks
subsequent consistency; it is not an independently witnessed timestamp.
"""

import hashlib
import json
import os
import re
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from config import settings
from core.codal_documents import public_document_url
from core.codal_financial_cells import _address_matches_sequence
from core.codal_tables import _unique
from core.timeutils import now_tehran_iso
from ops.codal_documents import _stamp

ARCHIVE_VERSION = 'codal-financial-cells-archive-v1'
SOURCE_FILES = ('core/codal_financial_cells.py', 'core/codal_financial_sheets.py',
                'core/codal_tables.py', 'core/codal_documents.py', 'data_collector/codal.py',
                'ops/codal_financial_archive.py', 'ops/codal_documents.py',
                'core/timeutils.py', 'core/text.py', 'config/settings.py',
                'config/default.yaml', 'requirements.lock.txt')
UNVERIFIED = ('document_identity_verified', 'publication_time_verified',
              'historical_availability_verified', 'numeric_facts_reviewed',
              'units_and_period_columns_verified', 'complete_revision_history_verified',
              'accepted_for_features', 'accepted_for_signals', 'profitability_proven')
DATA_UNVERIFIED = ('numeric_facts_reviewed', 'units_and_period_columns_verified',
                   'document_identity_verified', 'publication_time_verified',
                   'formulas_executed', 'accepted_for_features', 'accepted_for_signals')
_SHEET_ID = re.compile(r'-?(?:0|[1-9][0-9]{0,9})\Z', re.ASCII)


def _sha(body):
    return hashlib.sha256(body).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def _read_json(path):
    def invalid_constant(value):
        raise ValueError('Non-finite value in archive')
    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique,
                      parse_constant=invalid_constant)


def _trace(value):
    if type(value) is not int or not 0 < value < 2**53:
        raise ValueError('A positive expected tracing number is required')
    return value


def _check_cell_value(cell):
    """Keep absent, null, zero and their original JSON types distinct."""
    present = cell.get('value_present')
    kind = cell.get('raw_value_kind')
    if type(present) is not bool or 'raw_value' not in cell or type(kind) is not str:
        raise ValueError('Financial cell value metadata is invalid')
    value = cell['raw_value']
    if kind == 'absent':
        valid = not present and value is None
    elif kind == 'null':
        valid = present and value is None
    elif kind == 'string':
        valid = (present and type(value) is str and len(value) <= 20000
                 and not any(0xD800 <= ord(char) <= 0xDFFF for char in value))
    elif kind == 'boolean':
        valid = present and type(value) is bool
    elif kind == 'integer':
        valid = present and type(value) is int and -(2**63) <= value < 2**63
    elif kind == 'decimal':
        valid = False
        if present and type(value) is str and len(value) <= 100:
            try:
                number = Decimal(value)
                valid = number.is_finite() and str(number) == value
            except InvalidOperation:
                pass
    else:
        valid = False
    if not valid:
        raise ValueError('Financial cell value metadata conflicts with raw value')


def _requested_sheet_id(url):
    """Read only an explicit, unique, canonical sheetId in a validated public URL."""
    url = public_document_url(url)
    values = [value for key, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)
              if key.casefold() == 'sheetid']
    if not values:
        return None
    if len(values) != 1 or not _SHEET_ID.fullmatch(values[0]) or values[0] == '-0':
        raise ValueError('Explicit sheetId must be unique and a bounded canonical integer')
    return int(values[0])


def _sheet_evidence(source_url, effective_url, data, sheets):
    source_id, effective_id = _requested_sheet_id(source_url), _requested_sheet_id(effective_url)
    if source_id is not None and effective_id is not None and source_id != effective_id:
        raise ValueError('Requested and effective sheet IDs conflict')
    requested = source_id if source_id is not None else effective_id
    selected = sheets['selected_sheet_id']
    codes = [table.get('sheet_code') for table in data['tables']]
    known_codes = bool(codes) and all(type(code) is int for code in codes)
    if selected is not None and selected >= 0 and known_codes and selected not in codes:
        raise ValueError('Selected sheet ID conflicts with payload sheet codes')
    if requested is None:
        return dict(source_sheet_id=source_id, effective_sheet_id=effective_id, sheet_consistency='unknown')
    if known_codes and requested not in codes:
        raise ValueError('Requested sheet ID conflicts with payload sheet codes')
    if selected is None:
        return dict(source_sheet_id=source_id, effective_sheet_id=effective_id, sheet_consistency='unknown')
    if selected != requested:
        raise ValueError('Requested and selected sheet IDs conflict')
    if not known_codes:
        return dict(source_sheet_id=source_id, effective_sheet_id=effective_id, sheet_consistency='unknown')
    return dict(source_sheet_id=source_id, effective_sheet_id=effective_id, sheet_consistency='consistent_unverified')


def _check_data(data, tracing_no):
    if (not isinstance(data, dict) or data.get('version') != 'codal-financial-cells-v1'
            or not isinstance(data.get('report'), dict)):
        raise ValueError('Financial projection has no report identity')
    if _trace(data['report'].get('tracing_no')) != tracing_no:
        raise ValueError('Financial projection tracing number differs from expected number')
    if type(data['report'].get('type')) is not int or data['report']['type'] != 6:
        raise ValueError('Only a type 6 financial projection may be archived')
    for flag in UNVERIFIED:
        if flag in data and data[flag] is not False:
            raise ValueError('Financial projection cannot promote unverified data')
    if any(data.get(flag) is not False for flag in DATA_UNVERIFIED):
        raise ValueError('Financial projection does not preserve all evidence boundaries')
    tables = data.get('tables')
    if not isinstance(tables, list) or type(data.get('table_count')) is not int or data['table_count'] != len(tables):
        raise ValueError('Financial table count differs')
    if any(not isinstance(table, dict) or not isinstance(table.get('cells'), list) for table in tables):
        raise ValueError('Financial table cells are invalid')
    count = sum(len(table['cells']) for table in tables)
    if type(data.get('cell_count')) is not int or data['cell_count'] != count:
        raise ValueError('Financial cell count differs')
    mismatches = 0
    table_keys = set()
    for table in tables:
        sheet_code, table_code = table.get('sheet_code'), table.get('table_code')
        if ((sheet_code is not None and (type(sheet_code) is not int or not 0 <= sheet_code <= 10**9))
                or type(table_code) is not int or not 0 <= table_code <= 10**9):
            raise ValueError('Financial table identity is invalid')
        # Missing sheet identity remains explicitly unknown, as in sheet_consistency.
        if sheet_code is not None:
            table_key = (sheet_code, table_code)
            if table_key in table_keys:
                raise ValueError('Duplicate financial table identity')
            table_keys.add(table_key)
        addresses, coordinates = set(), set()
        for cell in table['cells']:
            if not isinstance(cell, dict):
                raise ValueError('Financial cell projection is invalid')
            _check_cell_value(cell)
            address, row, column = cell.get('address'), cell.get('row'), cell.get('column')
            if (type(address) is not str or not re.fullmatch(r'[A-Z]{1,4}[1-9][0-9]{0,6}', address)
                    or type(row) is not int or not 1 <= row <= 10**9
                    or type(column) is not int or not 1 <= column <= 10**9
                    or type(cell.get('address_matches_sequence')) is not bool):
                raise ValueError('Financial cell coordinates are invalid')
            if address in addresses or (row, column) in coordinates:
                raise ValueError('Duplicate financial cell coordinates')
            addresses.add(address)
            coordinates.add((row, column))
            matches = _address_matches_sequence(address, row, column)
            if cell['address_matches_sequence'] is not matches:
                raise ValueError('Financial cell coordinate comparison differs')
            mismatches += not matches
    if (type(data.get('address_sequence_mismatch_count')) is not int
            or data['address_sequence_mismatch_count'] != mismatches):
        raise ValueError('Financial cell coordinate mismatch count differs')


def _check_sheets(sheets):
    if (not isinstance(sheets, dict) or sheets.get('version') != 'codal-financial-sheets-v1'
            or sheets.get('status') not in ('observed', 'not_present')
            or not isinstance(sheets.get('sheets'), list)):
        raise ValueError('Financial sheet projection is invalid')
    if ((sheets['status'] == 'not_present') != (len(sheets['sheets']) == 0)
            or any(not isinstance(item, dict) or type(item.get('sheet_id')) is not int
                   or abs(item['sheet_id']) > 9_999_999_999 or type(item.get('selected')) is not bool
                   or not (item.get('title') is None or isinstance(item['title'], str))
                   for item in sheets['sheets'])):
        raise ValueError('Financial sheet entries are invalid')
    ids = [item['sheet_id'] for item in sheets['sheets']]
    if len(ids) != len(set(ids)):
        raise ValueError('Financial sheet ids are duplicated')
    marked_id = sheets.get('selected_sheet_id')
    if marked_id is not None and (type(marked_id) is not int or abs(marked_id) > 9_999_999_999):
        raise ValueError('Financial selected sheet id is invalid')
    selected = [sheet.get('sheet_id') for sheet in sheets['sheets']
                if isinstance(sheet, dict) and sheet.get('selected') is True]
    if (len(selected) > 1 or (selected[0] if selected else None) != sheets.get('selected_sheet_id')
            or sheets.get('selection_status') != ('selected' if selected else 'unmarked')):
        raise ValueError('Financial sheet selection is ambiguous')


def load_codal_financial_archive(folder):
    """Recheck content and evidence limits, not historical financial correctness."""
    folder = Path(folder).resolve()
    manifest = _read_json(folder / 'manifest.json')
    if manifest.get('version') != ARCHIVE_VERSION or manifest.get('snapshot_id') != folder.name:
        raise ValueError('Financial archive identity mismatch')
    tracing_no = _trace(manifest.get('expected_tracing_no'))
    public_document_url(manifest['source_url'])
    public_document_url(manifest['effective_url'])
    if (_stamp(manifest['observed_at']) < _stamp(manifest['requested_at'])
            or _stamp(manifest['available_at']) < max(_stamp(manifest['observed_at']),
                                                       _stamp(manifest['archived_at']))):
        raise ValueError('Archive availability cannot be backdated')
    if any(manifest.get(flag) is not False for flag in UNVERIFIED):
        raise ValueError('Financial archive cannot promote unverified data')
    if (not isinstance(manifest.get('response_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', manifest['response_sha256'])
            or type(manifest.get('response_bytes')) is not int or manifest['response_bytes'] <= 0):
        raise ValueError('Original response evidence is invalid')
    expected = {'financial.json', 'sheets.json', 'report.txt'} | {'code/' + name for name in SOURCE_FILES}
    hashes = manifest.get('files_sha256')
    if not isinstance(hashes, dict) or set(hashes) != expected:
        raise ValueError('Financial archive file list differs')
    for name, digest in hashes.items():
        path = (folder / name).resolve()
        if not path.is_relative_to(folder) or not path.is_file() or _sha(path.read_bytes()) != digest:
            raise ValueError('Financial archive file digest differs')
    data, sheets = _read_json(folder / 'financial.json'), _read_json(folder / 'sheets.json')
    _check_data(data, tracing_no)
    _check_sheets(sheets)
    evidence = _sheet_evidence(manifest['source_url'], manifest['effective_url'], data, sheets)
    if any(manifest.get(key) != value or type(manifest.get(key)) is not type(value)
           for key, value in evidence.items()):
        raise ValueError('Financial sheet evidence differs from archived projections')
    return {'manifest': manifest, 'data': data, 'sheets': sheets}


def capture_financial_statement(url, expected_tracing_no, *, provider=None, root=None):
    """Capture one public response; failed, challenged or mismatched reports are not published."""
    from data_collector.codal import CodalProvider

    source = public_document_url(url)
    _requested_sheet_id(source)  # Reject ambiguous or malformed requests before any network activity.
    tracing_no = _trace(expected_tracing_no)
    sources = {name: (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
               for name in SOURCE_FILES}
    result = (CodalProvider() if provider is None else provider).get_document(source, financial_statement=True)
    if result['status'] != 'captured':
        return result
    if (result.get('format') != 'text' or not isinstance(result.get('content'), bytes)
            or result.get('source_url') != source or not isinstance(result.get('financial_sheets'), dict)):
        raise ValueError('Financial document response is not consistent')
    public_document_url(result.get('effective_url'))
    data = result.get('financial_data')
    _check_data(data, tracing_no)
    _check_sheets(result['financial_sheets'])
    sheet_evidence = _sheet_evidence(source, result['effective_url'], data, result['financial_sheets'])
    if (_stamp(result['observed_at']) < _stamp(result['requested_at'])
            or not isinstance(result.get('response_sha256'), str)
            or not re.fullmatch('[0-9a-f]{64}', result['response_sha256'])
            or type(result.get('response_bytes')) is not int or result['response_bytes'] <= 0):
        raise ValueError('Financial response evidence is invalid')
    for name, content in sources.items():
        if (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n') != content:
            raise ValueError('Financial capture source changed during collection')
    files = {'report.txt': result['content'], 'financial.json': _json_bytes(data),
             'sheets.json': _json_bytes(result['financial_sheets'])}
    files.update({'code/' + name: body for name, body in sources.items()})
    snapshot_id = 'CODALFIN-' + uuid.uuid4().hex
    archive_root = Path(root or settings.DATA_DIR / 'private' / 'codal-financial-cells').resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    folder = archive_root / snapshot_id
    stage = archive_root / ('.staging-' + snapshot_id)
    stage.mkdir(exist_ok=False)
    archived = now_tehran_iso()
    manifest = dict(version=ARCHIVE_VERSION, snapshot_id=snapshot_id,
                    expected_tracing_no=tracing_no, project_version=settings.VERSION,
                    source_url=source, effective_url=result['effective_url'],
                    requested_at=result['requested_at'], observed_at=result['observed_at'],
                    archived_at=archived,
                    available_at=max(_stamp(archived), _stamp(result['observed_at'])).isoformat(),
                    response_sha256=result['response_sha256'], response_bytes=result['response_bytes'],
                    files_sha256={name: _sha(body) for name, body in files.items()},
                    **sheet_evidence,
                    **dict.fromkeys(UNVERIFIED, False),
                    interpretation='Observed public projection only. Units, dates, identity and financial '
                                   'correctness are unverified. No formula execution, feature or signal. '
                                   'Local hashes are not independent timestamp attestation.')
    for name, body in files.items():
        path = stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    (stage / 'manifest.json').write_bytes(_json_bytes(manifest))
    # Publish a single directory rename only after every file has been written.
    os.replace(stage, folder)
    load_codal_financial_archive(folder)
    return {'status': 'captured', 'folder': str(folder), **manifest}
