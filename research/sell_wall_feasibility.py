"""Offline data feasibility for a saved sell-wall pilot; no financial replay."""

import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from config import settings
from core.queue_fall_study import MAX_BOOK_BYTES, MAX_THRESHOLD_BYTES, _payload, book_request, trade_observation
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, now_tehran_iso
from ops.queue_fall_archive import _restore
from ops.validation_run import write_state
from research import sell_wall
from research.run import _sha256

VERSION = "sell-wall-data-feasibility-v1"
REFERENCE_BUDGET_RIAL = 1_340_000_000
DELAY_SECONDS = 15


def seconds(value):
    parts = value.split(":")
    if len(parts) != 3 or any(not x.isdigit() for x in parts):
        raise ValueError("An exact source HH:MM:SS is required")
    hour, minute, second = map(int, parts)
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        raise ValueError("Invalid source time")
    return hour * 3600 + minute * 60 + second


def event_evidence(observed, quotes, *, budget=REFERENCE_BUDGET_RIAL):
    """Use the FIRST observable ask, never the best later price or a carried quote."""
    event = observed["event"]
    if event is None:
        return None
    if [at for at, _ in quotes] != sorted({at for at, _ in quotes}):
        raise ValueError("Quote seconds must be unique and chronological")
    anchor, trigger = seconds(event["anchor_time"]), seconds(event["time"])
    after = trigger + DELAY_SECONDS
    first = next(
        (
            (at, state)
            for at, state in quotes
            if after <= at < after + sell_wall.AUDIT_SECONDS and state is not None and state[3].ask_price > 0
        ),
        None,
    )
    selected = None
    if first is not None:
        at, state = first
        top = state[3]
        quantity = math.floor(budget / (top.ask_price * (1 + settings.FEES["buy"])))
        selected = dict(
            source_time=sell_wall._hms(at),
            seconds_after_trigger=at - trigger,
            seconds_after_delay=at - after,
            price_rial=top.ask_price,
            displayed_shares=top.ask_shares,
            displayed_notional_rial=top.ask_price * top.ask_shares,
            five_levels_same_revision=state[4],
            floor_rial=state[1],
            ceiling_rial=state[2],
            reference_budget_rial=budget,
            reference_quantity_including_buy_fee=quantity,
            displayed_best_ask_covers_reference_quantity=top.ask_shares >= quantity,
            hypothetical_fill_quantity=None,
            actual_fill_probability=None,
        )
    times = [at for at, _ in quotes if anchor <= at < after + sell_wall.AUDIT_SECONDS]
    unknown_states = [at for at, state in quotes if anchor <= at < after + sell_wall.AUDIT_SECONDS and state is None]
    quote_gaps = [b - a for a, b in zip(times, times[1:])]
    # This residual equals no identified cancellation quantity. New/revised
    # orders and incomplete observation can affect either side of the equation.
    residual = event["initial_wall_shares"] - event["remaining_floor_ask_shares"] - event["observed_floor_trade_shares"]
    return dict(
        **event,
        event_time_tehran=datetime.fromisoformat(observed["date"] + "T" + event["time"])
        .replace(tzinfo=TEHRAN_TZ)
        .isoformat(),
        queue_drop_minus_observed_floor_trades_shares=residual,
        identified_cancellation_volume=None,
        corroboration="observed_floor_prints" if event["min_floor_volume_met"] else "insufficient_floor_prints",
        first_observed_ask_after_15s=selected,
        ambiguous_quote_seconds_in_local_window=unknown_states,
        maximum_observed_quote_gap_seconds=max(quote_gaps, default=None),
        gap_is_proven_source_outage=False,
        exchange_clock_synchronization_verified=False,
        source_publication_latency_seconds=None,
        order_level_priority_available=False,
        financial_entry_feasibility_verified=False,
    )


