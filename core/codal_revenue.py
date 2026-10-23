"""Reconcile primitive monthly revenue in the narrow Codal production V4 table.

This is a descriptive source ledger, not a feature or a formula evaluator. Cached
totals are comparison targets; original quantities and rates remain unverified.
"""

import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal, DecimalException, Inexact, Rounded, Underflow, localcontext

from core.codal_tables import TABLE_VERSION, numeric_candidate
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, jalali_to_iso

VERSION = 'codal-primitive-revenue-v1'
_SECTIONS = (
    ('domestic', 'فروش داخلی:', 'جمع فروش داخلی'),
    ('export', 'فروش صادراتی:', 'جمع فروش صادراتی'),
    ('services', 'درآمد ارائه خدمات:', 'جمع درآمد ارائه خدمات'),
    ('returns', 'برگشت از فروش:', 'جمع برگشت از فروش'),
)
_COLS = {1: 'A', 2: 'B', 14: 'N', 15: 'O', 16: 'P', 17: 'Q'}


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _constant(_):
    raise ValueError('non-finite JSON number')


def _time(value):
    if not isinstance(value, str):
        raise ValueError('explicit timezone-aware availability required')
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError('invalid availability timestamp') from error
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('timezone-aware availability required')
    return stamp.astimezone(TEHRAN_TZ)


def _text(cell):
    value = cell.get('raw_value')
    if cell.get('value_present') is not True or not isinstance(value, str):
        raise ValueError('explicit cell value required')
    return normalize_symbol(value).strip()


def _decimal(raw):
    # Decimal may compress a huge fractional string to a short exponent, so a
    # post-parse string limit alone does not bound precision or prevent underflow.
    if len(raw) > 128:
        raise ValueError('unsupported revenue number length')
    parsed = numeric_candidate(raw)
    if parsed is None or len(parsed) > 40:
        raise ValueError('unknown or unsupported revenue number')
    value = Decimal(parsed)
    if not value.is_finite() or value.as_tuple().exponent < -40:
        raise ValueError('unsupported revenue precision')
    return value


def _number(cell, *, primitive):
    if primitive and (cell.get('formula_present') is not False
                      or cell.get('data_type') not in ('Integer', 'Float')):
        raise ValueError('primitive revenue must have a known numeric type and no formula')
    if not primitive and cell.get('formula_present') is not True:
        raise ValueError('this template requires explicit cached formula totals')
    raw = _text(cell)
    value = _decimal(raw)
    if primitive and cell['data_type'] == 'Integer' and value != value.to_integral_value():
        raise ValueError('fractional Integer revenue')
    return value


def _header(cells, address, value, *, row_span=1, column_span=1):
    cell = cells.get(address)
    if (cell is None or cell.get('group') != 'Header' or cell.get('visible') is not True
            or cell.get('formula_present') is not False or cell['row_span'] != row_span
            or cell['column_span'] != column_span or _text(cell) != value):
        raise ValueError(f'unsupported header {address}')


def _address(row, column):
    letters = ''
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return f'{letters}{row}'


def _overlaps(cell, *, first_row, last_row, first_column, last_column):
    return (cell['row'] <= last_row and cell['row'] + cell['row_span'] - 1 >= first_row
            and cell['column'] <= last_column
            and cell['column'] + cell['column_span'] - 1 >= first_column)


