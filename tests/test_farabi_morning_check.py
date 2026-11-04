"""Synthetic offline evidence for the read-only pre-open diagnostics."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scripts import farabi_morning_check as check

ISIN = "IRO3SIMC0001"
NOW = datetime(2026, 10, 31, 5, 14, 54, tzinfo=timezone.utc)


def sample(offset: float = 0, seconds_ago: float = 1) -> dict:
    return {"received_utc": (NOW - timedelta(seconds=seconds_ago)).isoformat(),
            "roundtrip_ms": 40, "offset_ms": offset}


def valid_probes(a: float = 1, b: float = 2) -> list[dict]:
    return [{"name": name, "server": server, "status": "SAMPLES_OBSERVED",
             "samples": [sample(value), sample(value)]}
            for (name, server), value in zip(check.SOURCES, (a, b), strict=True)]


def session(root: Path, name: str, *, isin: str = ISIN, age: float = 1,
            health: str = "RECORDING", gaps: int = 0) -> None:
    folder = root / name
    folder.mkdir()
    (folder / "manifest.json").write_text(json.dumps({"isin": isin, "version": "1.0.3", "synthetic": False,
        "mode": "in-app-browser-cua-passive", "started_utc": (NOW - timedelta(seconds=15)).isoformat()}))
    (folder / "health.json").write_text(json.dumps({"health": health, "gaps": gaps,
        "last_durable_ingest_sequence": 8, "updated_utc": (NOW - timedelta(seconds=age)).isoformat()}))


def test_parses_only_valid_rdtsc_rows_and_preserves_offset_sign():
    raw = """error: 0x80072741
