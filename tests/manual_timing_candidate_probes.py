"""Reproduce reviewed candidate defects with fake APIs; never send desktop input.

The source snapshots are ignored local audit inputs, not dependencies of BRS.
Only explicitly selected, reviewed definitions are compiled. Module startup,
GUI code, native DLL loading, and networking are never executed.
"""
from __future__ import annotations

import ast
import copy
import ctypes
import hashlib
import json
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.timeutils import now_tehran

SNAPSHOTS = ROOT / "tests/.timing-output/clicker-comparison-20261024"


def definitions(relative: str, names: list[str], scope: dict) -> dict:
    source = SNAPSHOTS / relative
    inventory = json.loads((SNAPSHOTS / "inventory.json").read_text(encoding="utf-8"))
    expected = next(entry["sha256"] for entry in inventory["files"] if entry["name"] == relative)
    content = source.read_bytes()
    if hashlib.sha256(content).hexdigest() != expected:
        raise ValueError("Source changed after inspection; review it before executing definitions")
    tree = ast.parse(content.decode("utf-8-sig"))
    chosen = []
    for name in names:
        node = copy.deepcopy(next(n for n in ast.walk(tree) if isinstance(n, (ast.ClassDef, ast.FunctionDef)) and n.name == name))
        for child in ast.walk(node):
            if isinstance(child, ast.FunctionDef):
                child.returns = None
                child.decorator_list = []
                for arg in [*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs]:
                    arg.annotation = None
        chosen.append(node)
    module = ast.fix_missing_locations(ast.Module(body=chosen, type_ignores=[]))
    exec(compile(module, str(source), "exec"), scope)
    return scope


def require(value: bool, message: str) -> None:
    if not value:
        raise AssertionError(message)


def ai1_cancel() -> dict:
    event = threading.Event()
    event.set()
    ns = definitions("ai1/precision_clicker.py", ["wait_until"], {"now_ns": lambda: 100, "SPIN_NS": 80_000_000, "time": time})
    proceeded = ns["wait_until"](100, event)
    require(proceeded is True, "Expected documented cancellation race was not reproduced")
    return {"deadline_due": True, "cancel_already_set": True, "wait_returned_proceed": proceeded}


def ai1_send_failure() -> dict:
    calls = []
    ns = definitions("ai1/precision_clicker.py", ["do_click"], {"u32": NS(SendInput=lambda *args: calls.append(args) or 0), "ctypes": NS(sizeof=lambda _: 40), "INPUT": object})
    result = ns["do_click"](object())
    require(len(calls) == 1 and result is None, "Expected ignored zero return was not reproduced")
    return {"native_return_count": 0, "raised": False, "return_value": result}


def ai2_partial_replay() -> dict:
    fallback = []
    user32 = NS(SendInput=lambda *_: 2, mouse_event=lambda flags, *_: fallback.append(flags))
    ns = definitions("ai2/schedule_clicker/core/mouse.py", ["_send_atomic_inputs"], {"sys": NS(platform="win32"), "ctypes": NS(windll=NS(user32=user32), byref=lambda value: value, sizeof=lambda _: 40), "INPUT": object})
    packets = [NS(union=NS(mi=NS(dwFlags=flags, dx=0, dy=0, mouseData=0, dwExtraInfo=0))) for flags in [2, 4, 2, 4]]
    result = ns["_send_atomic_inputs"](packets, 4)
    require(result is True and fallback == [2, 4, 2, 4], "Expected partial-batch replay was not reproduced")
    return {"requested_clicks": 2, "native_queued_prefix_flags": [2, 4], "fallback_flags": fallback, "total_simulated_down_events": 3, "reported_success": result}


def struct_scope() -> dict:
    from ctypes import wintypes
    return {"ctypes": ctypes, "wintypes": wintypes, "ULONG_PTR": ctypes.c_size_t}


def ai3_gap_coordinate() -> dict:
    path = "ai3/AutoClicker_MS_v3.py"
    ns = definitions(path, ["POINT", "RECT", "MOUSEINPUT", "KEYBDINPUT", "HARDWAREINPUT", "INPUT_UNION", "INPUT", "SafetyAbort", "DeadlineMissed", "TargetNotReached"], struct_scope())
    target = datetime(2030, 1, 2, tzinfo=timezone.utc)
    sent = []
    display = ((0, 0, 4480, 1080), ((0, 0, 1920, 1080), (2560, 0, 4480, 1080)))
    ns.update({"display_signature": lambda: display, "user32": NS(GetAsyncKeyState=lambda _: 0, SendInput=lambda count, *_: sent.append(count) or count), "datetime": NS(now=lambda: target), "time": time, "lateness_ms": lambda a, b: (a-b).total_seconds()*1000, "emergency_release_left_button": lambda: True, "logger": NS(error=lambda *_: None)})
    ns.update(dict(INPUT_MOUSE=0, MOUSEEVENTF_MOVE=1, MOUSEEVENTF_ABSOLUTE=0x8000, MOUSEEVENTF_VIRTUALDESK=0x4000, MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4, VK_LBUTTON=1, KEY_DOWN_MASK=0x8000, VK_SHIFT=16, VK_CONTROL=17, VK_MENU=18, VK_LWIN=91, VK_RWIN=92))
    ns["ctypes"] = NS(set_last_error=lambda _: None, get_last_error=lambda: 0, sizeof=ctypes.sizeof)
    definitions(path, ["send_left_click"], ns)
    result = ns["send_left_click"](2200, 100, display, target, 100)
    require(sent == [3] and result["sent"] == 3, "Expected gap-coordinate acceptance was not reproduced")
    return {"virtual_bounds": display[0], "monitor_bounds": display[1], "point_in_gap": [2200, 100], "simulated_input_batch_accepted": True}


