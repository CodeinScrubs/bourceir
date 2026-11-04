# -*- coding: utf-8 -*-
"""
تامین‌کننده اطلاعیه‌های کدال (سامانه جامع اطلاع‌رسانی ناشران — codal.ir)

اطلاعیه‌ها (گزارش فعالیت ماهانه، صورت‌های مالی، مجامع، افزایش سرمایه، افشای اطلاعات بااهمیت)
مهم‌ترین «رویدادهای» کوتاه‌مدت بازار سهام ایران هستند. زمان انتشار هر اطلاعیه به وقت تهران ثبت
می‌شود تا در ساخت فیچر فقط اطلاعیه‌هایی استفاده شوند که قبل از لحظه تصمیم منتشر شده‌اند.
"""

import hashlib
import logging
import math
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from config import settings
from core.codal_documents import is_verification_page, pdf_has_valid_ending, public_document_url, visible_document
from core.codal_news import NewsDecodeError, native_evidence, normalize_letter, parse_news_body
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso

logger = logging.getLogger(__name__)

SEARCH_URL = "https://search.codal.ir/api/search/v2/q"
CODAL_BASE = "https://codal.ir"
MAX_RETRY_AFTER_SECONDS = 2**31-1
MAX_NEWS_WINDOW_NATIVE_BYTES = 2 * 1024 * 1024


