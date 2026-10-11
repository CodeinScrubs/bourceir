# -*- coding: utf-8 -*-
"""
اتصال به مدل Jev (سرویس TypeSafe System One)

Jev متن نمی‌نویسد؛ به سؤال‌های ساختاریافته درباره یک «state» پاسخ نوع‌دار و احتمالاتی می‌دهد:
- choice: انتخاب یک گزینه از یک مجموعه (با احتمال هر گزینه)
- score : درجه‌بندی روی سطوح مرتب
- noul  : احتمال «بله» بودن یک شرط
مستندات: https://docs.typesafe.ai  — کلید API در فایل .env با نام TYPESAFE_API_KEY
"""

import logging
from typing import Any, Dict, Optional

from config import settings

logger = logging.getLogger(__name__)


class JevUnavailableError(RuntimeError):
    """کلید API تنظیم نشده یا SDK نصب نیست."""


class JevClient:
    """پوشش نازک روی SDK رسمی TypeSafe با تنظیمات پروژه."""

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = (api_key or settings.TYPESAFE_API_KEY or "").strip()
        self.model = model or settings.JEV_MODEL
        self._client = None

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get_client(self):
        if not self.available:
            raise JevUnavailableError("کلید TYPESAFE_API_KEY در فایل .env تنظیم نشده است.")
        if self._client is None:
            try:
                from typesafe_sdk import TypeSafeClient
            except ImportError as e:
                raise JevUnavailableError("پکیج typesafe-sdk نصب نیست (SETUP.bat را اجرا کنید).") from e
            self._client = TypeSafeClient(
                api_key=self.api_key, model=self.model, timeout=settings.JEV_TIMEOUT_SECONDS,
            )
        return self._client

    def ask(self, state: Any, questions: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """
        ارسال یک state و چند سؤال مستقل در یک درخواست (سؤال‌ها موازی پاسخ داده می‌شوند).
        خروجی: {'model', 'answers': {key: dict}, 'input_tokens'}
        """
        response = self._get_client().system_one(state=state, questions=questions)
        answers = {key: answer.model_dump() if hasattr(answer, "model_dump") else dict(answer)
                   for key, answer in response.answers.items()}
        return {"model": response.model, "answers": answers, "input_tokens": response.usage.input_tokens}

    def ping(self) -> Optional[str]:
        """آزمایش سبک اتصال؛ خروجی: نسخه مدل در صورت موفقیت، در غیر این صورت None."""
        try:
            result = self.ask(
                state={"text": "بازار امروز مثبت بود."},
                questions={"is_positive": {"type": "noul", "instructions": "آیا این جمله مثبت است؟"}},
            )
            return result["model"]
        except Exception as e:
            logger.error("اتصال به Jev ناموفق بود: %s", e)
            return None

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
