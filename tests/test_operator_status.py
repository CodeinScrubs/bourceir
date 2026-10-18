"""پنل مشترک نباید داده حساب را تغییر دهد، نوع ابزار را حدس بزند یا اندازه سفارش بسازد."""

from datetime import datetime

import pandas as pd

from core.flow_filters import flow_metrics_v4
from core.risk import RiskProfile, plan_trade
from core.timeutils import TEHRAN_TZ
from database.paper import PaperRepository
from database.workbench import WorkbenchRepository
from ops.operator_status import _holding_scope, operator_status
from tests.test_flow_filters import history


def test_operator_empty_state_is_read_only_and_does_not_invent_a_trade(db):
    before = db.conn.total_changes
    result = operator_status(db, pd.DataFrame(), pd.DataFrame(), at=datetime(2024, 1, 1, 8, tzinfo=TEHRAN_TZ))
    assert result['accepted_candidates'] == 0 and result['candidates'] == []
    assert result['broker'] is None and result['paper_portfolios'] == []
    assert not result['automatic_orders'] and db.conn.total_changes == before


def test_scope_uses_verified_type_not_symbol_suffix_and_preserves_unknown_and_micro():
    result = _holding_scope([
        dict(symbol_fa='نام آزمون', research_scope_eligible=True, weight_pct=50., instrument_type_code=300),
        dict(symbol_fa='ابزار خارج دامنه', research_scope_eligible=False, weight_pct=.05, instrument_type_code=400),
        dict(symbol_fa='ابزار نامعلوم', research_scope_eligible=None, weight_pct=None, instrument_type_code=None)])
    assert (result['in_scope'], result['outside_scope'], result['unknown_scope'], result['micro_positions']) == (1, 1, 1, 1)


def test_operator_paper_summary_uses_actual_paper_ledger_without_mutation(db):
    PaperRepository(db).create(name='آزمون', capital_rial=100000, rationale='داده فقط در DB موقت')
    before = db.conn.total_changes
    result = operator_status(db, pd.DataFrame(), pd.DataFrame())
    assert result['paper_portfolios'][0]['cash_rial'] == 100000
    assert result['paper_portfolios'][0]['equity_rial'] == 100000
    assert db.conn.total_changes == before


def test_match_without_manual_budget_keeps_caps_unknown_and_no_signal_quantity(db):
    metrics = pd.DataFrame(dict(symbol=['الف'], date=['2024-01-01'], value_prior_20d_mean_rial=[30e9],
        turnover_percentile_lower=[.2], turnover_percentile_upper=[.3], last_to_official=[1.011],
        range_position=[.8], buyer_power_value=[1.8], per_capita_buy_mean_3d_rial=[3e9],
        per_capita_buy_mean_5d_rial=[2e9], legal_buy_fraction=[.29], not_locked_up=[True]))
    result = operator_status(db, metrics, pd.DataFrame())
    assert len(result['candidates']) == 1
    candidate = result['candidates'][0]
    assert candidate['signal_quantity'] is None and candidate['caps'] is None
    assert any('پروفایل' in x for x in candidate['blockers'])


def test_old_plan_budget_is_not_reverse_engineered_from_rounded_quantity(db):
    p, c = history(70)
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    db.upsert_daily_prices_df(sid, p.drop(columns='symbol'))
    metrics = flow_metrics_v4(p, c).tail(1).copy()
    metrics['symbol'] = 'آزمون'
    metrics[['turnover_percentile_lower', 'turnover_percentile_upper']] = [.2, .3]
    metrics[['last_to_official', 'range_position', 'buyer_power_value']] = [1.02, .8, 2.]
    metrics[['per_capita_buy_mean_3d_rial', 'per_capita_buy_mean_5d_rial', 'legal_buy_fraction']] = [3e9, 2e9, .2]
    metrics['not_locked_up'] = True
    profile = RiskProfile(100000000, 10000000)
    repo = WorkbenchRepository(db)
    repo.save_profile(profile, True)
    calculation = plan_trade(profile=profile, entry_rial=1000, stop_rial=980, target_rial=1050,
        trade_loss_budget_rial=10000, available_cash_rial=profile.capital_rial, equity_rial=profile.capital_rial,
        daily_value_rial=30e9, data_current=True, account_complete=True)
    repo.save_plan(symbol='آزمون', mode='actual', horizon='swing', entry_rial=1000, stop_rial=980, target_rial=1050,
        rationale='فقط آزمون', calculation=calculation)
    result = operator_status(db, metrics, pd.DataFrame())
    assert result['candidates'][0]['caps'] is None
    assert any('بودجه اولیه' in r for r in result['candidates'][0]['blockers'])
