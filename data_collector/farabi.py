"""Read local Farabi exports. No credentials, network requests or order APIs."""

import io
import json
import re
import zipfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET

from core.text import normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, jalali_to_iso, now_tehran

PORTFOLIO_HEADERS = ['نماد', 'تعداد', 'خالص میانگین', 'جمع خرید', 'سر به سر', 'قیمت لحظه‌ای', 'ارزش', 'سود/زیان']
TURNOVER_HEADERS = ['تاریخ و زمان', 'نماد', 'تعداد', 'قیمت', 'بدهکار', 'بستانکار', 'مانده', 'توضیحات']
FILLS_HEADERS = ['تاریخ و زمان', 'سمت معامله', 'نماد', 'تعداد معامله شده', 'قیمت معامله شده', 'کارمزد معامله']
NS = {'x': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_XML_BYTES = 64 * 1024 * 1024
PARSER_VERSION = 'farabi-v3'


def _display_text(value):
    text = to_en_digits(str(value)).replace('٬', ',').replace('٫', '.').replace('٪', '%').replace('−', '-')
    return re.sub(r'[\u200e\u200f\u202a-\u202e\u2066-\u2069]', '', text).strip()


def _performance(value):
    text = _display_text(value)
    percent = r'([+-]?(?:\d+(?:\.\d+)?|∞)|-)'
    amount = r'([+-]?[\d,]+)'
    match = re.fullmatch(r'\(' + percent + r'%\)\s*' + amount, text)
    if match:
        return match[1], match[2]
    match = re.fullmatch(amount + r'\s*\(' + percent + r'%\)', text)
    if match:
        return match[2], match[1]
    raise ValueError('قالب قیمت یا سود/زیان سبد تغییر کرده است')


def import_report(repository, data: bytes, filename: str, captured_at=None):
    """Validate source bytes at the collection boundary, then persist through a repository."""
    parsed = parse_portfolio(data) if filename.lower().endswith('.json') else parse_export(data, captured_at)
    return repository.store_report(data, filename, parsed, parser_version=PARSER_VERSION)


def number(value, *, integer=False, optional=False):
    text = _display_text(value)
    if optional and text in ('', '-', 'None', '∞', '+∞', '-∞'):
        return None
    if ',' in text and not re.fullmatch(r'[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?', text):
        raise ValueError('جداکننده عدد در گزارش فارابی معتبر نیست')
    text = text.replace(',', '')
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError('عدد نامعتبر در گزارش فارابی') from exc
    if not parsed.is_finite() or (integer and parsed != parsed.to_integral_value()):
        raise ValueError('عدد غیرمتناهی یا تعداد غیرصحیح در گزارش')
    if abs(parsed) > 9_000_000_000_000_000:
        raise ValueError('مبلغ خارج از محدوده قابل محاسبه')
    return int(parsed) if integer or parsed == parsed.to_integral_value() else float(parsed)


def _timestamp(value):
    try:
        timestamp = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError('زمان برداشت نامعتبر است') from exc
    if timestamp.tzinfo is None or timestamp > now_tehran():
        raise ValueError('زمان برداشت باید با منطقه زمانی و حداکثر زمان کنونی باشد')
    return timestamp.astimezone(TEHRAN_TZ).isoformat()


def parse_portfolio(data: bytes):
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('فایل بیش از حد بزرگ است')
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError('اسنپ‌شات باید شیء JSON با قالب فارابی باشد')
    if payload.get('format') != 'farabi-dom-v1' or payload.get('kind') != 'portfolio' or payload.get('currency') != 'IRR':
        raise ValueError('قالب اسنپ‌شات یا واحد ریال تأیید نشد')
    if not isinstance(payload.get('source_url'), str):
        raise ValueError('آدرس منبع اسنپ‌شات نامعتبر است')
    source = urlsplit(payload['source_url'])
    if source.scheme != 'https' or source.netloc != 'farabixo.irfarabi.com' or source.username or source.password:
        raise ValueError('منبع اسنپ‌شات فارابی نیست')
    if payload.get('headers') != PORTFOLIO_HEADERS:
        raise ValueError('ستون‌های سبد تغییر کرده‌اند؛ برداشت متوقف شد')
    captured_at = _timestamp(payload.get('captured_at'))
    if not isinstance(payload.get('rows'), list) or not isinstance(payload.get('summary'), dict):
        raise ValueError('ساختار ردیف‌ها یا خلاصه اسنپ‌شات نامعتبر است')
    rows, seen = [], set()
    for values in payload.get('rows', []):
        if not isinstance(values, list) or len(values) != 8 or not all(isinstance(v, str) for v in values):
            raise ValueError('تعداد ستون‌های ردیف سبد نامعتبر است')
        symbol = normalize_symbol(values[0])
        if not symbol or symbol in seen:
            raise ValueError('نماد خالی یا تکراری در برداشت سبد')
        seen.add(symbol)
        pnl_pct, pnl_amount = _performance(values[7])
        _, quote_amount = _performance(values[5])
        row = dict(symbol_fa=symbol, quantity=number(values[1], integer=True),
                   broker_net_average_rial=number(values[2]), buy_total_rial=number(values[3], integer=True),
                   breakeven_rial=number(values[4]), last_price_rial=number(quote_amount, integer=True),
                   value_rial=number(values[6], integer=True), broker_pnl_rial=number(pnl_amount, integer=True),
                   broker_return_pct=number(pnl_pct, optional=True))
        if row['quantity'] <= 0 or min(row['buy_total_rial'], row['value_rial'], row['last_price_rial']) < 0:
            raise ValueError('موجودی یا ارزش دارایی نامعتبر است')
        rows.append(row)
    expected = number(payload.get('expected_positions'), integer=True)
    if expected <= 0 or len(rows) != expected:
        raise ValueError('برداشت سبد ناقص است؛ تعداد دارایی با شمار پنل برابر نیست')
    source_summary = payload.get('summary', {})
    keys = ['net_assets_rial', 'portfolio_value_rial', 'cash_rial', 'buying_power_rial', 'blocked_rial',
            'buy_total_rial', 'broker_pnl_rial']
    summary = {key: number(source_summary.get(key), integer=True) for key in keys}
    if min(summary[k] for k in ('net_assets_rial', 'portfolio_value_rial', 'buying_power_rial', 'blocked_rial')) < 0:
        raise ValueError('ارزش منفی در خلاصه حساب')
    summary['broker_return_pct'] = number(source_summary.get('broker_return_pct'), optional=True)
    # Each displayed row is rounded to whole rial independently of the total.
    differences = {key: sum(r[field] for r in rows) - summary[key] for key, field in (
        ('portfolio_value_rial', 'value_rial'), ('buy_total_rial', 'buy_total_rial'), ('broker_pnl_rial', 'broker_pnl_rial'))}
    if any(abs(diff) > expected for diff in differences.values()):
        raise ValueError('جمع ردیف‌های سبد با خلاصه فارابی سازگار نیست')
    if abs(summary['net_assets_rial'] - summary['portfolio_value_rial'] - summary['cash_rial']) > 1:
        raise ValueError('ارزش سبد و مانده با خالص دارایی برابر نیست')
    market_date = payload.get('market_date')
    if market_date is not None:
        try:
            parsed_date = datetime.strptime(market_date, '%Y-%m-%d').date()
        except (TypeError, ValueError) as exc:
            raise ValueError('تاریخ جلسه بازار نامعتبر است') from exc
        if parsed_date > datetime.fromisoformat(captured_at).date():
            raise ValueError('تاریخ بازار از زمان برداشت جلوتر است')
    return dict(kind='portfolio', captured_at=captured_at, rows=rows, summary=summary,
                market_date=market_date, expected_positions=expected, rounding_differences_rial=differences,
                rounding_tolerance_rial=expected, actual_history_complete=False)


def _xml(raw):
    if len(raw) > MAX_XML_BYTES or re.search(br'<!\s*(DOCTYPE|ENTITY)', raw, re.I):
        raise ValueError('XML نامعتبر یا بیش از حد بزرگ در فایل')
    return ET.fromstring(raw)


def xlsx_rows(data: bytes):
    """Restricted values-only reader for the observed Farabi XLSX layout, without extracting files."""
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('فایل بیش از حد بزرگ است')
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > 100 or len({x.filename for x in infos}) != len(infos):
                raise ValueError('ساختار ZIP نامعتبر است')
            if sum(x.file_size for x in infos) > MAX_XML_BYTES or any(x.flag_bits & 1 for x in infos):
                raise ValueError('فایل فشرده بیش از حد بزرگ یا رمزدار است')
            sheets = [x.filename for x in infos if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', x.filename)]
            if sheets != ['xl/worksheets/sheet1.xml']:
                raise ValueError('گزارش باید یک برگه با قالب رسمی فارابی داشته باشد')
            strings = []
            if 'xl/sharedStrings.xml' in archive.namelist():
                strings = [''.join(node.itertext()) for node in _xml(archive.read('xl/sharedStrings.xml')).findall('x:si', NS)]
            root = _xml(archive.read(sheets[0]))
            rows = []
            for element in root.findall('x:sheetData/x:row', NS):
                values = []
                for cell in element.findall('x:c', NS):
                    if cell.find('x:f', NS) is not None:
                        raise ValueError('فرمول در گزارش خام پذیرفته نیست')
                    reference = re.fullmatch(r'([A-Z]+)\d+', cell.get('r', ''))
                    if reference is None:
                        raise ValueError('آدرس سلول نامعتبر است')
                    col = 0
                    for char in reference[1]:
                        col = col * 26 + ord(char) - ord('A') + 1
                    if col > 32 or col <= len(values):
                        raise ValueError('ستون نامعتبر یا تکراری در فایل')
                    values.extend([''] * (col - len(values) - 1))
                    value = cell.find('x:v', NS)
                    text = value.text if value is not None and value.text is not None else ''
                    if cell.get('t') == 's' and text:
                        string_index = int(text)
                        if not 0 <= string_index < len(strings):
                            raise ValueError('ارجاع متن سلول نامعتبر است')
                        text = strings[string_index]
                    elif cell.get('t') == 'inlineStr':
                        inline = cell.find('x:is', NS)
                        text = ''.join(inline.itertext()) if inline is not None else ''
                    elif cell.get('t') in ('e', 'b', 'd'):
                        raise ValueError('نوع سلول در گزارش پشتیبانی نمی‌شود')
                    values.append(text)
                if any(values):
                    if rows and len(values) < len(rows[0]):
                        values.extend([''] * (len(rows[0]) - len(values)))
                    rows.append(values)
                if len(rows) > 100_000:
                    raise ValueError('تعداد ردیف بیش از حد مجاز است')
            return rows
    except (zipfile.BadZipFile, KeyError, ET.ParseError, IndexError) as exc:
        raise ValueError('فایل اکسل معتبر با قالب فارابی لازم است') from exc


def parse_export(data: bytes, captured_at=None):
    rows = xlsx_rows(data)
    if not rows:
        raise ValueError('گزارش خالی است')
    captured_at = _timestamp(captured_at or now_tehran().isoformat())
    records = []
    if rows[0] == FILLS_HEADERS:
        for row in rows[1:]:
            if len(row) != 6 or row[1] not in ('خرید', 'فروش') or ' ' not in row[0].strip():
                raise ValueError('ردیف معاملات خروجی سابقه سفارشات نامعتبر است')
            timestamp = jalali_to_iso(row[0])
            if datetime.fromisoformat(timestamp) > datetime.fromisoformat(captured_at):
                raise ValueError('زمان گزارش معامله در آینده است')
            record = dict(executed_at=timestamp, date=timestamp[:10], side='buy' if row[1] == 'خرید' else 'sell',
                          raw_symbol=normalize_symbol(row[2]) or None, quantity=number(row[3], integer=True),
                          price_rial=number(row[4]), fee_rial=number(row[5]), fee_source='broker_export')
            if min(record['quantity'], record['price_rial']) <= 0 or record['fee_rial'] < 0:
                raise ValueError('تعداد، قیمت یا کارمزد گزارش معامله نامعتبر است')
            records.append(record)
        return dict(kind='fills', captured_at=captured_at, rows=records, actual_history_complete=False)
    if rows[0] != TURNOVER_HEADERS:
        raise ValueError('ستون‌های خروجی فارابی شناسایی نشدند')
    balance = None
    opening_balance = None
    previous_date = None
    for ordinal, row in enumerate(rows[1:]):
        if len(row) != 8:
            raise ValueError('ردیف گردش حساب نامعتبر است')
        # The official export uses negative debits, unlike its positive UI debit column.
        debit, credit, reported = (number(row[i], integer=True) for i in (4, 5, 6))
        if debit > 0 or credit < 0 or (debit and credit):
            raise ValueError('علامت بدهکار/بستانکار با قالب خروجی رسمی سازگار نیست')
        if row[0] == '-':
            if ordinal != 0 or debit or credit or number(row[2]) or number(row[3]):
                raise ValueError('مانده ابتدای دوره نامعتبر است')
            balance = reported
            opening_balance = reported
            continue
        if balance is None:
            raise ValueError('مانده ابتدای دوره گزارش وجود ندارد')
        date = jalali_to_iso(row[0])[:10]
        if date > captured_at[:10] or (previous_date and date < previous_date):
            raise ValueError('ترتیب تاریخ گردش حساب نامعتبر است')
        if balance + debit + credit != reported:
            raise ValueError(f'مانده گردش حساب در ردیف {ordinal + 2} با مبالغ برابر نیست')
        balance, previous_date = reported, date
        description = normalize_symbol(row[7])
        if description.startswith('خرید '):
            category = 'buy'
        elif description.startswith('فروش '):
            category = 'sell'
        elif description.startswith(('دریافت واریز ', 'دریافت انتقالی ')):
            category = 'cash_in'
        elif description.startswith('پرداخت '):
            category = 'cash_out'
        else:
            category = 'unclassified'
        records.append(dict(date=date, raw_symbol=normalize_symbol(row[1]), quantity=number(row[2], integer=True),
                            price_rial=number(row[3]), debit_rial=debit, credit_rial=credit,
                            balance_rial=reported, category=category, description=description))
    if not records:
        raise ValueError('گزارش فاقد گردش حساب است')
    return dict(kind='turnover', captured_at=captured_at, rows=records, opening_balance_rial=opening_balance,
                ending_balance_rial=balance,
                date_start=records[0]['date'], date_end=records[-1]['date'],
                balance_chain_verified=True, actual_history_complete=False)
