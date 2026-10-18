"""سلول‌های اعلام‌شده JSON کدال؛ بدون اجرای JavaScript یا تفسیر واحد مالی."""

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser

from core.text import normalize_symbol, to_en_digits
from core.timeutils import jalali_to_iso

TABLE_VERSION = 'codal-monthly-cells-v1'
MONTHLY_VERSION = 'codal-monthly-v2'
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


def _aware_time(value):
    if not isinstance(value, str):
        raise ValueError('زمان دسترسی صریح و منطقه‌دار لازم است')
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError('زمان دسترسی باید منطقه‌دار باشد')
    return stamp


def _month_number(jalali):
    text = to_en_digits(jalali)
    if not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', text):
        raise ValueError('تاریخ شمسی صریح در عنوان دوره لازم است')
    jalali_to_iso(text)
    year, month, _ = map(int, text.split('/'))
    return year * 12 + month - 1


def normalize_monthly_tables(data, *, available_at, observed_at, archived_at, identity_verified=False):
    """زیرمجموعه محدود و سخت‌گیر جدول ماهانه؛ فقط عنوان‌های واقعاً نمایان.

    سطر جمع با عنوان صریح و سطرهای محصول باید دقیقاً آشتی کنند. فرمول، چند
    جدول مشابه، سربرگ متناقض یا واحد نرخ نامعلوم، کل خروجی عددی را متوقف می‌کند.
    این تابع هیچ پرچم/فایل cells-v1 را بازنویسی یا زمان انتشار ادعایی را مصرف نمی‌کند.
    """
    result = dict(version=MONTHLY_VERSION, verified=False, rows=None, periods=None,
                  units_and_period_columns_verified=False, accepted_for_features=False,
                  accepted_for_signals=False, available_at=available_at, reasons=[])
    try:
        available = _aware_time(available_at)
        if available < max(_aware_time(observed_at), _aware_time(archived_at)):
            raise ValueError('زمان دسترسی از مشاهده/آرشیو زودتر است')
        if type(identity_verified) is not bool or data.get('version') != TABLE_VERSION:
            raise ValueError('قرارداد سلول/هویت نامعتبر است')
        report = data['report']
        if report['type'] != 58 or report['period_months'] != 1 or type(report['tracing_no']) is not int:
            raise ValueError('گزارش یک‌ماهه نوع ۵۸ لازم است')
        current_month = _month_number(report['period_end_jalali'])
        if available < _aware_time(jalali_to_iso(report['period_end_jalali'])):
            raise ValueError('گزارش ماهانه پیش از پایان دوره قابل دسترس فرض شده است')
        if report.get('is_consolidated') is not False:
            raise ValueError('قرارداد محدود فقط صورت مستقل با وضعیت تلفیق معلوم را می‌پذیرد')
        # از جدول دیگر یا metadata واحد به سلول تزریق نمی‌شود.
        candidates = []
        for table in data['tables']:
            headers = [c for c in table['cells'] if c.get('visible') is True and c.get('group') == 'Header']
            if any('مبلغ فروش' in normalize_symbol(c.get('raw_value') or '') for c in headers):
                candidates.append((table, headers))
        if len(candidates) != 1:
            raise ValueError('دقیقاً یک جدول فروش با سربرگ نمایان پشتیبانی می‌شود')
        table, headers = candidates[0]
        visible = [c for c in table['cells'] if c.get('visible') is True]
        if any(c.get('formula_present') is not False for c in visible):
            raise ValueError('سلول نمایان دارای فرمول یا وضعیت فرمول نامعلوم است')
        max_header_row = max(c['row'] + c['row_span'] - 1 for c in headers)
        body = [c for c in visible if c.get('group') == 'Body' and c['row'] > max_header_row]
        if not body or any(c['row_span'] != 1 or c['column_span'] != 1 for c in body):
            raise ValueError('بدنه تک‌سلولی صریح لازم است')
        by_coordinate = {}
        for cell in body:
            key = (cell['row'], cell['column'])
            if key in by_coordinate:
                raise ValueError('مختصات بدنه تکراری است')
            by_coordinate[key] = cell
        columns, product_column, periods = {}, [], {}
        for column in sorted({c['column'] for c in body}):
            covering = [c for c in headers if c['column'] <= column < c['column'] + c['column_span']]
            # هم‌پوشانی در یک ردیف سربرگ تفسیر یکتا نمی‌سازد.
            occupied = set()
            for cell in covering:
                interval = set(range(cell['row'], cell['row'] + cell['row_span']))
                if occupied & interval:
                    raise ValueError('سربرگ‌های هم‌پوشان مبهم‌اند')
                occupied |= interval
            text = normalize_symbol(' '.join(c.get('raw_value') or '' for c in sorted(covering, key=lambda c: c['row'])))
            if text.strip() in ('نام محصول', 'شرح محصول', 'محصول'):
                product_column.append(column)
                continue
            revenue, rate = 'مبلغ فروش' in text, 'نرخ فروش' in text
            if not revenue and not rate:
                continue
            if revenue and rate:
                raise ValueError('مبلغ و نرخ در یک ستون مبهم‌اند')
            dates = set(re.findall(r'[0-9]{4}/[0-9]{2}/[0-9]{2}', to_en_digits(text)))
            if len(dates) != 1:
                raise ValueError('هر ستون فروش به دقیقاً یک تاریخ نمایان نیاز دارد')
            period = next(iter(dates))
            month = _month_number(period)
            if 'دوره یک ماهه منتهی به' in text:
                role = 'current'
                if period != report['period_end_jalali']:
                    raise ValueError('دوره جاری با گزارش یکسان نیست')
            elif 'ماه قبل' in text:
                role = 'previous'
                if month != current_month - 1:
                    raise ValueError('تاریخ ماه قبل ناسازگار است')
            elif 'دوره مشابه سال قبل' in text:
                role = 'year_ago'
                if month != current_month - 12:
                    raise ValueError('تاریخ دوره مشابه ناسازگار است')
            else:
                raise ValueError('نقش ماهانه ستون صریح نیست؛ ستون تجمیعی مصرف نمی‌شود')
            # عبارت کامل واحد، نه صرفاً وجود کلمه ریال داخل میلیون ریال.
            units = re.findall(r'\(([^()]*)\)', text)
            if len(units) != 1:
                raise ValueError('واحد صریح و یکتای ستون لازم است')
            unit = units[0].strip()
            if revenue:
                scale = {'ریال': 1, 'هزار ریال': 1000, 'میلیون ریال': 1000000}.get(unit)
                if scale is None:
                    raise ValueError('واحد مبلغ فروش پشتیبانی نمی‌شود')
                quantity_unit = None
            else:
                parts = unit.split('/')
                if len(parts) != 2 or not parts[1].strip():
                    raise ValueError('واحد نرخ باید پول بر واحد محصول باشد')
                scale = {'ریال': 1, 'هزار ریال': 1000, 'میلیون ریال': 1000000}.get(parts[0].strip())
                if scale is None:
                    raise ValueError('واحد پولی نرخ پشتیبانی نمی‌شود')
                quantity_unit = parts[1].strip()
            kind = 'revenue_rial' if revenue else 'rate_rial_per_unit'
            if (role, kind) in columns:
                raise ValueError('چند ستون مبلغ/نرخ برای یک دوره پشتیبانی نمی‌شود')
            columns[role, kind] = (column, scale, quantity_unit)
            periods[role] = period
        if len(product_column) != 1 or not {('current', 'revenue_rial'), ('previous', 'revenue_rial')} <= set(columns):
            raise ValueError('نام محصول و مبلغ جاری/ماه قبلِ یکتا لازم است')
        rows, total, seen = [], None, set()
        for row_number in sorted({c['row'] for c in body}):
            label = by_coordinate.get((row_number, product_column[0]), {}).get('raw_value')
            label = normalize_symbol(label or '').strip()
            if not label or label in seen:
                raise ValueError('نام محصول خالی یا تکراری است')
            seen.add(label)
            values = {}
            for (role, kind), (column, scale, quantity_unit) in columns.items():
                cell = by_coordinate.get((row_number, column), {})
                number = numeric_candidate(cell.get('raw_value'))
                if number is None or cell.get('data_type') not in ('Integer', 'Float'):
                    # نرخ سطر جمع تعریف ندارد؛ سایر خانه‌های لازم باید معلوم باشند.
                    if label in ('جمع', 'جمع کل') and kind == 'rate_rial_per_unit':
                        continue
                    raise ValueError('عدد فروش/نرخ خالی، متنی یا نامعتبر است')
                scaled = Decimal(number) * scale
                if scaled < 0:
                    raise ValueError('مبلغ/نرخ منفی در قرارداد محدود پشتیبانی نمی‌شود')
                values.setdefault(role, {})[kind] = str(scaled)
                if quantity_unit is not None:
                    values[role]['quantity_unit'] = quantity_unit
            entry = dict(product=label, cells=values)
            if label in ('جمع', 'جمع کل'):
                if total is not None:
                    raise ValueError('چند سطر جمع مبهم است')
                total = entry
            else:
                rows.append(entry)
        if not rows or total is None:
            raise ValueError('محصول و جمع صریح لازم است')
        for role in periods:
            if sum(Decimal(r['cells'][role]['revenue_rial']) for r in rows) != Decimal(total['cells'][role]['revenue_rial']):
                raise ValueError('جمع مبلغ محصولات با جمع گزارش ناسازگار است')
        result.update(verified=True, rows=rows, totals=total['cells'], periods=periods,
                      tracing_no=report['tracing_no'], period_end_jalali=report['period_end_jalali'],
                      units_and_period_columns_verified=True, document_identity_verified=identity_verified,
                      accepted_for_features=identity_verified,
                      limitation='Only supported visible monthly sales columns; not profit, cash flow or audited company identity.')
    except (ValueError, KeyError, TypeError, InvalidOperation) as error:
        result['reasons'].append(str(error))
    return result


