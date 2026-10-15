"""سلول‌های اعلام‌شده JSON کدال؛ بدون اجرای JavaScript یا تفسیر واحد مالی."""

import json
import re
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from core.text import to_en_digits
from core.timeutils import jalali_to_iso

TABLE_VERSION = 'codal-monthly-cells-v1'
_ASSIGNMENT = re.compile(r'(?:var|let|const)\s+datasource\s*=\s*')


def numeric_candidate(value):
    """رشته Decimal، نه ریال؛ متن مبهم/خالی نامعلوم می‌ماند و صفر حفظ می‌شود."""
    if not isinstance(value, str):
        return None
    text = to_en_digits(value).strip().replace('٬', ',').replace('٫', '.')
    if text.startswith('(') and text.endswith(')'):
        text = '-' + text[1:-1]
    if not re.fullmatch(r'[+-]?(?:[0-9]+|[0-9]{1,3}(?:,[0-9]{3})+)(?:\.[0-9]+)?', text):
        return None
    try:
        return str(Decimal(text.replace(',', '')))
    except InvalidOperation:
        return None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.active, self.parts, self.scripts = False, [], []

    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active, self.parts = True, []

    def handle_data(self, text):
        if self.active:
            self.parts.append(text)

    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            self.scripts.append(''.join(self.parts))
            self.active = False


def _unique(pairs):
    output = {}
    for key, value in pairs:
        if key in output:
            raise ValueError('کلید تکراری در داده جدول کدال')
        output[key] = value
    return output


def _invalid_constant(value):
    raise ValueError('عدد غیرمتناهی در جدول کدال مجاز نیست')


def _initializers(script):
    """فقط اعلان خارج از رشته و comment؛ هیچ کد یا فرمولی اجرا نمی‌شود."""
    decoder = json.JSONDecoder(object_pairs_hook=_unique, parse_constant=_invalid_constant, parse_float=Decimal)
    i = 0
    while i < len(script):
        if script.startswith('//', i):
            end = script.find('\n', i + 2)
            i = len(script) if end < 0 else end + 1
        elif script.startswith('/*', i):
            end = script.find('*/', i + 2)
            if end < 0:
                raise ValueError('comment ناتمام در متن جدول')
            i = end + 2
        elif script[i] in '\'"`':
            quote, i = script[i], i + 1
            while i < len(script) and script[i] != quote:
                i += 2 if script[i] == '\\' else 1
            i += 1
        else:
            match = _ASSIGNMENT.match(script, i) if (
                i == 0 or not (script[i-1].isalnum() or script[i-1] in '_$')) else None
            # قرارداد محدود: اعلان باید ابتدای یک سطر مستقل باشد؛ متن داخل regex/عبارت اعلان نیست.
            if match and script[script.rfind('\n', 0, i) + 1:i].strip():
                raise ValueError('اعلان datasource خارج از ابتدای سطر مستقل پشتیبانی نمی‌شود')
            if match:
                start = match.end()
                try:
                    data, length = decoder.raw_decode(script[start:])
                except (ValueError, RecursionError) as exc:
                    raise ValueError('datasource باید JSON مستقل و معتبر باشد') from exc
                end = start + length
                if not isinstance(data, dict) or script[end:].lstrip()[:1] not in ('', ';'):
                    raise ValueError('عبارت اجرایی به جای JSON مستقل پذیرفته نمی‌شود')
                yield data
                i = end
            else:
                i += 1


def _integer(value, *, minimum=1):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError('شناسه/مختصات جدول باید عدد صحیح معتبر باشد')
    return value


def _text(value):
    if value is not None and (not isinstance(value, str) or len(value) > 20000):
        raise ValueError('مقدار متنی سلول معتبر نیست')
    return value


