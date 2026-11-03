"""Versioned monthly sales ledger with original coordinates and explicit unknowns.

Unlike the legacy v1 decoder, this contract supports the observed body starting
at row 6. It neither fills undeclared rows nor rewrites any legacy archive.
"""

import hashlib
import json
import re
from decimal import Decimal, DecimalException, Inexact, Rounded, Underflow, localcontext

from core.codal_operating import FALSE_FLAGS, _operating_row
from core.codal_revenue import (
    _COLS,
    _SECTIONS,
    _address,
    _constant,
    _decimal,
    _header,
    _number,
    _overlaps,
    _text,
    _time,
    _unique,
)
from core.codal_tables import TABLE_VERSION
from core.text import normalize_symbol
from core.timeutils import jalali_to_iso

VERSION = 'codal-monthly-sales-v2'


def _optional_text(cell):
    # Missing quantity/rate evidence must not erase independently reconciled
    # sales or become zero. Its primitive status is checked separately below.
    return _text(cell) if cell.get('value_present') is True and isinstance(cell.get('raw_value'), str) else None


def _geometry(data):
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
    if len(tables) != 1 or normalize_symbol(tables[0].get('title') or '') != 'تولید و فروش':
        raise ValueError('exactly one supported production table1197 required')
    cells, coordinates = {}, set()
    if not isinstance(tables[0]['cells'], list) or len(tables[0]['cells']) > 100000:
        raise ValueError('bounded table cells required')
    for cell in tables[0]['cells']:
        if any(type(cell.get(k)) is not int or cell[k] < 1 for k in ('row', 'column', 'row_span', 'column_span')):
            raise ValueError('invalid cell coordinates')
        address, key = cell.get('address'), (cell['row'], cell['column'])
        if address != _address(*key) or address in cells or key in coordinates:
            raise ValueError('duplicate or inconsistent cell coordinates')
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
    body = sorted({c['row'] for c in cells.values() if c.get('group') == 'Body'})
    if (not body or body[0] not in (3, 6) or body[-1] > 10005
            or body != list(range(body[0], body[-1] + 1))):
        raise ValueError('supported contiguous body rows required')
    for cell in cells.values():
        if cell['row'] not in (1, 2) and not body[0] <= cell['row'] <= body[-1]:
            raise ValueError('undeclared leading rows cannot hide declared cells')
        if (cell.get('group') == 'Header' and cell['address'] not in allowed_headers
                and any(cell['column'] <= col < cell['column'] + cell['column_span'] for col in _COLS)):
            raise ValueError('additional overlapping ledger header')
        for address in allowed_headers:
            header = cells[address]
            if cell['address'] != address and _overlaps(cell, first_row=header['row'],
                    last_row=header['row'] + header['row_span'] - 1, first_column=header['column'],
                    last_column=header['column'] + header['column_span'] - 1):
                raise ValueError('additional cell overlaps ledger header')
        overlaps = any(_overlaps(cell, first_row=3, last_row=body[-1],
            first_column=col, last_column=col) for col in _COLS)
        if overlaps and not (cell.get('group') == 'Body' and cell['row'] >= body[0]
                and cell['column'] in _COLS and cell['row_span'] == cell['column_span'] == 1):
            raise ValueError('additional cell overlaps ledger body or undeclared leading rows')
    for row in body:
        for col in _COLS.values():
            cell = cells.get(f'{col}{row}')
            if (cell is None or cell.get('group') != 'Body' or cell.get('visible') is not True
                    or cell['row_span'] != 1 or cell['column_span'] != 1):
                raise ValueError('missing, hidden or merged ledger cell')
            if col in ('A', 'B') and cell.get('formula_present') is not False:
                raise ValueError('formula or unknown product identity')
            if col in ('N', 'O', 'P', 'Q') and cell.get('period_end_jalali_raw') != period:
                raise ValueError('body cell period differs from current month')
    return report, period_iso, cells, body


def _sales(data):
    report, period_iso, cells, body = _geometry(data)
    rows, sections, placeholders, totals = [], [], [], []
    cursor, active, running, seen = 0, None, Decimal(0), set()
    grand, discount_seen = None, False
    for row in body:
        product, unit = _text(cells[f'A{row}']), _text(cells[f'B{row}'])
        amount_cell = cells[f'Q{row}']
        amount_text = _text(amount_cell)
        if cursor < len(_SECTIONS) and product == _SECTIONS[cursor][1]:
            if active is not None or unit or amount_text or amount_cell.get('formula_present') is not False:
                raise ValueError('invalid section heading')
            active, running, seen = _SECTIONS[cursor][0], Decimal(0), set()
            continue
        if active is not None and product == _SECTIONS[cursor][2]:
            cached = _number(amount_cell, primitive=False)
            if unit or cached != running:
                raise ValueError('section revenue disagrees with cached total')
            sections.append(dict(section=active, amount_rial=str(running * 1000000),
                cached_total_address=amount_cell['address'], cached_raw_value=amount_text))
            totals.append(running)
            cursor, active = cursor + 1, None
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
            if cached != sum(totals, Decimal(0)) or row != body[-1]:
                raise ValueError('grand revenue disagrees with cached total')
            grand = dict(amount_rial=str(cached * 1000000), cached_total_address=amount_cell['address'],
                cached_raw_value=amount_text)
            continue
        if active is None:
            raise ValueError('unexpected row outside revenue sections')
        if not product:
            if unit:
                raise ValueError('unnamed product has a unit')
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
            quantity_raw_value=_optional_text(cells[f'O{row}']), quantity_address=f'O{row}',
            rate_raw_value=_optional_text(cells[f'P{row}']), rate_address=f'P{row}', quantity_and_rate_verified=False))
    if active is not None or cursor != len(_SECTIONS) or grand is None:
        raise ValueError('incomplete monthly revenue ledger')
    return dict(tracing_no=report['tracing_no'], period_end_jalali=report['period_end_jalali'],
        period_end_iso=period_iso, table_code=1197, body_first_row=body[0], body_last_row=body[-1],
        undeclared_leading_rows=list(range(3, body[0])), rows=[_operating_row(r, cells) for r in rows],
        sections=sections, total=grand, structural_placeholders=placeholders,
        source_coordinates_preserved=True, formulas_executed=False)


def extract_monthly_sales(source_bytes, *, expected_sha256, available_at, observed_at, archived_at):
    """Reconcile exact signed sales and derive per-product rates; preserve nulls.

    Missing native quantities stay unknown even when the rendered UI shows zero.
    Receipt availability is never replaced with the claimed publication date.
    """
    result = dict(version=VERSION, status='invalid', source_sha256=None, available_at=None,
        rows=None, sections=None, total=None, reasons=[], source_bytes_verified=False,
        cached_totals_reconciled=False, **dict.fromkeys(FALSE_FLAGS, False))
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
            ledger = _sales(data)
        if observed.date().isoformat() < ledger['period_end_iso']:
            raise ValueError('observation cannot predate report period end')
        result.update(ledger, status='reconciled', available_at=available.isoformat(),
            observed_at=observed.isoformat(), archived_at=archived.isoformat(), cached_totals_reconciled=True)
    except (ValueError, TypeError, KeyError, AttributeError, UnicodeError, OverflowError,
            RecursionError, DecimalException) as error:
        result['reasons'].append(str(error))
    return result