def _ledger(data):
    report = data['report']
    if (data.get('version') != TABLE_VERSION or type(report.get('type')) is not int
            or report['type'] != 58 or type(report.get('period_months')) is not int
            or report['period_months'] != 1 or report.get('is_consolidated') is not False
            or type(report.get('tracing_no')) is not int or report['tracing_no'] <= 0):
        raise ValueError('one-month standalone type58 report required')
    period = report['period_end_jalali']
    if not isinstance(period, str) or not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', period):
        raise ValueError('explicit report period required')
    period_iso = jalali_to_iso(period)[:10]
    tables = [t for t in data['tables'] if type(t.get('table_code')) is int and t['table_code'] == 1197]
    if len(tables) != 1:
        raise ValueError('exactly one production V4 table1197 required')
    table = tables[0]
    if normalize_symbol(table.get('title') or '') != 'تولید و فروش':
        raise ValueError('unexpected table1197 title')
    cells, coordinates = {}, set()
    for cell in table['cells']:
        if any(type(cell.get(k)) is not int or cell[k] < 1 for k in ('row', 'column', 'row_span', 'column_span')):
            raise ValueError('invalid cell coordinates')
        address = cell.get('address')
        key = cell['row'], cell['column']
        if not isinstance(address, str) or address in cells or key in coordinates:
            raise ValueError('duplicate or invalid cell coordinates')
        if address != _address(cell['row'], cell['column']):
            raise ValueError('cell address disagrees with coordinates')
        cells[address] = cell
        coordinates.add(key)
    _header(cells, 'A1', 'شرح', column_span=2)
    _header(cells, 'A2', 'نام محصول')
    _header(cells, 'B2', 'واحد')
    _header(cells, 'N1', f'دوره یک ماهه منتهی به {period}', column_span=4)
    for address, label in [('N2', 'تعداد تولید'), ('O2', 'تعداد فروش'),
                           ('P2', 'نرخ فروش (ریال)'), ('Q2', 'مبلغ فروش (میلیون ریال)')]:
        _header(cells, address, label)
    if any(cells[a].get('period_end_jalali_raw') != period for a in ('N1', 'N2', 'O2', 'P2', 'Q2')):
        raise ValueError('header period differs from current month')
    allowed_headers = {'A1', 'A2', 'B2', 'N1', 'N2', 'O2', 'P2', 'Q2'}
    for cell in cells.values():
        covered = range(cell['column'], cell['column'] + cell['column_span'])
        if (cell.get('group') == 'Header' and any(col in covered for col in _COLS)
                and cell['address'] not in allowed_headers):
            raise ValueError('additional overlapping ledger header')
    body_rows = sorted({c['row'] for c in cells.values() if c.get('group') == 'Body'})
    if (not body_rows or max(body_rows) > 10002
            or body_rows != list(range(3, max(body_rows) + 1))):
        raise ValueError('contiguous explicit body rows required')
    # Every declared rectangle can affect the consumed cells, including an
    # otherwise ignored cumulative column or a cell with an unsupported group.
    # Check only the bounded ledger regions, without expanding arbitrary spans.
    for cell in cells.values():
        for address in allowed_headers:
            header = cells[address]
            if (cell['address'] != address and _overlaps(cell, first_row=header['row'],
                    last_row=header['row'] + header['row_span'] - 1, first_column=header['column'],
                    last_column=header['column'] + header['column_span'] - 1)):
                raise ValueError('additional cell overlaps ledger header')
        overlaps_body = any(_overlaps(cell, first_row=3, last_row=max(body_rows),
                                      first_column=col, last_column=col) for col in _COLS)
        if overlaps_body and not (cell['group'] == 'Body' and cell['row'] >= 3
                                  and cell['column'] in _COLS
                                  and cell['row_span'] == 1 and cell['column_span'] == 1):
            raise ValueError('additional cell overlaps ledger body')
    for row in body_rows:
        for col in _COLS.values():
            cell = cells.get(f'{col}{row}')
            if (cell is None or cell.get('group') != 'Body' or cell.get('visible') is not True
                    or cell['row_span'] != 1 or cell['column_span'] != 1):
                raise ValueError('missing, hidden or merged ledger cell')
            if col in ('A', 'B') and cell.get('formula_present') is not False:
                raise ValueError('formula or unknown product identity')
        for col in ('N', 'O', 'P', 'Q'):
            if cells[f'{col}{row}'].get('period_end_jalali_raw') != period:
                raise ValueError('body cell period differs from current month')
    rows, sections, placeholders, totals = [], [], [], []
    cursor = 0
    active = None
    seen = set()
    running = Decimal(0)
    grand = None
    discount_seen = False
    for row in body_rows:
        product, unit = _text(cells[f'A{row}']), _text(cells[f'B{row}'])
        amount_cell = cells[f'Q{row}']
        amount_text = _text(amount_cell)
        if cursor < len(_SECTIONS) and product == _SECTIONS[cursor][1]:
            if active is not None or unit or amount_text or amount_cell.get('formula_present') is not False:
                raise ValueError('invalid section heading')
            active = _SECTIONS[cursor][0]
            running, seen = Decimal(0), set()
            continue
        if active is not None and product == _SECTIONS[cursor][2]:
            cached = _number(amount_cell, primitive=False)
            if unit or cached != running:
                raise ValueError('section revenue disagrees with cached total')
            sections.append(dict(section=active, amount_rial=str(running * 1000000),
                                 cached_total_address=amount_cell['address'], cached_raw_value=amount_text))
            totals.append(running)
            cursor += 1
            active = None
            continue
        if product == 'تخفیفات':
            if active is not None or cursor != len(_SECTIONS) or discount_seen or grand is not None or unit:
                raise ValueError('discount row outside its explicit position')
            amount = _number(amount_cell, primitive=True)
            if amount > 0:
                raise ValueError('discount must be signed nonpositive')
            discount_seen = True
            rows.append(dict(section='discounts', product=product, quantity_unit=None,
                             revenue_address=amount_cell['address'], raw_value=amount_text,
                             source_unit='million_rial', scale_to_rial=1000000, amount_rial=str(amount * 1000000)))
            totals.append(amount)
            continue
        if product == 'جمع':
            if active is not None or cursor != len(_SECTIONS) or not discount_seen or grand is not None or unit:
                raise ValueError('grand total outside its explicit position')
            cached = _number(amount_cell, primitive=False)
            if cached != sum(totals, Decimal(0)) or row != body_rows[-1]:
                raise ValueError('grand revenue disagrees with cached total')
            grand = dict(amount_rial=str(cached * 1000000), cached_total_address=amount_cell['address'],
                         cached_raw_value=amount_text)
            continue
        if active is None:
            raise ValueError('unexpected row outside revenue sections')
        if not product:
            if unit:
                raise ValueError('unnamed product has a unit')
            # Template padding is structural. A missing value is not promoted to zero.
            for col in ('N', 'O', 'P', 'Q'):
                text = _text(cells[f'{col}{row}'])
                if text and _decimal(text) != 0:
                    raise ValueError('unnamed row contains a nonzero or unknown value')
            if amount_cell.get('formula_present') is not False:
                raise ValueError('unnamed primitive has formula or unknown formula state')
            placeholders.append(dict(row=row, revenue_address=amount_cell['address'], raw_value=amount_text))
            continue
        if product in seen or not unit:
            raise ValueError('duplicate product within section or missing unit')
        seen.add(product)
        amount = _number(amount_cell, primitive=True)
        if (active == 'returns' and amount > 0) or (active != 'returns' and amount < 0):
            raise ValueError('revenue sign conflicts with section')
        running += amount
        rows.append(dict(section=active, product=product, quantity_unit=unit,
                         revenue_address=amount_cell['address'], raw_value=amount_text,
                         source_unit='million_rial', scale_to_rial=1000000, amount_rial=str(amount * 1000000),
                         quantity_raw_value=_text(cells[f'O{row}']), quantity_address=f'O{row}',
                         rate_raw_value=_text(cells[f'P{row}']), rate_address=f'P{row}',
                         quantity_and_rate_verified=False))
    if active is not None or cursor != len(_SECTIONS) or grand is None:
        raise ValueError('incomplete monthly revenue ledger')
    return dict(tracing_no=report['tracing_no'], period_end_jalali=period, period_end_iso=period_iso,
                table_code=1197, rows=rows, sections=sections, total=grand,
                structural_placeholders=placeholders)


