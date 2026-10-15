# -*- coding: utf-8 -*-
"""
تامین‌کننده اطلاعیه‌های کدال (سامانه جامع اطلاع‌رسانی ناشران — codal.ir)

اطلاعیه‌ها (گزارش فعالیت ماهانه، صورت‌های مالی، مجامع، افزایش سرمایه، افشای اطلاعات بااهمیت)
مهم‌ترین «رویدادهای» کوتاه‌مدت بازار سهام ایران هستند. زمان انتشار هر اطلاعیه به وقت تهران ثبت
می‌شود تا در ساخت فیچر فقط اطلاعیه‌هایی استفاده شوند که قبل از لحظه تصمیم منتشر شده‌اند.
"""

import logging
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from config import settings
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import jalali_to_iso

logger = logging.getLogger(__name__)

SEARCH_URL = "https://search.codal.ir/api/search/v2/q"
CODAL_BASE = "https://codal.ir"


class CodalProvider:
    """دریافت فهرست اطلاعیه‌های کدال (فقط اطلاعات عمومی؛ بدون ورود به حساب کاربری)."""

    name = "codal"

    def __init__(self, session: Optional[requests.Session] = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (BRS Quant Engine)", "Accept": "application/json"})
        self.timeout = settings.REQUEST_TIMEOUT_SECONDS

    @staticmethod
    def normalize_letter(letter: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """تبدیل یک اطلاعیه خام کدال به ساختار استاندارد BRS."""
        try:
            return {
                "tracing_no": int(letter["TracingNo"]),
                "symbol_fa": normalize_symbol(letter.get("Symbol") or ""),
                "company_name": normalize_fa(letter.get("CompanyName") or ""),
                "title": normalize_fa(letter.get("Title") or ""),
                "letter_code": to_en_digits(letter.get("LetterCode") or "").strip() or None,
                "sent_at": jalali_to_iso(letter["SentDateTime"]) if letter.get("SentDateTime") else None,
                "published_at": jalali_to_iso(letter["PublishDateTime"]) if letter.get("PublishDateTime") else None,
                "url": CODAL_BASE + letter["Url"] if letter.get("Url") else None,
            }
        except Exception as e:
            logger.warning("اطلاعیه کدال نامعتبر نادیده گرفته شد: %s", e)
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

    def test_connection(self) -> bool:
        letters = self.get_letters(page=1)
        return bool(letters)
