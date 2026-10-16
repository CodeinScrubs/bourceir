"""آزمون ضد قطعیت کاذب؛ همه داده‌ها مصنوعی و فقط در محیط آزمون‌اند."""

import pytest

from core.query_screener import daily_evidence, trade_evidence
from tests.test_query_screener import bar, trades


@pytest.mark.parametrize('daily', [
    bar(volume=3), bar(trade_count=3), bar(value=400),
    bar(open_raw=99), bar(close_raw=100),
])
def test_conflicting_tick_totals_or_exhausted_path_cannot_confirm_a_witness(daily):
    result = trade_evidence(trades(), daily)
    assert result['event_confirmed'] is None
    assert not result['complete']
    assert result['contradictory']


@pytest.mark.parametrize('volume', [1.5, float('inf'), True])
def test_fractional_or_nonfinite_daily_share_volume_is_not_candle_evidence(volume):
    result = daily_evidence(bar(open_raw=98, volume=volume))
    assert result['event_confirmed'] is None and result['event_possible'] is None


def test_ninety_five_percent_tick_coverage_does_not_confirm_absence():
    source = trades((100, 102, 98, 101))
    for row in source['response']['tradeHistory']:
        row['qTitTran'] = 19
    result = trade_evidence(source, bar(volume=80, trade_count=5, value=8020))
    assert not result['complete'] and result['event_confirmed'] is None


def test_official_close_is_never_a_timed_last_trade_witness():
    assert daily_evidence(bar(close_final_raw=105))['event_confirmed'] is None


def test_tick_price_outside_daily_range_is_a_conflict():
    result = trade_evidence(trades((100, 97, 102, 101)), bar())
    assert result['contradictory'] and result['event_confirmed'] is None


def test_query_downgrades_candle_proof_when_provider_has_contradictory_totals(db, tmp_path, monkeypatch):
    from ops.query_screener import evaluate_query, start_query
    from tests.test_query_screener import make_query

    make_query(db, tmp_path, monkeypatch)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET close_raw=102')
    folder = start_query(db, dict(start='2024-02-03', end='2024-02-03', cap_low=0, cap_high=100))
    class Provider:
        def get_trade_session(self, ins_code, day):
            source = trades(ins_code=ins_code)
            for row in source['response']['tradeHistory']:
                row.update(insCode=ins_code, qTitTran=2)
            return source
    result, _ = evaluate_query(folder, provider=Provider(), max_trade_days=1)
    assert result['results'][0]['confirmed_count'] == 0 and result['results'][0]['ambiguous_count'] == 1
    evidence = next(iter(result['details'].values()))[0]
    assert evidence['evidence'] == 'daily_trade_conflict' and evidence['tick_evidence']['contradictory']
