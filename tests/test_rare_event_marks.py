"""Fixed-calendar synthetic-mark cases; valuations are not trading fills."""
import copy
import json

import pandas as pd
import pytest

from config import settings
from research.rare_event_marks import VERSION, compute_marked_rare_events, summarize_marked_events
from research.rare_events import compute_rare_events
from tests.test_no_trade_valuation import receipt

CALENDAR=['2024-01-01','2024-01-02','2024-01-03','2024-01-04']
DECISIONS=pd.DataFrame([dict(symbol='آزمون',date=CALENDAR[0])])


def panel():
    return pd.DataFrame([dict(symbol='آزمون',date=d,close_final_raw=c,ref_price_raw=r,volume=10)
        for d,c,r in [(CALENDAR[0],100.,100.),(CALENDAR[2],200.,100.),(CALENDAR[3],200.,200.)]])


def run(prices=None,records=None,**kwargs):
    return compute_marked_rare_events(panel() if prices is None else prices,DECISIONS,CALENDAR,
        observed_through=CALENDAR[-1],receipts=[receipt()] if records is None else records,
        horizons=(1,2,3),**kwargs)


def test_actual_mark_closes_a_gap_without_executable_return():
    old=compute_rare_events(panel(),DECISIONS,CALENDAR,observed_through=CALENDAR[-1],horizons=(1,2,3))
    assert list(old.status)==['missing_observation']*3
    new=run()
    assert list(new.status)==['complete_no_hit','hit','hit']
    assert list(new.no_trade_mark_sessions)==[1,1,1]
    assert list(new.endpoint_multiple)==[1.,2.,2.]
    assert new.label_contract.eq(VERSION).all()
    assert json.loads(new.no_trade_mark_dates.iloc[0])==[CALENDAR[1]]
    assert new.economic_double.isna().all() and new.executable_return.isna().all()
    assert panel().shape[0]==3


def test_empty_ledger_keeps_every_original_column_identical():
    old=compute_rare_events(panel(),DECISIONS,CALENDAR,observed_through=CALENDAR[-1],horizons=(1,2,3))
    new=run(records=[])
    pd.testing.assert_frame_equal(old,new.loc[:,old.columns])
    assert new.no_trade_mark_sessions.eq(0).all()


@pytest.mark.parametrize('record_changes',[{'pClosing':0},{'priceYesterday':0},{'pClosing':101},
    {'insCode':'0'},{'priceYesterday':True}])
def test_unusable_mark_cannot_resolve_a_path(record_changes):
    result=run(records=[receipt(native_changes=record_changes)])
    assert result.outcome.isna().all() and result.endpoint_multiple.isna().all()
    assert result.no_trade_mark_sessions.eq(0).all()


def test_flat_mark_with_wrong_prior_reference_is_a_conflict():
    result=run(records=[receipt(native_changes=dict(pClosing=99,priceYesterday=99))])
    assert result.status.eq('no_trade_reference_conflict').all()
    assert result.outcome.isna().all()


def test_later_agreement_cannot_bridge_an_earlier_missing_day():
    prices=panel().iloc[[0,2]].copy()
    result=run(prices=prices,records=[receipt(CALENDAR[2])])
    assert result.status.eq('missing_observation').all() and result.no_trade_mark_sessions.eq(0).all()


def test_consecutive_marks_require_every_day_and_link():
    prices=panel().iloc[[0,2]].copy()
    result=run(prices=prices,records=[receipt(),receipt(CALENDAR[2])])
    assert result.status.eq('complete_no_hit').all()
    assert list(result.no_trade_mark_sessions)==[1,2,2]


def test_mark_cannot_supply_a_missing_trading_anchor():
    prices=panel().iloc[[1,2]].copy()
    result=run(prices=prices,records=[receipt(CALENDAR[0])])
    assert result.status.eq('missing_anchor').all() and result.outcome.isna().all()


def test_existing_raw_bar_cannot_be_overwritten():
    with pytest.raises(ValueError,match='جایگزینی'):
        run(records=[receipt(CALENDAR[2])])


def test_duplicate_normalized_keys_cannot_select_a_better_source_revision():
    with pytest.raises(ValueError):
        run(records=[receipt(),receipt()])


@pytest.mark.parametrize('day',['2024-01-05',settings.RESEARCH_CONFIG['holdout_start']])
def test_date_scope_is_checked_before_source_values(monkeypatch,day):
    import research.rare_event_marks as module
    monkeypatch.setattr(module,'parse_no_trade_receipt',lambda *_:pytest.fail('numeric bytes consumed before scope guard'))
    with pytest.raises(ValueError):
        run(records=[receipt(day)])


def test_off_calendar_mark_does_not_create_a_session():
    raw=receipt();raw['task']['date']='2023-12-31'
    with pytest.raises(ValueError):
        run(records=[raw])


def test_raw_off_calendar_observation_still_invalidates_the_path():
    prices=pd.concat([panel(),pd.DataFrame([dict(symbol='آزمون',date='2024-01-02T00:00:00',close_final_raw=100,ref_price_raw=100,volume=10)])])
    with pytest.raises(ValueError):
        run(prices=prices)


def test_a_confirmed_hit_survives_a_later_unusable_mark():
    prices=panel().loc[lambda p:p.date.ne(CALENDAR[3])].copy()
    result=run(prices=prices,records=[receipt(),receipt(CALENDAR[3],native_changes={'pClosing':0})])
    assert result.iloc[-1].status=='hit' and result.iloc[-1].outcome
    assert pd.isna(result.iloc[-1].endpoint_multiple)
    assert result.iloc[-1].first_hit_date==CALENDAR[2]


def test_roundtrip_and_summary_keep_all_unknowns(tmp_path):
    result=run()
    path=tmp_path/'outcomes.parquet';result.to_parquet(path,index=False)
    pd.testing.assert_frame_equal(result,pd.read_parquet(path))
    summaries=summarize_marked_events(result)
    assert all(s['opportunities']==1 and s['paths_with_no_trade_marks']==1 for s in summaries)
    assert all(not s['economic_profit_verified'] and not s['accepted_for_signals'] for s in summaries)


def test_raw_inputs_and_receipts_are_not_mutated():
    prices=panel(); records=[receipt()]; saved_prices=prices.copy(deep=True);saved_records=copy.deepcopy(records)
    run(prices,records)
    pd.testing.assert_frame_equal(prices,saved_prices)
    assert records==saved_records
