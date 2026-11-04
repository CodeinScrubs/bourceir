"""Market updates must not silently relabel historical rows as another instrument."""

from data_collector.scheduler import DataCollector
from data_collector.universe import UniverseManager


def test_conflicting_instrument_code_keeps_original_symbol_and_prices(db):
    sid = db.upsert_symbol(dict(symbol_fa='نمون', ins_code='101', tse_code='IRO1TEST0001', instrument_type_code=300))
    assert db.insert_daily_price(sid, '2024-01-01', 100, 100, 100, 100, volume=10)
    before = db.get_symbol('نمون')
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code='202', tse_code='IRO1TEST0001',
                                 instrument_type_code=303)) is None
    assert db.get_symbol('نمون') == before
    assert db.conn.execute('SELECT symbol_id FROM daily_prices').fetchone()[0] == sid


def test_conflicting_isin_keeps_original_even_with_same_instrument_code(db):
    db.upsert_symbol(dict(symbol_fa='نمون', ins_code='101', tse_code='IRO1TEST0001', instrument_type_code=300))
    before = db.get_symbol('نمون')
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code='101', tse_code='IRO1TEST0002',
                                 instrument_type_code=300)) is None
    assert db.get_symbol('نمون') == before


def test_legacy_placeholder_can_be_enriched_and_same_identity_updated(db):
    sid = db.insert_symbol(tse_code='نمون', symbol_fa='نمون')
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code='101', tse_code='IRO1TEST0001',
                                 instrument_type_code=300)) == sid
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code='101', name_fa='شرکت نمونه')) == sid
    saved = db.get_symbol('نمون')
    assert saved['ins_code'] == '101' and saved['tse_code'] == 'IRO1TEST0001'
    assert saved['name_fa'] == 'شرکت نمونه'


def test_universe_reports_conflict_without_relabeling_existing_instrument(db):
    db.upsert_symbol(dict(symbol_fa='نمون', ins_code='101', tse_code='IRO1TEST0001', instrument_type_code=300))

    class Provider:
        def get_stock_universe(self, known):
            return [dict(symbol_fa='نمون', ins_code='202', tse_code='IRO1TEST0001', instrument_type_code=300)]

    result = UniverseManager(db=db, provider=Provider()).sync_universe()
    assert result['failed'] == 1 and result['saved'] == 0
    assert db.get_symbol('نمون')['ins_code'] == '101'


def test_search_result_must_match_requested_symbol_and_survive_upsert(db):
    db.upsert_symbol(dict(symbol_fa='دیگر', ins_code='101', tse_code='IRO1TEST0001', instrument_type_code=300))

    class WrongSearch:
        def search_symbol(self, _):
            return dict(symbol_fa='دیگر', ins_code='101', tse_code='IRO1TEST0001', instrument_type_code=300)

    collector = DataCollector(db=db, provider=WrongSearch())
    assert collector._resolve('ناشناخته') is None


def test_search_does_not_return_an_existing_row_after_failed_upsert(db, monkeypatch):
    class ExactSearch:
        def search_symbol(self, _):
            return dict(symbol_fa='تازه', ins_code='101', tse_code='IRO1TEST0001', instrument_type_code=300)

    collector = DataCollector(db=db, provider=ExactSearch())
    monkeypatch.setattr(db, 'upsert_symbol', lambda _: None)
    assert collector._resolve('تازه') is None


def test_compatible_legacy_numeric_identity_and_conflicting_isin(db):
    sid = db.insert_symbol(tse_code='IRO1TEST0001', symbol_fa='نمون')
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code=101, tse_code='IRO1TEST0002',
                                 instrument_type_code=300)) is None
    assert db.get_symbol('نمون')['ins_code'] is None
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code=101, tse_code='IRO1TEST0001',
                                 instrument_type_code=300)) == sid
    assert db.upsert_symbol(dict(symbol_fa='نمون', ins_code=101, instrument_type_code=300)) == sid
    assert db.get_symbol('نمون')['ins_code'] == '101'
