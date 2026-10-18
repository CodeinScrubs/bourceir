"""ابهام اقتصادی در همه خروجی‌ها محفوظ است؛ داده آزمون فقط در SQLite موقت است."""

import json
from datetime import timedelta

import pandas as pd
import pytest

from ops.operator_status import operator_status
from tests.test_paper import idea, portfolio, snapshot, trade
from tests.test_paper import paper as paper


@pytest.mark.parametrize('sold', [0, 5, 10])
def test_unresolved_action_masks_profit_in_report_export_and_saved_valuation(paper, db, sold):
    repo, clock = paper
    key = portfolio(paper)
    affected = idea(repo, key)
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, affected)
    db.insert_daily_price(db.get_symbol_id('فولاد'), '2026-10-13', open=1000, high=1100,
                          low=950, close=1000, close_final_raw=1000, ref_price_raw=1000)
    clock[0] += timedelta(days=1)
    snapshot(db, clock[0], ref_price=500, last_price=500, bid_price_1=499, ask_price_1=500,
             low_price=450, high_price=550, price_limit_down=400, price_limit_up=600)
    if sold:
        trade(repo, key, affected, side='sell', quantity=sold, price_rial=500)
    before = db.conn.total_changes
    report = repo.report(key)
    exported = json.loads(json.dumps(repo.export(key)))['report']
    assert db.conn.total_changes == before
    for result in (report, exported):
        assert any('شکست مرجع' in issue for issue in result['integrity'][affected])
        assert result['equity_rial'] is None and result['return_pct'] is None
        assert result['ledger']['realized_net_rial'] is None
        item, = result['attribution']
        assert item['realized_net_rial'] is None and item['unrealized_net_rial'] is None
        if sold < 10:
            position = result['ledger']['positions'][affected]
            assert position['quantity'] == 10 - sold
            assert position['liquidation_value_rial'] is None and position['unrealized_net_rial'] is None
        assert result['ledger']['cash_rial'] == pytest.approx(100_000 - 10_000 * 1.003712 + sold * 500 * .9912)
        assert result['ledger']['fees_rial'] == pytest.approx(10_000 * .003712 + sold * 500 * .0088)
    saved = repo.save_valuation(key)
    assert saved['ledger']['realized_net_rial'] is None and saved['total_net_rial'] is None
    assert repo.export(key)['events'][-1]['payload'] == saved
    assert db.conn.execute('SELECT count(*) FROM corporate_actions').fetchone()[0] == 0


def test_uncertain_idea_does_not_erase_independent_resolved_idea(paper, db):
    repo, clock = paper
    key = portfolio(paper)
    resolved, affected = idea(repo, key), idea(repo, key, label='تحلیل با ابهام بعدی')
    clock[0] += timedelta(seconds=10)
    snapshot(db, clock[0])
    trade(repo, key, resolved)
    trade(repo, key, resolved, side='sell', price_rial=1100)
    trade(repo, key, affected)
    db.insert_daily_price(db.get_symbol_id('فولاد'), '2026-10-13', open=1000, high=1100,
                          low=950, close=1000, close_final_raw=1000, ref_price_raw=1000)
    clock[0] += timedelta(days=1)
    snapshot(db, clock[0], ref_price=500, last_price=500, bid_price_1=499, ask_price_1=500,
             low_price=450, high_price=550, price_limit_down=400, price_limit_up=600)
    trade(repo, key, affected, side='sell', price_rial=500)
    result = repo.report(key)
    by_idea = {a['id']: a for a in result['attribution']}
    assert result['integrity'][resolved] == [] and result['integrity'][affected]
    assert by_idea[resolved]['realized_net_rial'] == pytest.approx(11_000 * .9912 - 10_000 * 1.003712)
    assert by_idea[affected]['realized_net_rial'] is None
    assert result['ledger']['realized_net_rial'] is None


@pytest.mark.parametrize('state', ['untraded', 'open', 'closed', 'unknown_mark'])
def test_operator_distinguishes_healthy_idea_entries_from_actual_integrity_issues(paper, db, state):
    repo, clock = paper
    key = portfolio(paper)
    iid = idea(repo, key)
    if state != 'untraded':
        clock[0] += timedelta(seconds=10)
        if state != 'unknown_mark':
            snapshot(db, clock[0])
        trade(repo, key, iid)
        if state == 'closed':
            trade(repo, key, iid, side='sell', price_rial=1100)
    before = db.conn.total_changes
    result = operator_status(db, pd.DataFrame(), pd.DataFrame(), at=clock[0])
    assert db.conn.total_changes == before
    row, = result['paper_portfolios']
    assert row['error'] is None and row['runtime_matches']
    assert row['integrity_unknown'] is (state == 'unknown_mark')
    assert (row['equity_rial'] is None) is (state == 'unknown_mark')