def book_quality(book, threshold, code, day):
    payload = _payload(book, code, day, book_request(code, day), MAX_BOOK_BYTES)
    native = payload["bestLimitsHistory"]
    groups, seen, repeated = defaultdict(set), set(), 0
    raw_clock_reversals = 0
    previous = None
    for row in native:
        key = row["hEven"], row["refID"], row["number"]
        signature = (*key, *(row.get(k) for k in ("pMeDem", "qTitMeDem", "zOrdMeDem", "pMeOf", "qTitMeOf", "zOrdMeOf")))
        repeated += signature in seen
        seen.add(signature)
        if 1 <= row["number"] <= 5:
            groups[row["hEven"], row["refID"]].add(row["number"])
        raw_clock_reversals += previous is not None and row["hEven"] < previous
        previous = row["hEven"]
    quotes, identity, date = sell_wall._book_seconds(book, threshold, code, day)
    first, last = sell_wall._seconds(sell_wall.EARLIEST_CLOCK), sell_wall._seconds(sell_wall.LATEST_CLOCK)
    local = [(at, q) for at, q in quotes if first <= at <= last]
    return quotes, dict(
        raw_rows=len(native),
        duplicate_identical_rows=repeated,
        raw_clock_reversals=raw_clock_reversals,
        raw_list_is_arrival_order_verified=False,
        native_instrument_id_verified=identity,
        native_date_verified=date,
        explicit_revision_groups=len(groups),
        explicit_five_level_groups=sum(len(v) == 5 for v in groups.values()),
        maximum_levels_in_one_revision=max(map(len, groups.values()), default=0),
        usable_top_seconds_in_event_window=sum(q is not None for _, q in local),
        ambiguous_top_seconds_in_event_window=sum(q is None for _, q in local),
        full_snapshot_vs_delta_feed_semantics_verified=False,
        missing_interval_count=None,
        full_session_coverage_verified=False,
    )


def _read_json(path):
    return json.loads(path.read_bytes())


def _capture(source, receipt, inputs):
    target = receipt["target"]
    code, day = target["ins_code"], target["entry_date"]
    if (
        not isinstance(code, str)
        or not code.isascii()
        or not code.isdigit()
        or not is_canonical_iso_date(day)
        or day >= settings.RESEARCH_CONFIG["holdout_start"]
    ):
        raise ValueError("Development-only native identities and dates are required")
    folder = (source / f"{day}-{code}").resolve()
    if not folder.is_relative_to(source):
        raise ValueError("Source observation escapes its archived folder")
    path = folder / "audit.json"
    if _sha256(path) != receipt["audit_sha256"]:
        raise ValueError("Archived session audit hash differs")
    inputs[str(path.relative_to(source))] = receipt["audit_sha256"]
    audit = _read_json(path)
    if audit["target"] != target:
        raise ValueError("Session target differs from registered request")
    daily_path = folder / "dated-daily.json"
    digest = _sha256(daily_path) if daily_path.is_file() else None
    if digest != receipt["daily_sha256"]:
        raise ValueError("Dated daily archive hash differs")
    if digest:
        inputs[str(daily_path.relative_to(source))] = digest
    daily = _read_json(daily_path) if digest else None
    observations, sources = {}, {}
    for name, limit in [("book", MAX_BOOK_BYTES), ("trade", MAX_BOOK_BYTES), ("threshold", MAX_THRESHOLD_BYTES)]:
        file = folder / (name + ".json.gz")
        metadata = audit["receipts"][name]["metadata"]
        compressed = file.read_bytes() if file.is_file() else None
        observations[name] = _restore(metadata, compressed, limit)
        if compressed is not None:
            inputs[str(file.relative_to(source))] = _sha256(file)
        if metadata is None:
            sources[name] = None
            continue
        requested, received = (datetime.fromisoformat(metadata[k]) for k in ("requested_at", "received_at"))
        sources[name] = {
            k: metadata[k] for k in ("url", "ins_code", "requested_at", "received_at", "body_sha256", "http_status")
        }
        sources[name].update(
            archive_request_duration_ms=(received - requested).total_seconds() * 1000,
            historical_publication_latency_ms=None,
        )
    return observations, daily, sources