def monthly_fundamental_features(archives, *, decision_at):
    """نسخه قابل دسترس تا زمان تصمیم؛ هیچ استفاده‌ای از publishDateTime یا تاریخ دوره برای دسترسی نیست.

    archives خروجی load_codal_tables است. پذیرش هویت باید در manifest گواهی شده
    باشد؛ آرشیو v1 کنونی عمداً هویت را تأیید نمی‌کند و فیچر مجاز نمی‌سازد.
    """
    cutoff = _aware_time(decision_at)
    output = dict(version=MONTHLY_VERSION, codal_rev_mom_ratio=None, codal_primary_rate_change=None,
                  available_at=None, sources=[], accepted_for_features=False, accepted_for_signals=False)
    selected = {}
    issuer = None
    for archive in archives:
        manifest, data = archive['manifest'], archive['data']
        if _aware_time(manifest['available_at']) > cutoff:
            continue
        if (manifest.get('document_identity_verified') is not True
                or not isinstance(manifest.get('issuer_id'), str) or not manifest['issuer_id'].strip()
                or not re.fullmatch('[a-f0-9]{64}', manifest.get('identity_review_sha256', ''))):
            continue
        if issuer is not None and manifest['issuer_id'] != issuer:
            raise ValueError('گزارش شرکت‌های متفاوت در یک فیچر ترکیب نمی‌شوند')
        issuer = manifest['issuer_id']
        month = _month_number(data['report']['period_end_jalali'])
        # اول نسخه قابل دسترس انتخاب می‌شود؛ رد اصلاحیه، نسخه منسوخ را معتبر نمی‌کند.
        if month not in selected or _aware_time(manifest['available_at']) > _aware_time(selected[month]['manifest']['available_at']):
            selected[month] = archive
        elif _aware_time(manifest['available_at']) == _aware_time(selected[month]['manifest']['available_at']):
            raise ValueError('دو نسخه با زمان دسترسی یکسان مبهم‌اند')
    if not selected:
        return output
    known = {}
    for month, archive in selected.items():
        manifest = archive['manifest']
        known[month] = normalize_monthly_tables(archive['data'], available_at=manifest['available_at'],
            observed_at=manifest['observed_at'], archived_at=manifest['archived_at'], identity_verified=True)
    month = max(selected)
    current = known[month]
    if not current['verified'] or not current['accepted_for_features']:
        return dict(output, available_at=selected[month]['manifest']['available_at'],
                    sources=[selected[month]['data']['report'].get('tracing_no')], blocking_reasons=current['reasons'])
    previous = Decimal(current['totals']['previous']['revenue_rial'])
    if previous > 0:
        output['codal_rev_mom_ratio'] = str(Decimal(current['totals']['current']['revenue_rial']) / previous)
    top = sorted(current['rows'], key=lambda r: (-Decimal(r['cells']['current']['revenue_rial']), r['product']))[0]
    tied = sum(Decimal(r['cells']['current']['revenue_rial']) == Decimal(top['cells']['current']['revenue_rial'])
               for r in current['rows']) > 1
    rates, units = [], []
    for lag in (1, 2, 3):
        candidates = [r for r in (known.get(month - lag, {}).get('rows') or []) if r['product'] == top['product']]
        cell = candidates[0]['cells']['current'] if len(candidates) == 1 else {}
        rates.append(cell.get('rate_rial_per_unit'))
        units.append(cell.get('quantity_unit'))
    today = top['cells']['current']
    if (not tied and Decimal(today['revenue_rial']) > 0 and today.get('rate_rial_per_unit') is not None and all(v is not None for v in rates)
            and all(u == today.get('quantity_unit') and u is not None for u in units)):
        average = sum(Decimal(v) for v in rates) / 3
        if average > 0:
            output['codal_primary_rate_change'] = str(Decimal(today['rate_rial_per_unit']) / average - 1)
    used = [selected[k] for k in sorted(selected) if month - 3 <= k <= month]
    output.update(available_at=max(used, key=lambda a: _aware_time(a['manifest']['available_at']))['manifest']['available_at'],
                  sources=[a['data']['report'].get('tracing_no') for a in used], primary_product=None if tied else top['product'],
                  accepted_for_features=True)
    return output
