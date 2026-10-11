# -*- coding: utf-8 -*-
"""
آزمون‌های کدال و لایه هوش مصنوعی (با Jev ساختگیِ آزمونی؛ بدون مصرف API)
"""

from ai.codal_judge import QUESTIONS_VERSION, build_questions, judge_pending_announcements
from data_collector.codal import CodalProvider
from tests.conftest import FakeSession

RAW_LETTER = {
    "TracingNo": 1605314, "Symbol": "فولاد", "CompanyName": "فولاد مبارکه اصفهان",
    "Title": "گزارش فعالیت ماهانه دوره ۱ ماهه منتهی به  ۱۴۰۵/۰۶/۳۱", "LetterCode": "ن-۳۰",
    "SentDateTime": "۱۴۰۵/۰۷/۰۷ ۱۸:۵۵:۳۴", "PublishDateTime": "۱۴۰۵/۰۷/۰۷ ۱۸:۵۵:۳۴",
    "Url": "/Reports/Decision.aspx?LetterSerial=abc",
}


def test_codal_letter_normalization():
    letter = CodalProvider.normalize_letter(RAW_LETTER)
    assert letter["tracing_no"] == 1605314
    assert letter["letter_code"] == "ن-30"
    assert letter["published_at"] == "2026-09-29T18:55:34+03:30"
    assert letter["url"].startswith("https://codal.ir/Reports/")
    assert "  " not in letter["title"]


def test_codal_provider_parses_search_response():
    provider = CodalProvider(session=FakeSession({"search.codal.ir": {"Letters": [RAW_LETTER, {"bad": 1}]}}))
    letters = provider.get_letters(page=1)
    assert len(letters) == 1  # اطلاعیه خراب نادیده گرفته می‌شود


class FakeJev:
    model = "jev-test"
    available = True

    def __init__(self):
        self.calls = 0

    def ask(self, state, questions):
        self.calls += 1
        assert state["announcement"]["title"]
        return {
            "model": self.model, "input_tokens": 600,
            "answers": {
                "category": {"type": "choice", "choice": "monthly_activity", "confidence": 0.98},
                "impact": {"type": "score", "score": 2.4, "confidence": 0.6},
                "is_material": {"type": "noul", "noul": 0.7},
            },
        }


def test_questions_are_well_formed():
    questions = build_questions()
    assert questions["category"]["type"] == "choice" and "other" in questions["category"]["criteria"]
    assert len(questions["impact"]["criteria"]) == 5
    assert questions["is_material"]["type"] == "noul"


def test_judge_pending_announcements_stores_once(db):
    db.upsert_symbol({"symbol_fa": "فولاد", "ins_code": "1"})
    assert db.upsert_announcements([CodalProvider.normalize_letter(RAW_LETTER)]) == 1
    assert db.upsert_announcements([CodalProvider.normalize_letter(RAW_LETTER)]) == 0  # تکراری

    jev = FakeJev()
    result = judge_pending_announcements(db, jev=jev)
    assert result["judged"] == 1 and jev.calls == 1
    row = db.conn.execute("SELECT category, impact_score, is_material, questions_version FROM announcement_judgments;").fetchone()
    assert tuple(row) == ("monthly_activity", 2.4, 0.7, QUESTIONS_VERSION)
    assert judge_pending_announcements(db, jev=jev)["judged"] == 0  # دوباره پول خرج نمی‌شود


def test_judging_is_skipped_without_api_key(db):
    class NoKey(FakeJev):
        available = False

    assert judge_pending_announcements(db, jev=NoKey())["status"] == "skipped"
