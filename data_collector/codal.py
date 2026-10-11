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
        url = f"{SEARCH_URL}?&PageNumber={int(page)}"
        if symbol_fa:
            url += f"&Symbol={quote(normalize_symbol(symbol_fa))}"
        try:
            response = self.session.get(url, timeout=self.timeout)
            response.raise_for_status()
            letters = response.json().get("Letters", [])
        except Exception as e:
            logger.error("خطا در دریافت اطلاعیه‌های کدال (صفحه %s): %s", page, e)
            return None
        return [n for n in (self.normalize_letter(letter) for letter in letters) if n]

    def get_latest(self, pages: int = 3, symbol_fa: Optional[str] = None) -> List[Dict[str, Any]]:
        """چند صفحه از جدیدترین اطلاعیه‌ها."""
        collected: List[Dict[str, Any]] = []
        for page in range(1, pages + 1):
            letters = self.get_letters(page=page, symbol_fa=symbol_fa)
            if not letters:
                break
            collected.extend(letters)
            time.sleep(settings.RATE_LIMIT_DELAY)
        return collected

    def test_connection(self) -> bool:
        letters = self.get_letters(page=1)
        return bool(letters)