def ai4_partial_report() -> dict:
    path = "ai4_claims/code-03.py"
    ns = definitions(path, ["POINT", "RECT", "MOUSEINPUT", "KEYBDINPUT", "HARDWAREINPUT", "_INPUTunion", "INPUT"], struct_scope())
    events, reports = [], []
    ticks = [0]

    def perf() -> int:
        ticks[0] += 1_000_000
        return ticks[0]

    def send(count, packets, _size):
        returned = 1
        events.append({"requested": count, "returned": returned, "queued_flags": [packets[i].mi.dwFlags for i in range(returned)]})
        return returned

    ns.update({"to_virtual_screen_coords": lambda *_: (0, 0), "prevent_system_sleep": lambda *_: None, "set_kernel_timer_resolution": lambda *_: None, "release_cursor_clip": lambda: None, "lock_cursor_at": lambda *_: None, "gc": NS(isenabled=lambda: True, disable=lambda: None, enable=lambda: None), "time": NS(perf_counter_ns=perf, sleep=lambda *_: None), "datetime": datetime, "kernel32": NS(GetCurrentThread=lambda: 1, SetThreadPriority=lambda *_: True), "user32": NS(SendInput=send, SetCursorPos=lambda *_: True, SetForegroundWindow=lambda *_: True, mouse_event=lambda *_: None)})
    ns.update(dict(INPUT_MOUSE=0, MOUSEEVENTF_MOVE=1, MOUSEEVENTF_ABSOLUTE=0x8000, MOUSEEVENTF_VIRTUALDESK=0x4000, MOUSEEVENTF_LEFTDOWN=2, MOUSEEVENTF_LEFTUP=4, MOUSEEVENTF_RIGHTDOWN=8, MOUSEEVENTF_RIGHTUP=16, MOUSEEVENTF_MIDDLEDOWN=32, MOUSEEVENTF_MIDDLEUP=64, THREAD_PRIORITY_TIME_CRITICAL=15, THREAD_PRIORITY_NORMAL=0))
    definitions(path, ["make_mouse_input", "_realtime_firing_worker"], ns)
    obj = NS(armed=True, ui_frozen=False, get_accurate_epoch=lambda: 0, disarm=reports.append, root=NS(after=lambda _, fn: fn()))
    cfg = dict(x=10, y=10, button="Left", hold_ns=0, mouseup_at_t0=False, effective_epoch=0, scheduled_epoch=0, focus_target=False, root_hwnd=0, clip_cursor=False, burst_count=1, burst_int_ns=0)
    ns["_realtime_firing_worker"](obj, cfg)
    require(len(reports) == 1 and "EXECUTED & VERIFIED" in reports[0] and events[-1]["requested"] == 2 and events[-1]["queued_flags"] == [2], "Expected partial result falsely verified was not reproduced")
    return {"input_calls": events, "reported_executed_and_verified": True, "release_after_partial_down": False}


def ai5_changed_window() -> dict:
    path = "ai5/precise_clicker (1).py"
    target = datetime(2030, 1, 2, tzinfo=timezone.utc)
    state = {"window": 1, "checks": [], "clicks": []}

    def window(*_):
        state["checks"].append(state["window"])
        return state["window"]

    def wait(*_args, **_kwargs):
        state["window"] = 2
        return target

    def click(*_args, **_kwargs):
        state["clicks"].append(state["window"])
        return target

    ns = definitions(path, ["run_scheduled_click"], {"IS_WINDOWS": True, "threading": threading, "get_virtual_screen": lambda: (0, 0, 1920, 1080), "validate_coordinates": lambda *_: None, "window_from_point": window, "wait_until": wait, "click_at": click, "drift_ms": lambda a, b: (a-b).total_seconds()*1000})
    ns["run_scheduled_click"](target, 10, 10, window_handle=1)
    require(state["checks"] == [1] and state["clicks"] == [2], "Expected pre-wait window check defect was not reproduced")
    return {"window_checks": state["checks"], "window_when_simulated_click_called": state["clicks"]}


def main() -> int:
    results = []
    for name, function in [("AI1 due deadline ignores already-set cancellation", ai1_cancel), ("AI1 zero SendInput count is ignored", ai1_send_failure), ("AI2 partial batch replays already queued events", ai2_partial_replay), ("AI3 gap coordinate passes final injection checks", ai3_gap_coordinate), ("AI4 partial button-down is reported verified", ai4_partial_report), ("AI5 changed window is not checked after waiting", ai5_changed_window)]:
        try:
            results.append({"name": name, "reproduced": True, "observation": function()})
        except Exception as error:
            results.append({"name": name, "reproduced": False, "error": f"{type(error).__name__}: {error}"})
    report = {"recorded_at_tehran": now_tehran().isoformat(), "scope": "Reviewed source definitions with fake OS/input/clock; no native input, UI, network, power or priority changes", "all_reproduced": all(r["reproduced"] for r in results), "cases": results}
    (SNAPSHOTS / "probe-results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["all_reproduced"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