1, 2, 134376401956528428, +00.0543529, -00.0011327
1, 2, 134376401967105645, -00.0783548, +00.0064406
1, 2, 999999999999999999999999999999999999, +00.001, +00.001
"""
    parsed = check.parse_samples(raw)
    assert len(parsed) == 1
    assert parsed[0]["roundtrip_ms"] == pytest.approx(54.3529)
    assert parsed[0]["offset_ms"] == pytest.approx(-1.1327)


def test_missing_bad_stale_or_disagreeing_sources_never_become_observed():
    assert check.clock_summary(valid_probes(), NOW)["status"] == "SAMPLES_OBSERVED"
    probes = valid_probes()
    probes[1]["status"] = "UNKNOWN"
    assert check.clock_summary(probes, NOW)["status"] == "CHECK"
    probes = valid_probes()
    probes[1]["samples"] = [sample(1, 10), sample(1, 10)]
    assert "SAMPLE_TIME_INVALID_OR_STALE" in check.clock_summary(probes, NOW)["flags"]
    assert "REFERENCE_MEDIANS_DIFFER_OVER_100MS" in check.clock_summary(valid_probes(-60, 60), NOW)["flags"]
    assert "OBSERVED_OFFSET_OVER_100MS" in check.clock_summary(valid_probes(101, 101), NOW)["flags"]
    probes = valid_probes()
    probes[1]["server"] = "different.example"
    assert "REFERENCE_SET_INCOMPLETE" in check.clock_summary(probes, NOW)["flags"]


def test_collector_freshness_is_local_only_and_missing_or_multiple_is_unknown(tmp_path):
    assert check.recorder_status(tmp_path, ISIN, NOW)["status"] == "UNKNOWN"
    session(tmp_path, "session-2026-10-31-test1")
    result = check.recorder_status(tmp_path, ISIN, NOW)
    assert result["status"] == "RECORDER_HEARTBEAT_OBSERVED"
    assert result["selected_tab_verified"] is False
    session(tmp_path, "session-2026-10-31-test2")
    assert check.recorder_status(tmp_path, ISIN, NOW)["reason"] == "MULTIPLE_FRESH_MATCHES"


def test_collector_stale_wrong_symbol_or_gap_never_claims_healthy(tmp_path):
    session(tmp_path, "session-2026-10-31-stale", age=5)
    session(tmp_path, "session-2026-10-31-wrong", isin="IRO3FSTZ0001")
    assert check.recorder_status(tmp_path, ISIN, NOW)["status"] == "UNKNOWN"
    session(tmp_path, "session-2026-10-31-gap", gaps=1)
    assert check.recorder_status(tmp_path, ISIN, NOW)["status"] == "DEGRADED_OR_STOPPED"


def test_finalized_or_malformed_health_never_claims_recording(tmp_path):
    session(tmp_path, "session-2026-10-31-saved")
    folder = tmp_path / "session-2026-10-31-saved"
    manifest = json.loads((folder / "manifest.json").read_text())
    manifest["health"] = "SAVED"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    assert check.recorder_status(tmp_path, ISIN, NOW)["status"] == "DEGRADED_OR_STOPPED"
    manifest["health"] = "STARTING"
    (folder / "manifest.json").write_text(json.dumps(manifest))
    health = json.loads((folder / "health.json").read_text())
    health["gaps"] = False
    (folder / "health.json").write_text(json.dumps(health))
    assert check.recorder_status(tmp_path, ISIN, NOW)["status"] == "DEGRADED_OR_STOPPED"


def test_synthetic_and_real_recorders_are_not_mixed(tmp_path):
    session(tmp_path, "session-2026-10-31-real")
    assert check.recorder_status(tmp_path, ISIN, NOW, synthetic=True)["status"] == "UNKNOWN"


def test_invalid_and_past_targets_do_not_roll_into_a_later_day():
    now = NOW.astimezone(check.TEHRAN_TZ)
    target = (now + timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%S")
    assert check.validate_target(target, now) > now
    for candidate in ["08:45", "2026-10-31T08:45:00", "2026-11-01T08:45:00", "2026-99-01T08:45:00"]:
        with pytest.raises(ValueError):
            check.validate_target(candidate, now)


def test_full_check_keeps_exchange_and_order_timing_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "now_tehran", lambda: NOW.astimezone(check.TEHRAN_TZ))
    monkeypatch.setattr(check, "probe_ntp", lambda name, server:
                        next(row for row in valid_probes() if row["name"] == name))
    session(tmp_path, "session-2026-10-31-test1")
    target = (NOW + timedelta(seconds=6)).astimezone(check.TEHRAN_TZ)
    report = check.perform_check(ISIN, tmp_path, target=target,
                                 scheduled_at=target - timedelta(seconds=10), synthetic=False)
    assert report["schedule_status"] == "LATE_OR_AFTER_TARGET"
    assert report["order_actions"] == 0
    assert report["automatic_click_offset_ms"] is None
    assert report["exchange_clock_verified"] is False
    assert report["order_latency_known"] is False
    assert report["recorder"]["status"] == "RECORDER_HEARTBEAT_OBSERVED"


def test_on_time_check_and_an_expired_target_are_distinguished(monkeypatch, tmp_path):
    monkeypatch.setattr(check, "now_tehran", lambda: NOW.astimezone(check.TEHRAN_TZ))
    monkeypatch.setattr(check, "probe_ntp", lambda name, server:
                        next(row for row in valid_probes() if row["name"] == name))
    local = NOW.astimezone(check.TEHRAN_TZ)
    report = check.perform_check(ISIN, tmp_path, target=local + timedelta(seconds=10), scheduled_at=local)
    assert report["schedule_status"] == "OBSERVED"
    report = check.perform_check(ISIN, tmp_path, target=local - timedelta(seconds=1), scheduled_at=local)
    assert report["schedule_status"] == "LATE_OR_AFTER_TARGET"


def test_exit_code_zero_without_numeric_rows_is_not_a_successful_sample(monkeypatch):
    monkeypatch.setattr(check, "_run", lambda *_args: {"started_at": NOW.isoformat(), "elapsed_ms": 1,
                        "exit_code": 0, "output": "error: 0x80072741", "error": None})
    assert check.probe_ntp(*check.SOURCES[0])["status"] == "UNKNOWN"


def test_display_marks_old_clock_results_and_uses_live_recorder_status():
    report = {"finished_tehran": NOW.astimezone(check.TEHRAN_TZ).isoformat(),
              "clock": {"status": "SAMPLES_OBSERVED", "flags": []}, "ntp": valid_probes(),
              "recorder": {"status": "RECORDER_HEARTBEAT_OBSERVED"}, "schedule_status": "OBSERVED"}
    text = check.summarize(report, recorder={"status": "UNKNOWN"}, age_seconds=13)
    assert "CLOCK SNAPSHOT STALE" in text
    assert "Recorder: UNKNOWN" in text
    assert "as of 08:44:54" in text


def test_report_rejects_unapproved_output_root(tmp_path):
    with pytest.raises(ValueError):
        check.save_report({"synthetic": True}, tmp_path)