def parse_retry_after(value, *, received_at, max_seconds=MAX_RETRY_AFTER_SECONDS):
    """Retain only bounded retry seconds; never archive the raw HTTP header."""
    if type(max_seconds) is not int or not 1 <= max_seconds <= MAX_RETRY_AFTER_SECONDS:
        raise ValueError('A positive supported retry metadata bound is required')
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.isascii() and text.isdigit():
        significant = text.lstrip('0') or '0'
        if len(significant) > 10:
            raise ValueError('Retry-After delay exceeds the supported range; no earlier retry may be inferred')
        seconds = int(significant)
    else:
        if len(text) > 128:
            return None
        try:
            target = parsedate_to_datetime(text)
            received = datetime.fromisoformat(received_at) if isinstance(received_at, str) else received_at
            if (not isinstance(received, datetime) or target.tzinfo is None or target.utcoffset() is None
                    or received.tzinfo is None or received.utcoffset() is None):
                return None
            seconds = max(0, math.ceil((target-received).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None
    if seconds > max_seconds:
        raise ValueError('Retry-After delay exceeds the supported range; no earlier retry may be inferred')
    return seconds


class CodalProvider:
    """دریافت فهرست اطلاعیه‌های کدال (فقط اطلاعات عمومی؛ بدون ورود به حساب کاربری)."""

    name = "codal"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = requests.Session() if session is None else session
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (BRS Quant Engine)", "Accept": "application/json"})
        self.timeout = settings.REQUEST_TIMEOUT_SECONDS

    @staticmethod
    def normalize_letter(letter: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """تبدیل یک اطلاعیه خام کدال به ساختار استاندارد BRS."""
        return normalize_letter(letter)

    @staticmethod
    def _document_link(value):
        if not value:
            return None
        try:
            return public_document_url(value)
        except ValueError:
            return None

    def get_document(self, url, *, max_bytes=20*1024*1024, structured_tables=False,
                     financial_statement=False):
        """یک سند عمومی با سقف حجم/زمان؛ کپچا شکست دریافت است و خودکار تکرار نمی‌شود.

        کوکی، سرآیند پاسخ و HTML دارای viewstate ذخیره نمی‌شوند. بدنه فقط در حافظه است.
        همه تغییرمسیرها پیش از درخواست بعدی کنترل می‌شوند؛ پیوندها خودکار دنبال نمی‌شوند.
        """
        source = public_document_url(url)
        if not isinstance(structured_tables, bool):
            raise ValueError('انتخاب دریافت جدول باید بولی باشد')
        if not isinstance(financial_statement, bool) or (financial_statement and structured_tables):
            raise ValueError('نوع استخراج باید صریح و یکتا باشد')
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= 20*1024*1024:
            raise ValueError('حد حجم سند باید عدد صحیح مثبت و حداکثر ۲۰ مگابایت باشد')
        result = dict(status='error', source_url=source, requested_at=now_tehran_iso())
        current, started = source, time.monotonic()
        try:
            for _ in range(4):
                remaining = 60 - (time.monotonic() - started)
                if remaining <= 0:
                    raise ValueError('زمان دریافت سند از حد مجاز گذشت')
                timeout = min(self.timeout, remaining)
                response = self.session.get(current, timeout=(timeout, timeout), stream=True,
                                            allow_redirects=False, headers={'Accept': 'text/html,application/pdf'})
                try:
                    result['http_status'] = response.status_code
                    if response.status_code in (301, 302, 303, 307, 308):
                        current = public_document_url(response.headers.get('Location'), base=current)
                        continue
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise ValueError('پاسخ جزئی یا ناموفق، سند کامل نیست')
                    length = response.headers.get('Content-Length')
                    declared_length = None
                    if length is not None:
                        if not isinstance(length, str) or not length.isascii() or not length.isdigit():
                            raise ValueError('طول اعلام‌شده سند معتبر نیست')
                        declared_length = int(length)
                        if declared_length > max_bytes:
                            raise ValueError('حجم اعلام‌شده سند از حد مجاز گذشت')
                    size = 0
                    chunks = []
                    for chunk in response.iter_content(chunk_size=65536):
                        size += len(chunk)
                        if size > max_bytes or time.monotonic()-started > 60:
                            raise ValueError('حجم یا زمان دریافت سند از حد مجاز گذشت')
                        chunks.append(chunk)
                    body = b''.join(chunks)
                    # requests decodes compressed bodies in iter_content, but the
                    # HTTP Content-Length describes the encoded bytes. Compare
                    # only when the two lengths are actually commensurate.
                    encoding = response.headers.get('Content-Encoding', '').strip().lower()
                    transfer = response.headers.get('Transfer-Encoding', '').strip().lower()
                    if (declared_length is not None and encoding in ('', 'identity')
                            and transfer in ('', 'identity') and size != declared_length):
                        return dict(result, error_reason='content_length_mismatch',
                                    message='طول بدنه با طول اعلام‌شده مطابقت ندارد؛ سند محفوظ نشد')
                    result.update(effective_url=current, observed_at=now_tehran_iso(),
                                  response_sha256=hashlib.sha256(body).hexdigest(), response_bytes=size)
                    if body.startswith(b'%PDF-'):
                        if structured_tables or financial_statement:
                            raise ValueError('دریافت جدول JSON از PDF پشتیبانی نمی‌شود')
                        if not pdf_has_valid_ending(body):
                            return dict(result, error_reason='invalid_pdf_ending',
                                        message='PDF پایان معتبر ندارد یا محتوای اضافی پس از پایان آن آمده است؛ محفوظ نشد')
                        return dict(result, status='captured', format='pdf', content=body, links=[])
                    # صفحات رسمی جاری UTF-8 هستند؛ خرابی رمزگذاری نباید متن تأییدشده بسازد.
                    html = body.decode('utf-8-sig')
                    if is_verification_page(html):
                        return dict(result, status='verification_required',
                                    message='کدال تأیید انسانی می‌خواهد؛ در مرورگر خودت تأیید کن')
                    if not any(tag in html.lower() for tag in ('<html', '<body', '<table')):
                        raise ValueError('پاسخ سند HTML/PDF معتبر نیست')
                    text, links = visible_document(html, current)
                    if not text.strip():
                        raise ValueError('متن قابل مشاهده‌ای در سند دریافت نشد')
                    output = dict(result, status='captured', format='text', content=text.encode('utf-8'), links=links)
                    if structured_tables:
                        from core.codal_tables import extract_monthly_tables
                        output['table_data'] = extract_monthly_tables(html)
                    if financial_statement:
                        from core.codal_financial_cells import extract_financial_cells
                        from core.codal_financial_sheets import discover_financial_sheets
                        output['financial_data'] = extract_financial_cells(html)
                        output['financial_sheets'] = discover_financial_sheets(html)
                    return output
                finally:
                    response.close()
            raise ValueError('تعداد تغییرمسیرهای سند بیش از حد مجاز است')
        except (requests.RequestException, ValueError, UnicodeError) as exc:
            logger.warning('دریافت سند عمومی کدال ناموفق بود: %s', type(exc).__name__)
            return dict(result, error_type=type(exc).__name__, message='دریافت سند معتبر کامل نشد؛ دوباره بررسی شود')

    def get_registration_universe_observation(self, *, start, end_exclusive, page=1, descriptive=False):
        """One public page; post-holdout dates require explicit descriptive opt-in."""
        from datetime import date, timedelta

        from core.codal_search import MAX_BYTES
        from core.codal_universe import registration_request

        if type(descriptive) is not bool:
            raise ValueError('descriptive opt-in must be explicit')
        if descriptive and end_exclusive > (date.fromisoformat(now_tehran_iso()[:10])+timedelta(days=1)).isoformat():
            raise ValueError('descriptive window cannot extend beyond today')
        url = registration_request(start=start, end_exclusive=end_exclusive,
                                   cutoff=end_exclusive if descriptive else settings.RESEARCH_CONFIG['holdout_start'],
                                   page=page)
        requested, started = now_tehran_iso(), time.monotonic()
        try:
            timeout = min(self.timeout, 10)
            response = self.session.get(url, timeout=(timeout, timeout), stream=True,
                                        allow_redirects=False, headers={'Accept': 'application/json'})
            try:
                if (response.status_code != 200 or response.url != url
                        or response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
                        != 'application/json'):
                    raise ValueError('complete unredirected JSON response required')
                length = response.headers.get('Content-Length', '')
                if length.isdigit() and int(length) > MAX_BYTES:
                    raise ValueError('declared response too large')
                chunks, size = [], 0
                for chunk in response.iter_content(chunk_size=65536):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic()-started > 30:
                        raise ValueError('response exceeds bounds')
                    chunks.append(chunk)
                body = b''.join(chunks)
                if not body:
                    raise ValueError('empty response')
                return dict(source='codal', url=url, requested_at=requested, received_at=now_tehran_iso(),
                            response_bytes=size, body_sha256=hashlib.sha256(body).hexdigest(), content=body)
            finally:
                response.close()
        except (requests.RequestException, ValueError) as error:
            logger.warning('public registration search failed: %s', type(error).__name__)
            return None

    def get_historical_search_observation(self, symbol_fa, *, start, end_exclusive, page=1, letter_type=None):
        """یک صفحه بایتی تاریخ‌دار؛ تاریخ/هویت هر ردیف باید پیش از مصرف کنترل شود."""
        from core.codal_search import MAX_BYTES, search_request

        request=search_request(symbol_fa,start=start,end_exclusive=end_exclusive,
            cutoff=settings.RESEARCH_CONFIG['holdout_start'],page=page,letter_type=letter_type)
        url=request['url'];requested=now_tehran_iso();started=time.monotonic()
        try:
            timeout=min(self.timeout,10)
            response=self.session.get(url,timeout=(timeout,timeout),stream=True,allow_redirects=False,
                headers={'Accept':'application/json'})
            try:
                if response.status_code!=200 or response.url!=url:
                    raise ValueError('پاسخ کامل و بدون تغییرمسیر لازم است')
                if response.headers.get('Content-Type','').split(';',1)[0].strip().lower()!='application/json':
                    raise ValueError('پاسخ JSON بومی لازم است')
                length=response.headers.get('Content-Length','')
                if length.isdigit() and int(length)>MAX_BYTES:
                    raise ValueError('حجم اعلام‌شده جست‌وجو از حد مجاز گذشت')
                chunks=[];size=0
                for chunk in response.iter_content(chunk_size=65536):
                    size+=len(chunk)
                    if size>MAX_BYTES or time.monotonic()-started>30:
                        raise ValueError('حجم یا زمان جست‌وجو از حد مجاز گذشت')
                    chunks.append(chunk)
                body=b''.join(chunks)
                if not body:raise ValueError('پاسخ خالی معتبر نیست')
                return dict(source='codal',url=url,requested_at=requested,received_at=now_tehran_iso(),
                    response_bytes=size,body_sha256=hashlib.sha256(body).hexdigest(),content=body)
            finally:
                response.close()
        except (requests.RequestException,ValueError) as error:
            logger.warning('جست‌وجوی تاریخ‌دار کدال ناموفق بود: %s',type(error).__name__)
            return None

    def get_letters(self, page: int = 1, symbol_fa: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
        """یک صفحه (۲۰ مورد) از جدیدترین اطلاعیه‌ها؛ با تعیین نماد فقط اطلاعیه‌های همان نماد."""
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise ValueError('شماره صفحه کدال باید عدد صحیح مثبت باشد')
        url = f"{SEARCH_URL}?&PageNumber={int(page)}"
        if symbol_fa:
            url += f"&Symbol={quote(normalize_symbol(symbol_fa))}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('Letters'), list):
                raise ValueError('ساختار پاسخ کدال معتبر نیست')
            letters = payload['Letters']
        except Exception as e:
            logger.error("خطا در دریافت اطلاعیه‌های کدال (صفحه %s): %s", page, e)
            return None
        normalized = [n for n in (self.normalize_letter(letter) for letter in letters) if n]
        return normalized if normalized or not letters else None

    def get_latest_report(self, pages: int = 3, symbol_fa: Optional[str] = None) -> Dict[str, Any]:
        """صفحه ناموفق با پایان فهرست یکی نیست؛ خروجی سقف‌دار ادعای پوشش کامل ندارد."""
        if isinstance(pages, bool) or not isinstance(pages, int) or not 1 <= pages <= 100:
            raise ValueError('تعداد صفحات کدال باید بین ۱ و ۱۰۰ باشد')
        collected, seen, fetched = [], set(), 0
        report = dict(letters=collected, pages_requested=pages, pages_fetched=0, failed_page=None,
                      reached_end=False, page_limit_reached=False, errors=[])
        for page in range(1, pages + 1):
            letters = self.get_letters(page=page, symbol_fa=symbol_fa)
            if letters is None:
                report['failed_page'] = page
                report['errors'].append(f'دریافت صفحه {page} کدال ناموفق بود؛ نبود اطلاعیه تأیید نشده است')
                break
            fetched += 1
            if not letters:
                report['reached_end'] = True
                break
            if symbol_fa and any(x['symbol_fa'] != normalize_symbol(symbol_fa) for x in letters):
                report['failed_page'] = page
                report['errors'].append('پاسخ کدال با نماد درخواستی تطبیق ندارد')
                break
            for letter in letters:
                if letter['tracing_no'] not in seen:
                    collected.append(letter)
                    seen.add(letter['tracing_no'])
            time.sleep(settings.RATE_LIMIT_DELAY)
        report['pages_fetched'] = fetched
        report['page_limit_reached'] = fetched == pages and not report['reached_end'] and report['failed_page'] is None
        return report

    def get_latest(self, pages: int = 3, symbol_fa: Optional[str] = None) -> List[Dict[str, Any]]:
        """رابط سازگار؛ گردآورنده از گزارش وضعیت صفحه‌ها استفاده می‌کند."""
        return self.get_latest_report(pages=pages, symbol_fa=symbol_fa)['letters']

    def get_news_page(self, page, *, timeout_seconds=None):
        """Bounded public metadata page with explicit partial-row accounting.

        The native Page field is a page-count claim, not the requested page ID.
        A verification response or malformed row is never a clean empty page.
        """
        if type(page) is not int or not 1 <= page <= 100:
            raise ValueError('A bounded positive page number is required')
        url = f'{SEARCH_URL}?&PageNumber={page}'
        result = dict(requested_page=page, requested_at=now_tehran_iso(), status='unavailable',
            url=url, http_status=None, failure_stage=None, failure_code=None,
            response_complete=False, response_bytes=None, response_sha256=None,
            response_prefix_bytes=None, response_prefix_sha256=None, rate_limit_retry_after_seconds=None,
            rate_limit_retry_after_unsupported=False,
            letters=[], raw_count=None, invalid_rows=0, native_evidence=native_evidence())
        response = None
        chunks, size, prefix_digest = [], 0, hashlib.sha256()
        stage, code = 'budget', 'request_budget_exhausted'
        started = time.monotonic()
        timeout = min(self.timeout, timeout_seconds) if timeout_seconds is not None else self.timeout
        try:
            if timeout <= 0:
                raise ValueError('News request budget exhausted')
            stage, code = 'request', 'request_failed'
            response = self.session.get(url, timeout=timeout,
                                        stream=True, allow_redirects=False)
            result['http_status'] = response.status_code if type(response.status_code) is int else None
            stage, code = 'http', 'http_status_not_200'
            if response.status_code in (429, 503):
                code = 'http_rate_limited' if response.status_code == 429 else 'http_service_unavailable'
                try:
                    result['rate_limit_retry_after_seconds'] = parse_retry_after(
                        getattr(response,'headers',{}).get('Retry-After'), received_at=now_tehran_iso())
                except ValueError:
                    result['rate_limit_retry_after_unsupported'] = True
            if response.status_code != 200:
                raise ValueError('A complete public page is required')
            stage, code = 'body', 'body_read_failed'
            for chunk in response.iter_content(chunk_size=65536):
                if not isinstance(chunk, bytes):
                    code = 'body_chunk_invalid'
                    raise ValueError('News response chunk must be bytes')
                kept = chunk[:max(0,1024*1024-size)]
                size += len(kept)
                chunks.append(kept)
                prefix_digest.update(kept)
                if len(kept) != len(chunk):
                    code = 'body_size_limit'
                    raise ValueError('News page exceeded its size budget')
                if time.monotonic()-started > timeout:
                    code = 'body_time_limit'
                    raise ValueError('News page exceeded its time budget')
            body = b''.join(chunks)
            result.update(response_complete=True,response_bytes=len(body),response_sha256=prefix_digest.hexdigest())
            parsed, approved = parse_news_body(body, page=page)
            result.update(parsed)
            result['native_evidence'] = native_evidence(body) if approved else native_evidence(
                reason='public_schema_unapproved')
        except (requests.RequestException, ValueError, UnicodeError) as error:
            if isinstance(error, NewsDecodeError):
                stage, code = error.stage, error.code
            result['error_type'] = type(error).__name__
            result.update(failure_stage=stage,failure_code=code)
            if stage == 'body' and not result['response_complete']:
                result.update(response_prefix_bytes=size,response_prefix_sha256=prefix_digest.hexdigest())
            logger.warning('News page %s unavailable (%s/%s): %s', page, stage, code, type(error).__name__)
        finally:
            if response is not None:
                response.close()
            result['received_at'] = now_tehran_iso()
        return result

    def get_news_window(self, *, since, pages=50, time_budget_seconds=120, progress=None):
        """Scan back to a retained time boundary, then recheck the mutable head."""
        from core.news_continuity import NewsWindow

        if type(pages) is not int or not 1 <= pages <= 100:
            raise ValueError('A bounded page budget is required')
        if type(time_budget_seconds) is not int or not 1 <= time_budget_seconds <= 300:
            raise ValueError('A bounded request time is required')
        scan = NewsWindow(since=since, requested_at=now_tehran_iso())
        deadline = time.monotonic()+time_budget_seconds
        stop, head, native_bytes = 'page_limit', None, 0
        for page in range(1, pages+1):
            if native_bytes >= MAX_NEWS_WINDOW_NATIVE_BYTES:
                stop = 'native_body_budget'; break
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                stop = 'time_budget'; break
            observation = self.get_news_page(page, timeout_seconds=remaining)
            scan.add(observation)
            if observation.get('native_evidence', {}).get('status') == 'captured':
                native_bytes += observation['response_bytes']
            if progress:
                progress(page)
            # A feed ordering anomaly invalidates timestamp-based completion, but
            # must not repeatedly strand collection at the same page. Retain the
            # observed order and continue within the original request bounds.
            if any(error != 'publication_order_changed' for error in scan.errors):
                stop = 'source_error'; break
            if scan.reached_end or (scan.boundary_reached and not scan.errors):
                stop = 'end' if scan.reached_end else 'boundary'
                break
            time.sleep(min(settings.RATE_LIMIT_DELAY, max(0, deadline-time.monotonic())))
        if scan.boundary_reached and not scan.errors and native_bytes >= MAX_NEWS_WINDOW_NATIVE_BYTES:
            stop = 'native_body_budget'
        if (scan.boundary_reached and not scan.errors and deadline-time.monotonic() > 0
                and native_bytes < MAX_NEWS_WINDOW_NATIVE_BYTES):
            head = self.get_news_page(1, timeout_seconds=deadline-time.monotonic())
        return scan.result(head_recheck=head, stop_reason=stop)

    def test_connection(self) -> bool:
        letters = self.get_letters(page=1)
        return bool(letters)