def extract_monthly_revenue(source_bytes, *, expected_sha256, available_at, observed_at, archived_at):
    """Read exact preserved tables.json bytes; return no partial ledger on failure.

    Availability may not predate receipt or archive. No issuer identity, historical
    publication, formula correctness, feature eligibility or economic truth is
    certified by matching local source bytes and reconciling their arithmetic.
    """
    result = dict(version=VERSION, status='invalid', source_sha256=None, available_at=None,
                  rows=None, sections=None, total=None, reasons=[], source_bytes_verified=False,
                  cached_totals_reconciled=False, formulas_executed=False,
                  document_identity_verified=False, historical_publication_verified=False,
                  quantity_and_rate_verified=False, accepted_for_features=False,
                  accepted_for_signals=False, profitability_proven=False)
    try:
        if (not isinstance(source_bytes, bytes) or not 0 < len(source_bytes) <= 20 * 1024 * 1024
                or not isinstance(expected_sha256, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha256)
                or hashlib.sha256(source_bytes).hexdigest() != expected_sha256):
            raise ValueError('source bytes must match the expected SHA256')
        result.update(source_sha256=expected_sha256, source_bytes_verified=True)
        available, observed, archived = map(_time, (available_at, observed_at, archived_at))
        if available < max(observed, archived) or archived < observed:
            raise ValueError('availability cannot predate observation or archive')
        data = json.loads(source_bytes.decode('utf-8'), object_pairs_hook=_unique, parse_constant=_constant)
        with localcontext() as context:
            context.prec = 100
            for signal in (Inexact, Rounded, Underflow):
                context.traps[signal] = True
            ledger = _ledger(data)
        if observed.date().isoformat() < ledger['period_end_iso']:
            raise ValueError('observation cannot predate report period end')
        result.update(ledger, status='reconciled', available_at=available.isoformat(),
                      observed_at=observed.isoformat(), archived_at=archived.isoformat(),
                      cached_totals_reconciled=True)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError,
            RecursionError, DecimalException) as error:
        result['reasons'].append(str(error))
    return result
