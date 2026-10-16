"""Temporary-fixture integration only: no market forecast, real data or broker order.

Exercise the actual SQLite, feature publisher/reader, observed paper ledger,
partial exit, idempotency, private JSON export and read-only database reopen.
"""

import json
from datetime import timedelta

import pytest

from config import settings
from database.db import DatabaseManager
from database.paper import PaperRepository
from features.daily import FEATURE_SET_VERSION
from features.store import build_feature_store, load_feature_snapshot
from ops.report_files import write_private_report
from ops.research_preflight import research_preflight
from tests.test_client_quality import coherent_history
from tests.test_paper import idea, portfolio, snapshot, trade
from tests.test_paper import paper as paper


def test_fixture_history_to_features_paper_partial_exit_export_and_reopen(paper, db, tmp_path, monkeypatch):
    repo, clock = paper
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    prices, clients = coherent_history()
    sid = db.get_symbol_id('فولاد')
    db.upsert_daily_prices_df(sid, prices)
    db.upsert_client_types_df(sid, clients)
    preflight = research_preflight(db)
    assert preflight['input_contract_passed'] and preflight['coherent_client_pairs'] == 25
    assert not preflight['accepted_for_signals']
    root = tmp_path / 'features'
    manifest = build_feature_store(db, symbols=['فولاد'], root=root)
    panel, published = load_feature_snapshot(root=root)
    assert published == manifest and published['version'] == FEATURE_SET_VERSION
    assert panel.buyer_power.iloc[-1] == pytest.approx(1.2)

    key = portfolio(paper, mode='observed')
    iid = idea(repo, key, thesis='Functional fixture; input ' + manifest['provenance']['parquet_sha256'])
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, iid, price_rial=None, request_id='pipeline-buy')
    trade(repo, key, iid, price_rial=None, request_id='pipeline-buy')
    assert len(repo.report(key)['events']) == 1
    for quantity in (4, 6):
        clock[0] += timedelta(seconds=10)
        snapshot(db, clock[0], last_price=1050, bid_price_1=1049, ask_price_1=1050)
        trade(repo, key, iid, side='sell', quantity=quantity, price_rial=None,
              request_id='pipeline-sell-' + str(quantity))
        if quantity == 4:
            assert repo.report(key)['ledger']['positions'][iid]['quantity'] == 6

    exported = repo.export(key)
    ledger = exported['report']['ledger']
    trades = [event['payload'] for event in exported['events']]
    fees = exported['protocol']['fees']
    expected_cash = 100000.
    for event in trades:
        gross = event['quantity'] * event['price_rial']
        expected_cash += -gross * (1 + fees['buy']) if event['side'] == 'buy' else gross * (1 - fees['sell'])
    assert ledger['cash_rial'] == pytest.approx(expected_cash)
    assert ledger['equity_rial'] == pytest.approx(expected_cash)
    assert ledger['realized_net_rial'] == pytest.approx(expected_cash - 100000.)
    assert not exported['accepted_for_signals'] and not exported['report']['profitability_proven']
    target = write_private_report(tmp_path / 'private' / 'paper.json', exported, forbidden_paths=[db.db_path])
    assert len(json.loads(target.read_text(encoding='utf-8'))['events']) == 3

    with DatabaseManager(db_path=db.db_path, read_only=True) as reopened:
        restored = PaperRepository(reopened).export(key)
        assert restored['events'] == exported['events']
        assert restored['report']['ledger'] == ledger
        assert research_preflight(reopened)['raw_inputs'] == preflight['raw_inputs']
    after, after_manifest = load_feature_snapshot(root=root)
    assert after_manifest == manifest and after.equals(panel)