def summarize(rows, *, statistical_design_registered=False):
    if type(statistical_design_registered) is not bool:
        raise ValueError("An explicit statistical-design status is required")
    events = [r for r in rows if r["event"] is not None]
    known = [r for r in rows if r["observed"]["status"] in ("depletion_observed", "depletion_not_observed")]
    corroborated = sum(r["event"]["min_floor_volume_met"] for r in events)
    uncorroborated = len(events) - corroborated
    blockers = []
    if not corroborated or not uncorroborated:
        blockers.append("both_comparison_groups_not_observed")
    if any(
        not (
            r["observed"].get("book_payload_identity_verified")
            and r["observed"].get("book_payload_date_verified")
            and (r["observed"].get("daily") or {}).get("trade_payload_identity_verified")
            and (r["observed"].get("daily") or {}).get("trade_payload_date_verified")
        )
        for r in events
    ):
        blockers.append("event_sources_bound_to_request_only_not_native_identity_and_date")
    if not events or any(not r["event"]["financial_entry_feasibility_verified"] for r in events):
        blockers.append("timestamped_15s_execution_capacity_not_established")
    # A data audit does not infer power or a financial trial registration from
    # the number of detected price events.
    if not statistical_design_registered:
        blockers.append("independent_date_effect_variance_and_sample_size_plan_missing")
    return dict(
        version=VERSION,
        decision="DATA INSUFFICIENT" if blockers else "DATA SUFFICIENT",
        material_blockers=blockers,
        registered_symbol_days=len(rows),
        unique_target_dates=len({r["target"]["entry_date"] for r in rows}),
        unique_target_instruments=len({r["target"]["ins_code"] for r in rows}),
        daily_reconciled_days=sum((r["observed"].get("daily") or {}).get("status") == "reconciled" for r in rows),
        assessable_quote_days=len(known),
        no_event_observed_days=sum(r["observed"]["status"] == "depletion_not_observed" for r in rows),
        unresolved_source_days=len(rows) - len(known),
        observed_event_count=len(events),
        volume_corroborated_events=corroborated,
        insufficient_volume_events=uncorroborated,
        unique_event_dates=len({r["target"]["entry_date"] for r in events}),
        explicit_five_level_event_pairs=sum(
            r["event"]["five_levels_at_anchor"] and r["event"]["five_levels_at_event"] for r in events
        ),
        observed_asks_in_15s_plus_60s_windows=sum(
            r["event"]["first_observed_ask_after_15s"] is not None for r in events
        ),
        verified_realtime_15s_entry_observations=sum(
            r["event"]["financial_entry_feasibility_verified"] for r in events
        ),
        actual_fills_verified=False,
        missing_intervals_identified=None,
        actual_queue_priority_verified=False,
        corroboration_does_not_prove_all_depletion_was_traded=True,
        comparator_group_is_empty=uncorroborated == 0,
        observed_day_status_counts=dict(Counter(r["observed"]["status"] for r in rows)),
        eligible_for_financial_registration=not blockers,
        economic_outcomes_computed=False,
        holdout_loaded=False,
        external_requests=0,
        models_trained=0,
    )


