"""Bounded, read-only clock and passive-recorder observations before an opening.

This program does not click, submit, simulate orders, modify clocks, or attach to
the browser. The listed NTP sources are diagnostic references, not the exchange.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import os
import re
import stat
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

WORKSPACE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKSPACE))
from core.timeutils import TEHRAN_TZ, now_tehran  # noqa: E402

SOURCES = (("ntp_ref_1", "162.159.200.1"), ("time_ir_experimental", "ntp.time.ir"))
ROWS = re.compile(r"^\s*\d+,\s*\d+,\s*(\d+),\s*([+-]\d+\.\d+),\s*([+-]\d+\.\d+)\s*$")
SESSION_NAME = re.compile(r"^session-[A-Za-z0-9-]+$")
ISIN = re.compile(r"^[A-Z0-9]{12}$")
W32TM = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "w32tm.exe")


def parse_samples(output: str) -> list[dict[str, Any]]:
    """Parse only the documented numeric /rdtsc rows, never success from exit code alone."""
    samples = []
    for line in output.splitlines():
        m = ROWS.match(line)
        if not m:
            continue
        try:
            ticks = int(m.group(1))
            delay_ms, offset_ms = float(m.group(2)) * 1000, float(m.group(3)) * 1000
            received = datetime(1601, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=ticks // 10)
            if not math.isfinite(delay_ms) or not math.isfinite(offset_ms) or not 0 <= delay_ms <= 10_000:
                continue
            samples.append({"received_utc": received.isoformat(), "roundtrip_ms": delay_ms, "offset_ms": offset_ms})
        except (OverflowError, ValueError):
            continue
    return samples


def _run(args: list[str], timeout: float) -> dict[str, Any]:
    started = now_tehran()
    mark = time.monotonic()
    try:
        result = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"started_at": started.isoformat(), "elapsed_ms": (time.monotonic() - mark) * 1000,
                "exit_code": result.returncode, "output": (result.stdout + result.stderr)[:12000], "error": None}
    except subprocess.TimeoutExpired:
        return {"started_at": started.isoformat(), "elapsed_ms": (time.monotonic() - mark) * 1000,
                "exit_code": None, "output": "", "error": "TIMEOUT"}
    except OSError:
        return {"started_at": started.isoformat(), "elapsed_ms": (time.monotonic() - mark) * 1000,
                "exit_code": None, "output": "", "error": "UNAVAILABLE"}


def probe_ntp(name: str, server: str) -> dict[str, Any]:
    result = _run([W32TM, "/stripchart", "/computer:" + server, "/samples:2", "/period:1", "/rdtsc"], 4.5)
    samples = parse_samples(result["output"])
    return {"name": name, "server": server, **result, "samples": samples,
            "status": "SAMPLES_OBSERVED" if result["exit_code"] == 0 and len(samples) >= 2 else "UNKNOWN"}


def clock_summary(probes: list[dict[str, Any]], observed_at: datetime) -> dict[str, Any]:
    flags = []
    medians = []
    if {(p.get("name"), p.get("server")) for p in probes} != set(SOURCES):
        flags.append("REFERENCE_SET_INCOMPLETE")
    for probe in probes:
        samples = probe.get("samples", [])
        if probe.get("status") != "SAMPLES_OBSERVED" or len(samples) < 2:
            flags.append("REFERENCE_UNAVAILABLE")
            continue
        try:
            ages = [(observed_at.astimezone(timezone.utc) - datetime.fromisoformat(s["received_utc"])).total_seconds()
                    for s in samples]
            if any(age < -0.5 or age > 8 for age in ages):
                flags.append("SAMPLE_TIME_INVALID_OR_STALE")
            median = statistics.median(float(s["offset_ms"]) for s in samples)
            if not math.isfinite(median):
                raise ValueError("invalid median")
            medians.append(median)
            if abs(median) > 100:
                flags.append("OBSERVED_OFFSET_OVER_100MS")
        except (KeyError, ValueError, TypeError, OverflowError):
            flags.append("SAMPLE_INVALID")
    if len(medians) == 2 and abs(medians[0] - medians[1]) > 100:
        flags.append("REFERENCE_MEDIANS_DIFFER_OVER_100MS")
    return {"status": "CHECK" if flags else "SAMPLES_OBSERVED", "flags": sorted(set(flags)),
            "diagnostic_threshold_ms": 100, "exchange_clock_verified": False,
            "note": "The 100 ms threshold is a warning heuristic, not an error bound, exchange-clock match, or click offset."}


def parse_utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def recorder_status(root: Path, isin: str, observed_at: datetime, *, synthetic: bool = False) -> dict[str, Any]:
    """Observe recent local collector status. Never claim the selected broker tab is verified."""
    if not ISIN.fullmatch(isin):
        raise ValueError("Invalid ISIN")
    now_utc = observed_at.astimezone(timezone.utc)
    candidates = []
    try:
        folders = list(root.iterdir())
    except OSError:
        return {"status": "UNKNOWN", "reason": "RECORDER_DIRECTORY_UNAVAILABLE"}
    for folder in folders:
        if not SESSION_NAME.fullmatch(folder.name) or folder.is_symlink() or not folder.is_dir():
            continue
        try:
            manifest_file, health_file = folder / "manifest.json", folder / "health.json"
            if manifest_file.is_symlink() or health_file.is_symlink():
                continue
            manifest = json.loads(manifest_file.read_text(encoding="utf-8-sig"))
            health = json.loads(health_file.read_text(encoding="utf-8-sig"))
            if (manifest.get("isin") != isin or manifest.get("mode") != "in-app-browser-cua-passive"
                    or manifest.get("synthetic") is not synthetic):
                continue
            updated, started = parse_utc(health.get("updated_utc")), parse_utc(manifest.get("started_utc"))
            if not updated or not started:
                continue
            age, start_age = (now_utc - updated).total_seconds(), (now_utc - started).total_seconds()
            if -0.5 <= age <= 4 and 0 <= start_age <= 120:
                candidates.append((folder.name, manifest, health, age))
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            continue
    if len(candidates) != 1:
        return {"status": "UNKNOWN", "reason": "NO_FRESH_MATCH" if not candidates else "MULTIPLE_FRESH_MATCHES"}
    name, manifest, health, age = candidates[0]
    healthy = (health.get("health") == "RECORDING" and type(health.get("gaps")) is int and health["gaps"] == 0
               and type(health.get("last_durable_ingest_sequence")) is int
               and manifest.get("health") not in ("SAVED", "DEGRADED")
               and health["last_durable_ingest_sequence"] > 0)
    return {"status": "RECORDER_HEARTBEAT_OBSERVED" if healthy else "DEGRADED_OR_STOPPED",
            "session": name, "recorder_version": manifest.get("version"), "heartbeat_age_ms": age * 1000,
            "reported_gaps": health.get("gaps"), "selected_tab_verified": False,
            "note": "Local collector health only; form, broker, order acceptance and exchange are not verified."}


def perform_check(isin: str, recorder_root: Path, *, target: datetime | None = None,
                  scheduled_at: datetime | None = None, synthetic: bool = False) -> dict[str, Any]:
    start = now_tehran()
    monotonic_start = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as executor:
        jobs = [executor.submit(probe_ntp, name, server) for name, server in SOURCES]
        probes = [job.result() for job in jobs]
    finished = now_tehran()
    wall_elapsed = (finished - start).total_seconds()
    elapsed = time.monotonic() - monotonic_start
    clock = clock_summary(probes, finished)
    if abs(wall_elapsed - elapsed) > 0.2:
        clock["status"] = "CHECK"
        clock["flags"].append("LOCAL_CLOCK_STEP_DURING_CHECK")
    start_late_ms = ((start - scheduled_at).total_seconds() * 1000) if scheduled_at else None
    before_target = (finished < target) if target else None
    return {"schema_version": 1, "synthetic": synthetic, "started_tehran": start.isoformat(),
            "finished_tehran": finished.isoformat(), "elapsed_ms": elapsed * 1000,
            "target_tehran": target.isoformat() if target else None,
            "scheduled_sample_tehran": scheduled_at.isoformat() if scheduled_at else None,
            "sample_start_lateness_ms": start_late_ms, "finished_before_target": before_target,
            "schedule_status": ("LATE_OR_AFTER_TARGET" if target and
                                (before_target is False or (start_late_ms is not None and start_late_ms > 1500))
                                else "OBSERVED" if target else "IMMEDIATE_DIAGNOSTIC"),
            "clock": clock, "ntp": probes,
            "recorder": recorder_status(recorder_root, isin, finished, synthetic=synthetic),
            "exchange_clock_verified": False, "order_latency_known": False, "automatic_click_offset_ms": None,
            "order_actions": 0, "clock_settings_changed": False,
            "guidance": "Manual action only at the announced opening after checking the market and form; these measurements do not determine order timing or rank.",
            "scope": "This file reports observations through finished_tehran only; later clock steps, market status or connection changes are not captured."}


def save_report(report: dict[str, Any], output_root: Path) -> Path:
    allowed = WORKSPACE / ("tests/.timing-output" if report.get("synthetic") is True else "data/private/farabi-timing")
    if output_root == allowed or not output_root.is_relative_to(allowed):
        raise ValueError("Output is outside the approved private/test root")
    # Refuse Windows junctions as well as symbolic links in the output path.
    cursor = output_root
    while cursor != WORKSPACE:
        try:
            info = cursor.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                raise ValueError("Reparse point in output path")
        except FileNotFoundError:
            pass
        cursor = cursor.parent
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / ("readiness-" + now_tehran().astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    payload = (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    with path.with_suffix(".sha256.txt").open("x", encoding="ascii") as stream:
        stream.write(hashlib.sha256(payload).hexdigest() + "  " + path.name + "\n")
    return path


def summarize(report: dict[str, Any], *, recorder: dict[str, Any] | None = None,
              age_seconds: float | None = None) -> str:
    observed_at = datetime.fromisoformat(report["finished_tehran"]).strftime("%H:%M:%S")
    age = "" if age_seconds is None else f"; age {max(0, age_seconds):.1f}s"
    parts = [f"Clock samples: {report['clock']['status']} (as of {observed_at}{age})",
             f"Clock warnings: {', '.join(report['clock']['flags']) or 'none observed'}"]
    for row in report["ntp"]:
        offsets = [round(s["offset_ms"], 1) for s in row["samples"]]
        delays = [round(s["roundtrip_ms"], 1) for s in row["samples"]]
        parts.append(f"{row['name']}: offset {offsets} ms; RTT {delays} ms")
    if age_seconds is not None and (age_seconds < -0.5 or age_seconds > 12):
        parts.append("CLOCK SNAPSHOT STALE OR CLOCK CHANGED")
    parts += [f"Recorder: {(recorder or report['recorder'])['status']}", f"Schedule: {report['schedule_status']}",
              "Market/form/order latency/exchange time: NOT VERIFIED",
              "NO automatic click; NO calculated early offset."]
    return "\n".join(parts)


def validate_target(value: str, now: datetime) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=TEHRAN_TZ)
    except ValueError as exc:
        raise ValueError("Use an explicit Tehran date/time YYYY-MM-DDTHH:MM:SS") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S") != value:
        raise ValueError("Use an explicit Tehran date/time YYYY-MM-DDTHH:MM:SS")
    delta = (parsed - now).total_seconds()
    if not 12 <= delta <= 1800:
        raise ValueError("Start 12 seconds to 30 minutes before the explicit target; past times never roll over")
    return parsed


def run_window(target: datetime, isin: str, recorder_root: Path, output_root: Path, synthetic: bool) -> None:
    import tkinter as tk

    root = tk.Tk()
    # Start-Process hides the console. Explicitly show this diagnostic window
    # after initialization so it is not inherited as a hidden first window.
    root.withdraw()
    root.after(250, root.deiconify)
    root.title("Pre-open timing diagnostics (no trading)")
    root.geometry("700x350+30+30")
    label = tk.Label(root, font=("Segoe UI", 13), justify="left", anchor="w")
    label.pack(fill="both", expand=True, padx=12, pady=8)
    detail = tk.Label(root, font=("Segoe UI", 10), justify="left", anchor="w", wraplength=660)
    detail.pack(fill="x", padx=12, pady=8)
    detail.config(text="No orders or clicks. Recorder status is only a local heartbeat. "
                       "Verify market, symbol and form yourself. Exchange time is unknown.")
    planned = target - timedelta(seconds=10)
    start_wall, start_mono = now_tehran(), time.monotonic()
    state: dict[str, Any] = {"started": False, "report": None, "error": None, "clock_step": False,
                             "live_recorder": {"status": "UNKNOWN"}, "last_recorder_poll": 0.0}

    def worker() -> None:
        try:
            report = perform_check(isin, recorder_root, target=target, scheduled_at=planned, synthetic=synthetic)
            if state["clock_step"]:
                report["clock"]["status"] = "CHECK"
                report["clock"]["flags"].append("LOCAL_CLOCK_STEP_BEFORE_CHECK")
            report["report_file"] = str(save_report(report, output_root))
            state["report"] = report
        except Exception as exc:  # show failure rather than an apparently healthy empty panel
            state["error"] = type(exc).__name__

    def refresh() -> None:
        now = now_tehran()
        if abs((now - start_wall).total_seconds() - (time.monotonic() - start_mono)) > 0.2:
            state["clock_step"] = True
        if not state["started"] and now >= planned:
            state["started"] = True
            threading.Thread(target=worker, daemon=True).start()
        mono = time.monotonic()
        if mono - state["last_recorder_poll"] >= 1:
            state["last_recorder_poll"] = mono
            try:
                state["live_recorder"] = recorder_status(recorder_root, isin, now, synthetic=synthetic)
            except (OSError, ValueError):
                state["live_recorder"] = {"status": "UNKNOWN"}
        prefix = f"LOCAL Tehran {now.strftime('%H:%M:%S.%f')[:-3]}\nTarget {target.strftime('%Y-%m-%d %H:%M:%S')}"
        if state["error"]:
            body = "CHECK FAILED: " + state["error"]
        elif state["report"]:
            age = (now - datetime.fromisoformat(state["report"]["finished_tehran"])).total_seconds()
            body = summarize(state["report"], recorder=state["live_recorder"], age_seconds=age)
        elif state["started"]:
            body = "MEASURING (no result yet)"
        else:
            body = "Waiting to sample at target minus 10 seconds."
        if state["clock_step"]:
            body += "\nCLOCK STEP DETECTED — time is unverified."
        label.config(text=prefix + "\n\n" + body)
        root.after(50, refresh)

    refresh()
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only, bounded clock and recorder observations; never trades.")
    parser.add_argument("--isin", required=True)
    parser.add_argument("--target", help="Tehran local date/time YYYY-MM-DDTHH:MM:SS")
    parser.add_argument("--measure-now", action="store_true", help="run an immediate diagnostic and exit")
    parser.add_argument("--synthetic", action="store_true", help="use synthetic recorder/output paths in tests")
    parser.add_argument("--once", action="store_true", help="run a scheduled headless diagnostic and exit")
    args = parser.parse_args()
    if not ISIN.fullmatch(args.isin):
        parser.error("--isin must be 12 uppercase letters or digits")
    if args.measure_now == bool(args.target) or (args.measure_now and args.once):
        parser.error("choose exactly one of --measure-now or --target; --once requires --target")
    recorder_root = WORKSPACE / ("tests/.timing-output" if args.synthetic else "data/private/farabi-timing")
    output_root = WORKSPACE / ("tests/.timing-output/morning-check" if args.synthetic else "data/private/farabi-timing/morning-check")
    if args.measure_now:
        report = perform_check(args.isin, recorder_root, synthetic=args.synthetic)
        file = save_report(report, output_root)
        print(summarize(report) + "\nReport: " + str(file), flush=True)
        return
    try:
        target = validate_target(args.target, now_tehran())
    except ValueError as exc:
        parser.error(str(exc))
    planned = target - timedelta(seconds=10)
    if args.once:
        start_wall, start_mono = now_tehran(), time.monotonic()
        changed = False
        while now_tehran() < planned:
            time.sleep(min(0.1, max(0.001, (planned - now_tehran()).total_seconds())))
            changed |= abs((now_tehran() - start_wall).total_seconds() - (time.monotonic() - start_mono)) > 0.2
        report = perform_check(args.isin, recorder_root, target=target, scheduled_at=planned, synthetic=args.synthetic)
        if changed:
            report["clock"]["status"] = "CHECK"
            report["clock"]["flags"].append("LOCAL_CLOCK_STEP_BEFORE_CHECK")
        file = save_report(report, output_root)
        print(summarize(report) + "\nReport: " + str(file), flush=True)
    else:
        run_window(target, args.isin, recorder_root, output_root, args.synthetic)


if __name__ == "__main__":
    main()
