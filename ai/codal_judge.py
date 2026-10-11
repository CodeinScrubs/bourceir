# -*- coding: utf-8 -*-
"""
قضاوت هوش مصنوعی روی اطلاعیه‌های کدال با Jev

برای هر اطلاعیه سه قضاوت مستقل در یک درخواست گرفته می‌شود:
- category   (choice): نوع اطلاعیه
- impact     (score) : اثر کوتاه‌مدت احتمالی بر قیمت از دید سهامدار (۰ = به‌شدت منفی ... ۴ = به‌شدت مثبت)
- is_material(noul)  : آیا اطلاعیه «بااهمیت و قیمت‌ساز» است؟

⚠️ این خروجی‌ها «سیگنال معاملاتی» نیستند. فقط فیچر خام هستند که باید با بکتست خارج از نمونه
(بازده واقعی روزهای بعد از انتشار) ارزیابی شوند (قانون ۲ و ۸ AGENTS.md).
هر تغییر در متن سؤال‌ها باید QUESTIONS_VERSION را افزایش دهد تا نتایج قابل مقایسه بمانند.
"""

import logging
from typing import Any, Dict, Optional

from ai.jev import JevClient, JevUnavailableError
from database.db import DatabaseManager

logger = logging.getLogger(__name__)

QUESTIONS_VERSION = "codal-title-v1"

CATEGORIES = {
    "monthly_activity": "گزارش فعالیت ماهانه / گزارش فروش و تولید ماهانه شرکت",
    "financial_statements": "صورت‌های مالی میان‌دوره‌ای یا سالانه، یا توضیحات درباره آن‌ها",
    "capital_increase": "افزایش سرمایه (پیشنهاد هیئت مدیره، گزارش توجیهی، مجوز، تصمیم مجمع یا ثبت)",
    "agm_dividend": "مجمع عمومی (آگهی دعوت، تصمیمات) و تقسیم سود نقدی",
    "trading_halt": "توقف یا بازگشایی نماد",
    "material_news": "افشای اطلاعات بااهمیت (قرارداد بزرگ، رویداد مهم، تغییر مدیریت کلیدی)",
    "clarification": "پاسخ به شایعه، ابهام یا درخواست توضیح از سوی بورس",
    "other": "سایر موارد (اصلاحیه، اطلاعیه اداری و ...)",
}

IMPACT_LEVELS = [
    "به‌شدت منفی: احتمالاً باعث افت محسوس قیمت یا صف فروش می‌شود",
    "منفی: احتمالاً اثر منفی ملایم بر قیمت دارد",
    "خنثی: اثر قابل توجهی بر قیمت ندارد یا از عنوان قابل تشخیص نیست",
    "مثبت: احتمالاً اثر مثبت ملایم بر قیمت دارد",
    "به‌شدت مثبت: احتمالاً باعث رشد محسوس قیمت یا صف خرید می‌شود",
]


def build_questions() -> Dict[str, Dict[str, Any]]:
    return {
        "category": {
            "type": "choice",
            "instructions": "این اطلاعیه منتشرشده در سامانه کدال (بازار سرمایه ایران) از چه نوعی است؟",
            "criteria": CATEGORIES,
        },
        "impact": {
            "type": "score",
            "instructions": (
                "از دید یک سهامدار عادی بورس تهران، اثر کوتاه‌مدت (۱ تا ۳ روز معاملاتی) این اطلاعیه "
                "بر قیمت سهم شرکت `announcement.symbol` چیست؟"
            ),
            "criteria": IMPACT_LEVELS,
        },
        "is_material": {
            "type": "noul",
            "instructions": "آیا این اطلاعیه احتمالاً اطلاعات بااهمیت و قیمت‌ساز برای سهام شرکت دارد؟",
        },
    }


def judge_announcement(jev: JevClient, announcement: Dict[str, Any]) -> Dict[str, Any]:
    """قضاوت روی یک اطلاعیه (فقط عنوان، نماد و نام شرکت؛ بدون متن کامل)."""
    state = {
        "announcement": {
            "symbol": announcement.get("symbol_fa"),
            "company": announcement.get("company_name"),
            "title": announcement.get("title"),
        }
    }
    result = jev.ask(state=state, questions=build_questions())
    answers = result["answers"]
    category, impact, material = answers.get("category", {}), answers.get("impact", {}), answers.get("is_material", {})
    return {
        "model": result["model"],
        "category": category.get("choice"),
        "category_confidence": category.get("confidence"),
        "impact_score": impact.get("score"),
        "impact_confidence": impact.get("confidence"),
        "is_material": material.get("noul"),
        "input_tokens": result["input_tokens"],
        "raw": answers,
    }


def judge_pending_announcements(db: DatabaseManager, limit: int = 50,
                                jev: Optional[JevClient] = None) -> Dict[str, Any]:
    """قضاوت روی اطلاعیه‌هایی که هنوز با این نسخه سؤال‌ها ارزیابی نشده‌اند."""
    jev = jev or JevClient()
    if not jev.available:
        return {"status": "skipped", "reason": "کلید Jev تنظیم نشده است", "judged": 0}
    pending = db.get_unjudged_announcements(jev.model, QUESTIONS_VERSION, limit=limit)
    judged = failed = tokens = 0
    for announcement in pending:
        try:
            judgment = judge_announcement(jev, announcement)
            db.insert_announcement_judgment(announcement["id"], jev.model, QUESTIONS_VERSION, judgment)
            judged += 1
            tokens += judgment["input_tokens"] or 0
        except JevUnavailableError as e:
            return {"status": "failed", "reason": str(e), "judged": judged}
        except Exception as e:
            logger.error("قضاوت اطلاعیه %s ناموفق بود: %s", announcement.get("tracing_no"), e)
            failed += 1
    return {"status": "success" if failed == 0 else "partial", "judged": judged, "failed": failed,
            "input_tokens": tokens}
