"""Data gates and evidence semantics; all artificial market rows remain in tests."""

from copy import deepcopy

import pytest

from research.sell_wall import Quote
from research.sell_wall_feasibility import event_evidence, seconds, summarize


def observation():
    return dict(
        date="2020-07-06",
        status="depletion_observed",
        book_payload_identity_verified=False,
        book_payload_date_verified=False,
        daily=dict(status="reconciled", trade_payload_identity_verified=False, trade_payload_date_verified=False),
        event=dict(
            anchor_time="10:00:00",
            time="10:01:00",
            elapsed_seconds=60,
            initial_wall_shares=5_000_000,
            remaining_floor_ask_shares=500_000,
            observed_floor_trade_shares=1_000_000,
            min_floor_volume_met=True,
            five_levels_at_anchor=False,
            five_levels_at_event=False,
        ),
    )


def quote(price, shares=1000):
    return (0, 95, 110, Quote(94, 100, 1, price, shares, 1), False)


def test_first_observed_ask_is_used_not_cheapest_or_last_quote_before_delay():
    observed = observation()
    source = deepcopy(observed)
    at = seconds("10:01:00")
    result = event_evidence(observed, [(at + 14, quote(95)), (at + 16, quote(107)), (at + 20, quote(96))])
    selected = result["first_observed_ask_after_15s"]
    assert selected["price_rial"] == 107 and selected["seconds_after_delay"] == 1
    assert selected["hypothetical_fill_quantity"] is None and selected["actual_fill_probability"] is None
    assert not selected["displayed_best_ask_covers_reference_quantity"]
    assert result["source_publication_latency_seconds"] is None
    assert result["event_time_tehran"] == "2020-07-06T10:01:00+04:30"
    assert observed == source


def test_missing_quote_is_unknown_without_forward_fill_or_search_past_declared_window():
    at = seconds("10:01:00")
    result = event_evidence(observation(), [(at + 14, quote(95)), (at + 15, None), (at + 75, quote(100))])
    assert result["first_observed_ask_after_15s"] is None
    assert result["ambiguous_quote_seconds_in_local_window"] == [at + 15]
    assert result["gap_is_proven_source_outage"] is False
    with pytest.raises(ValueError, match="chronological"):
        event_evidence(observation(), [(at + 20, quote(96)), (at + 16, quote(107))])


def test_floor_trade_volume_above_net_queue_drop_does_not_create_cancellation_estimate():
    observed = observation()
    observed["event"]["observed_floor_trade_shares"] = 6_000_000
    result = event_evidence(observed, [])
    assert result["queue_drop_minus_observed_floor_trades_shares"] == -1_500_000
    assert result["identified_cancellation_volume"] is None


def test_unknown_no_event_and_empty_control_group_remain_separate():
    observed = observation()
    event = event_evidence(observed, [])
    rows = [
        dict(target=dict(entry_date="2020-07-06", ins_code="123"), observed=observed, event=event),
        dict(target=dict(entry_date="2020-07-07", ins_code="123"), observed=dict(status="unknown"), event=None),
        dict(
            target=dict(entry_date="2020-07-06", ins_code="456"),
            observed=dict(status="depletion_not_observed", daily=dict(status="reconciled")),
            event=None,
        ),
    ]
    result = summarize(rows)
    assert result["registered_symbol_days"] == 3 and result["unique_target_dates"] == 2
    assert result["assessable_quote_days"] == 2 and result["unresolved_source_days"] == 1
    assert result["no_event_observed_days"] == 1 and result["unique_event_dates"] == 1
    assert result["volume_corroborated_events"] == 1 and result["insufficient_volume_events"] == 0
    assert result["comparator_group_is_empty"]
    assert result["decision"] == "DATA INSUFFICIENT" and result["economic_outcomes_computed"] is False


def test_gate_uses_evidence_and_declared_design_instead_of_forcing_a_fixed_verdict():
    rows = []
    for code, day, supported in [("123", "2020-07-06", True), ("456", "2020-07-07", False)]:
        observed = observation()
        observed.update(book_payload_identity_verified=True, book_payload_date_verified=True)
        observed["daily"].update(trade_payload_identity_verified=True, trade_payload_date_verified=True)
        event = event_evidence(observed, [])
        event.update(min_floor_volume_met=supported, financial_entry_feasibility_verified=True)
        rows.append(dict(target=dict(ins_code=code, entry_date=day), observed=observed, event=event))
    assert summarize(rows)["decision"] == "DATA INSUFFICIENT"
    passed = summarize(rows, statistical_design_registered=True)
    assert passed["decision"] == "DATA SUFFICIENT" and passed["verified_realtime_15s_entry_observations"] == 2
    assert passed["economic_outcomes_computed"] is False and passed["actual_fills_verified"] is False


@pytest.mark.parametrize("stamp", ["10:61:00", "24:00:00", "10:00", "10:a:01"])
def test_invalid_clock_is_rejected(stamp):
    with pytest.raises(ValueError):
        seconds(stamp)