def run_audit(source, output):
    private = (settings.DATA_DIR / "private").resolve()
    source, output = Path(source).resolve(), Path(output).resolve()
    if any(p == private or not p.is_relative_to(private) for p in (source, output)):
        raise ValueError("Explicit private source and fresh output paths are required")
    protocol, capture = (_read_json(source / name) for name in ("protocol.json", "capture.json"))
    saved = _read_json(source / "analysis-v1/result.json")
    analysis = _read_json(source / "analysis-v1/analysis-protocol.json")
    if (
        capture["protocol_sha256"] != _sha256(source / "protocol.json")
        or analysis["capture_sha256"] != _sha256(source / "capture.json")
        or saved["analysis_protocol_sha256"] != _sha256(source / "analysis-v1/analysis-protocol.json")
        or protocol["end_exclusive"] > settings.RESEARCH_CONFIG["holdout_start"]
        or protocol["holdout_opened"]
        or capture["holdout_opened"]
        or analysis["event"] != protocol["event"]
        or len(saved["rows"]) != len(protocol["targets"])
        or capture["unattempted"]
        or len(capture["receipts"]) != len(protocol["targets"])
        or [r["target"] for r in capture["receipts"]] != protocol["targets"]
    ):
        raise ValueError("Pilot source linkage, denominator or development boundary differs")
    for name, digest in analysis["code_files"].items():
        if Path(name).name != name or _sha256(source / "analysis-v1" / name) != digest:
            raise ValueError("Archived analysis source differs")
    if _sha256(settings.BASE_DIR / "research/sell_wall.py") != analysis["code_files"]["sell_wall.py"]:
        raise ValueError("Event detector changed; a separate versioned comparison is required")
    inputs = {
        name: _sha256(source / name)
        for name in ["protocol.json", "capture.json", "analysis-v1/result.json", "analysis-v1/analysis-protocol.json"]
    }
    output.mkdir(parents=True, exist_ok=False)
    code = {
        name: _sha256(settings.BASE_DIR / name)
        for name in [
            "research/sell_wall_feasibility.py",
            "research/sell_wall.py",
            "core/minute_candles.py",
            "core/queue_fall_study.py",
            "ops/queue_fall_archive.py",
        ]
    }
    write_state(
        output / "scope.json",
        dict(
            version=VERSION,
            created_at=now_tehran_iso(),
            source=str(source),
            source_inputs_sha256=inputs.copy(),
            code_sha256=code,
            registered_targets=len(protocol["targets"]),
            event_definition=protocol["event"],
            reference_budget_rial=REFERENCE_BUDGET_RIAL,
            reference_budget_is_not_authorized_order=True,
            source_selection=protocol["selection"],
            external_requests=0,
            financial_backtests=0,
            models_trained=0,
            holdout_loaded=False,
        ),
    )
    rows = []
    for index, receipt in enumerate(capture["receipts"]):
        observations, daily, sources = _capture(source, receipt, inputs)
        code_id, day = receipt["target"]["ins_code"], receipt["target"]["entry_date"]
        tape = trade_observation(observations["trade"], code_id, day) if observations["trade"] else None
        observed = sell_wall.study_sell_wall(observations["book"], observations["threshold"], tape, daily, code_id, day)
        old = saved["rows"][index]
        if (
            old["target"] != receipt["target"]
            or observed["status"] != old["status"]
            or observed["event"] != old["event"]
        ):
            raise ValueError("Saved pilot event result did not reproduce")
        quality, quotes, issue = None, [], None
        try:
            quotes, quality = book_quality(observations["book"], observations["threshold"], code_id, day)
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            issue = str(exc)
        event = event_evidence(observed, quotes)
        rows.append(
            dict(
                target=receipt["target"],
                observed=observed,
                book_quality=quality,
                book_issue=issue,
                event=event,
                sources=sources,
                daily_evidence="saved decoded JSON projection; raw HTTP daily body not established",
            )
        )
        if (index + 1) % 10 == 0:
            print(json.dumps(dict(reviewed=index + 1, expected=len(capture["receipts"]))), flush=True)
    result = summarize(rows)
    result.update(
        registered_at=protocol["registered_at"],
        pilot_selection=protocol["selection"],
        eligible_prior_days=sum(d["eligible_prior_days"] for d in protocol["eligible_prior_days_by_fold"]),
        counts_not_representative_of_all_market_days=True,
    )
    if any(_sha256(source / name) != digest for name, digest in inputs.items()):
        raise ValueError("Pilot source changed during review")
    if any(_sha256(settings.BASE_DIR / name) != digest for name, digest in code.items()):
        raise ValueError("Audit source changed during review")
    write_state(output / "symbol-days.json", rows)
    write_state(
        output / "event-evidence.json",
        [
            dict(
                target=r["target"],
                event=r["event"],
                book_quality=r["book_quality"],
                daily=r["observed"]["daily"],
                sources=r["sources"],
            )
            for r in rows
            if r["event"]
        ],
    )
    write_state(output / "result.json", result)
    write_state(output / "source-files-sha256.json", inputs)
    write_state(output / "manifest.json", {p.name: _sha256(p) for p in output.iterdir() if p.is_file()})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    print(json.dumps(run_audit(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
