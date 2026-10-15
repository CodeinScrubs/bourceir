"""نمونه نقض ادعاهای قطعی پیوست؛ فقط محاسبه و دیتابیس موقت آزمون."""

import sqlite3

import pytest

from config import settings
from core.market_rules import breakeven_move


def test_wal_allows_normal_reader_but_does_not_make_two_writers_concurrent(db):
    # این آزمون، سنجش بار ۶۰۰ نماد نیست؛ صرفاً ادعای «قفل هرگز رخ نمی‌دهد» را رد می‌کند.
    writer = sqlite3.connect(db.db_path, timeout=.01)
    reader = sqlite3.connect(db.db_path.resolve().as_uri() + '?mode=ro', uri=True, timeout=.01)
    try:
        db.conn.execute('BEGIN IMMEDIATE')
        assert reader.execute('SELECT COUNT(*) FROM symbols').fetchone()[0] >= 0
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            writer.execute('BEGIN IMMEDIATE')
    finally:
        db.conn.rollback()
        writer.close()
        reader.close()


def test_one_percent_band_alone_cannot_prove_negative_expected_return():
    # نسبت بالقوه کف تا سقف، امید ریاضی یا امکان اجرای آن معامله را ثابت نمی‌کند.
    band = .01
    low_to_high = (1 + band) / (1 - band) - 1
    assert low_to_high > breakeven_move()
    possible_net = ((1 + band) * (1 - settings.FEES['sell'])) / ((1 - band) * (1 + settings.FEES['buy'])) - 1
    assert possible_net > 0
