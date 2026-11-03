"""Bounded raw Codal financial-statement cells; no financial inference or executable JS.

This parser does not establish issuer, units, historical publication, completeness,
or point-in-time eligibility. Callers must bind it to an independently archived
source and never treat values as verified facts or trading features.
"""

import re
from decimal import Decimal

from core.codal_tables import _initializers, _Scripts
from core.timeutils import jalali_to_iso

VERSION = 'codal-financial-cells-v1'
MAX_HTML_BYTES = 20 * 1024 * 1024
MAX_SHEETS = 50
MAX_TABLES = 500
MAX_CELLS = 100000
MAX_STRING = 20000


def _text(value):
    if value is not None and (type(value) is not str or len(value) > MAX_STRING
                              or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        raise ValueError('متن بومی محدود و معتبر لازم است')
    return value


def _integer(value, *, minimum=0, maximum=10**9):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('شناسه یا مختصات صحیح در محدوده لازم است')
    return value


def _flag(value):
    if value is not None and type(value) is not bool:
        raise ValueError('وضعیت بومی باید بولی یا نامعلوم باشد')
    return value


def _scalar(value):
    """Represent decimal JSON tokens as decimal text with an explicit source type."""
    if value is None:
        return None, 'null'
    if type(value) is str:
        return _text(value), 'string'
    if type(value) is bool:
        return value, 'boolean'
    if type(value) is int and -(2**63) <= value < 2**63:
        return value, 'integer'
    if type(value) is Decimal and value.is_finite() and len(str(value)) <= 100:
        return str(value), 'decimal'
    raise ValueError('مقدار سلول بومی باید تک‌مقداری محدود باشد')


def _address_matches_sequence(address, row, column):
    """Compare conventional A1 notation, without assuming Codal requires it."""
    match = re.fullmatch(r'([A-Z]{1,4})([1-9][0-9]{0,6})', address)
    letters, row_text = match.groups()
    address_column = 0
    for letter in letters:
        address_column = address_column * 26 + ord(letter) - ord('A') + 1
    return int(row_text) == row and address_column == column


def extract_financial_cells(html):
    """Extract all cells from one displayed type-6 datasource, preserving blanks/zero.

    One response can expose one or more sheets, but no inference about sheets that
    were not present in that response is made. Never executes formulas or scripts.
    """
    if type(html) is not str:
        raise ValueError('صفحه باید متن باشد')
    try:
        if len(html.encode('utf-8')) > MAX_HTML_BYTES:
            raise ValueError('صفحه از حد حجم مجاز گذشت')
    except UnicodeError as error:
        raise ValueError('متن صفحه رمزگذاری معتبر ندارد') from error
    parser = _Scripts()
    parser.feed(html)
    parser.close()
    initializers = [data for script in parser.scripts for data in _initializers(script)]
    if len(initializers) != 1:
        raise ValueError('دقیقاً یک datasource مستقل لازم است')
    raw = initializers[0]
    if type(raw.get('type')) is not int or raw['type'] != 6:
        raise ValueError('فقط صورت مالی ساختاریافته نوع ۶ پشتیبانی می‌شود')
    tracing = _integer(raw.get('tracingNo'), minimum=1, maximum=2**63-1)
    period = _integer(raw.get('period'), minimum=1, maximum=12)
    for key in ('periodEndToDate', 'yearEndToDate'):
        text = raw.get(key)
        if type(text) is not str or not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', text):
            raise ValueError('تاریخ دوره شمسی معتبر لازم است')
        jalali_to_iso(text)
    consolidated, audited = _flag(raw.get('isConsolidated')), _flag(raw.get('isAudited'))
    sheets = raw.get('sheets')
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= MAX_SHEETS:
        raise ValueError('فهرست محدود برگه‌ها لازم است')
    tables, seen_sheets, seen_tables, total, mismatches = [], set(), set(), 0, 0
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ValueError('برگه باید شیء باشد')
        code = _integer(sheet.get('code'))
        if code in seen_sheets:
            raise ValueError('شناسه برگه تکراری است')
        seen_sheets.add(code)
        source_tables = sheet.get('tables')
        if not isinstance(source_tables, list) or not source_tables:
            raise ValueError('فهرست جدول‌های برگه لازم است')
        for table in source_tables:
            if not isinstance(table, dict):
                raise ValueError('جدول باید شیء باشد')
            table_code = _integer(table.get('code'))
            key = (code, table_code)
            if key in seen_tables:
                raise ValueError('شناسه جدول تکراری است')
            seen_tables.add(key)
            if len(seen_tables) > MAX_TABLES:
                raise ValueError('تعداد جدول از حد مجاز گذشت')
            native_cells = table.get('cells')
            if not isinstance(native_cells, list) or not native_cells:
                raise ValueError('سلول جدول موجود نیست')
            cells, addresses, coordinates = [], set(), set()
            for cell in native_cells:
                if not isinstance(cell, dict):
                    raise ValueError('سلول باید شیء باشد')
                address = cell.get('address')
                if type(address) is not str or not re.fullmatch(r'[A-Z]{1,4}[1-9][0-9]{0,6}', address):
                    raise ValueError('نشانی سلول معتبر نیست')
                row, column = _integer(cell.get('rowSequence'), minimum=1), _integer(cell.get('columnSequence'), minimum=1)
                if address in addresses or (row, column) in coordinates:
                    raise ValueError('نشانی یا مختصات سلول تکراری است')
                addresses.add(address)
                coordinates.add((row, column))
                present = 'value' in cell
                value, value_kind = _scalar(cell.get('value')) if present else (None, 'absent')
                formula = _text(cell.get('formula'))
                matches = _address_matches_sequence(address, row, column)
                mismatches += not matches
                cells.append(dict(address=address, row=row, column=column,
                    address_matches_sequence=matches,
                    row_span=_integer(cell.get('rowSpan'), minimum=1),
                    column_span=_integer(cell.get('colSpan'), minimum=1),
                    group=_text(cell.get('cellGroupName')), data_type=_text(cell.get('dataTypeName')),
                    value_type=_text(cell.get('valueTypeName')), visible=_flag(cell.get('isVisible')),
                    cell_is_audited=_flag(cell.get('isAudited')), value_present=present,
                    raw_value=value, raw_value_kind=value_kind, formula_present=bool(formula),
                    period_end_jalali_raw=_text(cell.get('periodEndToDate')),
                    year_end_jalali_raw=_text(cell.get('yearEndToDate'))))
                total += 1
                if total > MAX_CELLS:
                    raise ValueError('تعداد سلول از حد مجاز گذشت')
            tables.append(dict(sheet_code=code, sheet_title=_text(sheet.get('title_Fa')),
                table_code=table_code, title=_text(table.get('title_Fa')),
                description=_text(table.get('description')), cells=cells))
    return dict(version=VERSION, report=dict(tracing_no=tracing, type=6, period_months=period,
        title=_text(raw.get('title_Fa')), period_end_jalali=raw['periodEndToDate'],
        year_end_jalali=raw['yearEndToDate'], claimed_publish_time_jalali=_text(raw.get('publishDateTime')),
        is_consolidated=consolidated, is_audited=audited), tables=tables, table_count=len(tables), cell_count=total,
        address_sequence_mismatch_count=mismatches,
        numeric_facts_reviewed=False, units_and_period_columns_verified=False,
        document_identity_verified=False, publication_time_verified=False,
        formulas_executed=False, accepted_for_features=False, accepted_for_signals=False)