def extract_monthly_tables(html):
    if not isinstance(html, str) or len(html.encode('utf-8')) > 20 * 1024 * 1024:
        raise ValueError('صفحه جدول باید متن محدود باشد')
    parser = _Scripts()
    parser.feed(html)
    parser.close()
    candidates = [data for script in parser.scripts for data in _initializers(script)]
    if len(candidates) != 1:
        raise ValueError('دقیقاً یک datasource مستقل لازم است')
    raw = candidates[0]
    if type(raw.get('type')) is not int or raw['type'] != 58 or type(raw.get('period')) is not int or raw['period'] != 1:
        raise ValueError('فقط گزارش تولیدی نوع ۵۸ با دوره یک‌ماهه پشتیبانی می‌شود')
    tracing = _integer(raw.get('tracingNo'))
    for key in ('periodEndToDate', 'yearEndToDate'):
        if not isinstance(raw.get(key), str) or not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', raw[key]):
            raise ValueError('تاریخ دوره گزارش باید شمسی معتبر باشد')
        jalali_to_iso(raw[key])
    sheets = raw.get('sheets')
    if not isinstance(sheets, list) or not 1 <= len(sheets) <= 50:
        raise ValueError('برگه گزارش معتبر نیست')
    tables, table_keys, total = [], set(), 0
    for sheet in sheets:
        if not isinstance(sheet, dict):
            raise ValueError('برگه باید شیء JSON باشد')
        sheet_code = _integer(sheet.get('code'))
        if not isinstance(sheet.get('tables'), list):
            raise ValueError('فهرست جدول‌ها موجود نیست')
        for table in sheet['tables']:
            if not isinstance(table, dict):
                raise ValueError('جدول باید شیء JSON باشد')
            table_code = _integer(table.get('code'))
            key = (sheet_code, table_code)
            if key in table_keys:
                raise ValueError('شناسه جدول تکراری است')
            table_keys.add(key)
            if not isinstance(table.get('cells'), list) or not table['cells']:
                raise ValueError('جدول بدون سلول پذیرفته نمی‌شود')
            cells, addresses = [], set()
            for cell in table['cells']:
                if not isinstance(cell, dict):
                    raise ValueError('سلول باید شیء JSON باشد')
                address = cell.get('address')
                if not isinstance(address, str) or not re.fullmatch(r'[A-Z]{1,4}[1-9][0-9]{0,6}', address) or address in addresses:
                    raise ValueError('نشانی سلول نامعتبر یا تکراری است')
                addresses.add(address)
                visible = cell.get('isVisible')
                if visible is not None and not isinstance(visible, bool):
                    raise ValueError('وضعیت نمایش سلول نامعتبر است')
                value, dtype = _text(cell.get('value')), _text(cell.get('dataTypeName'))
                cells.append(dict(address=address, row=_integer(cell.get('rowSequence')),
                                  column=_integer(cell.get('columnSequence')),
                                  row_span=_integer(cell.get('rowSpan')), column_span=_integer(cell.get('colSpan')),
                                  group=_text(cell.get('cellGroupName')), data_type=dtype,
                                  value_type=_text(cell.get('valueTypeName')),
                                  period_end_jalali_raw=_text(cell.get('periodEndToDate')),
                                  year_end_jalali_raw=_text(cell.get('yearEndToDate')),
                                  formula_present=bool(_text(cell.get('formula'))),
                                  visible=visible, value_present='value' in cell, raw_value=value,
                                  numeric_candidate=numeric_candidate(value) if visible is True and dtype in ('Integer', 'Float') else None))
                total += 1
                if total > 100000:
                    raise ValueError('تعداد سلول‌ها از حد مجاز گذشت')
            tables.append(dict(sheet_code=sheet_code, sheet_title=_text(sheet.get('title_Fa')),
                               table_code=table_code, title=_text(table.get('title_Fa')), cells=cells))
    if not tables:
        raise ValueError('هیچ جدول ساختاریافته‌ای موجود نیست')
    for flag in ('isConsolidated', 'isAudited'):
        if raw.get(flag) is not None and not isinstance(raw[flag], bool):
            raise ValueError('وضعیت حسابرسی/تلفیق گزارش معتبر نیست')
    return dict(version=TABLE_VERSION, report=dict(tracing_no=tracing, type=58, period_months=1,
                title=_text(raw.get('title_Fa')), period_end_jalali=raw['periodEndToDate'],
                year_end_jalali=raw['yearEndToDate'], claimed_publish_time_jalali=_text(raw.get('publishDateTime')),
                is_consolidated=raw.get('isConsolidated'), is_audited=raw.get('isAudited')),
                tables=tables, table_count=len(tables), cell_count=total,
                numeric_candidate_count=sum(c['numeric_candidate'] is not None for t in tables for c in t['cells']),
                numeric_facts_reviewed=False, units_and_period_columns_verified=False,
                document_identity_verified=False, publication_time_verified=False,
                formulas_executed=False, accepted_for_features=False, accepted_for_signals=False)
