"""Inconsistent displayed book levels must not become usable depth or queue data."""

from copy import deepcopy
from datetime import timedelta

import pytest

from core.paper import quote_fill
from data_collector.validator import DataValidator
from ops.intraday_audit import audit_intraday
from tests.conftest import FOOLAD_INS
from tests.test_capture_evidence import clock as clock
from tests.test_capture_evidence import finish, start_run
from tests.test_live_quote_contract import account, valid_book
from tests.test_scheduler import _collector
from tests.test_workbench import quote


@pytest.mark.parametrize('field,value', [
    ('bid_price_1', 969), ('bid_price_5', 969),
    ('ask_price_1', 1031), ('ask_price_4', 1031),
])
def test_validator_rejects_observed_book_prices_outside_both_known_bounds(field, value):
    _, book = valid_book(**{field: value})
    valid, reasons = DataValidator.validate_snapshot(book)
    assert not valid
    assert any(field in reason for reason in reasons)


def test_missing_bounds_do_not_create_a_fabricated_book_contradiction():
    _, book = valid_book(price_limit_down=None, bid_price_5=969)
    assert DataValidator.validate_snapshot(book)[0]


def test_old_stored_inconsistent_depth_blocks_plan_and_account_mark():
    at, book = valid_book(bid_price_4=969)
    calculation = quote(intraday=True, intraday_snapshot=book, at=at, stop_rial=980, target_rial=1020)
    assert calculation['quantity'] == 0 and calculation['blockers']
    result = account(book, at)
    assert not result['current'] and result['state']['equity_rial'] is None


def test_old_stored_inconsistent_depth_blocks_observed_paper_fill():
    at, book = valid_book(bid_price_4=969, low_price=970, high_price=1030)
    with pytest.raises(ValueError, match='عمق'):
        quote_fill(book, side='buy', quantity=1, at=at, decision_at=(at - timedelta(seconds=1)).isoformat(),
                   protocol=dict(slippage_bps=0, participation=.05))


def test_collector_preserves_price_but_quarantines_inconsistent_book_and_recovers(
        db, market_watch_rows, monkeypatch):
    times = iter(['2026-10-11T10:00:00+03:30', '2026-10-11T10:00:01+03:30',
                  '2026-10-11T10:00:02+03:30'])
    monkeypatch.setattr('data_collector.tsetmc.now_tehran_iso', lambda: next(times))
    broken = deepcopy(market_watch_rows[0])
    broken['blDs'][1]['pmd'] = 3900  # Below the 3980 limit: preserve price, not book.
    collector = _collector(db, {'GetMarketWatch': {'marketwatch': [broken]}, 'GetClientTypeAll': {}})
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))

    first = collector.collect_market_snapshot()
    observation = first['coverage']['per_instrument'][0]
    assert first['status'] == 'failed' and first['coverage']['valid'] == 0
    assert first['coverage']['rejected'] == first['book_quarantined'] == 1
    assert first['price_only_saved'] == 1
    assert observation['status'] == 'rejected' and observation['book_violations']
    assert observation.get('snapshot_binding') is None
    assert observation['price_only_binding']
    row = dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    assert row['last_price'] == 4220
    assert row['bid_price_1'] is None and row['bid_price_2'] is None and row['ask_price_1'] is None
    assert row['buy_queue_volume'] is None and row['sell_queue_volume'] is None

    second = collector.collect_market_snapshot()
    assert second['price_only_saved'] == 0 and second['price_only_unchanged'] == 1
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 1

    broken['blDs'][1]['pmd'] = 4210
    recovered = collector.collect_market_snapshot()
    assert recovered['status'] == 'success' and recovered['coverage']['valid'] == 1
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 2
    latest = dict(db.conn.execute('SELECT * FROM snapshots ORDER BY captured_at DESC').fetchone())
    assert latest['bid_price_2'] == 4210 and latest['buy_queue_volume'] == 3_837_639


def test_bad_book_after_good_snapshot_cannot_leave_old_book_as_latest(db, market_watch_rows, monkeypatch):
    times = iter(['2026-10-11T10:00:00+03:30', '2026-10-11T10:00:01+03:30'])
    monkeypatch.setattr('data_collector.tsetmc.now_tehran_iso', lambda: next(times))
    row = deepcopy(market_watch_rows[0])
    collector = _collector(db, {'GetMarketWatch': {'marketwatch': [row]}, 'GetClientTypeAll': {}})
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    assert collector.collect_market_snapshot()['status'] == 'success'
    row['blDs'][1]['pmd'] = 3900
    report = collector.collect_market_snapshot()
    assert report['coverage']['rejected'] == 1 and report['price_only_saved'] == 1
    latest = dict(db.conn.execute('SELECT * FROM snapshots ORDER BY captured_at DESC').fetchone())
    assert latest['last_price'] == 4220 and latest['bid_price_1'] is None
    assert latest['buy_queue_volume'] is None


def test_book_quarantine_does_not_rescue_an_invalid_price(db, market_watch_rows):
    row = deepcopy(market_watch_rows[0])
    row['blDs'][1]['pmd'] = 3900
    row['pdv'] = 5000
    collector = _collector(db, {'GetMarketWatch': {'marketwatch': [row]}, 'GetClientTypeAll': {}})
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    report = collector.collect_market_snapshot()
    assert report['coverage']['rejected'] == 1 and report['price_only_saved'] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 0


def test_capture_counts_quarantined_book_as_rejected_not_complete(db, market_watch_rows, clock):
    row = deepcopy(market_watch_rows[0])
    row['blDs'][1]['pmd'] = 3900
    db.upsert_symbol(dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300))
    repo, run_id = start_run(db)
    repo.append(run_id, 1, 'poll_started')
    report = _collector(db, {'GetMarketWatch': {'marketwatch': [row]}, 'GetClientTypeAll': {}}).collect_market_snapshot()
    finish(repo, run_id, 1, report)
    run = audit_intraday(db, '2026-10-14')['capture']['runs'][0]
    assert run['complete_scope_polls'] == 0
    assert run['per_symbol'][0]['rejected'] == 1 and run['per_symbol'][0]['valid'] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0] == 1


def test_code_identities_include_book_dependency():
    from config import settings
    from data_collector.daily_publications import CODE_FILES as publication_files
    from database.capture import CAPTURE_FILES, capture_code_identity
    from database.forward_observations import CODE_FILES as forward_files
    from database.paper import CODE_FILES as paper_files
    from ops.capital_capacity import ENGINE_FILES
    from ops.opportunity_radar import SOURCE_FILES as radar_files
    from ops.price_recheck import SOURCE_FILES as recheck_files
    from research.anniversary_run import FILES as anniversary_files

    for names in (CAPTURE_FILES, forward_files, paper_files, publication_files, ENGINE_FILES,
                  radar_files, recheck_files, anniversary_files):
        assert 'core/order_book.py' in names
    assert 'core/order_book.py' in capture_code_identity()['files']
    sources = {name: (settings.BASE_DIR / name).read_text(encoding='utf-8') for name in CAPTURE_FILES}
    original = capture_code_identity(sources=sources)
    sources['core/order_book.py'] += '\n# changed for hash verification\n'
    assert capture_code_identity(sources=sources)['sha256'] != original['sha256']
