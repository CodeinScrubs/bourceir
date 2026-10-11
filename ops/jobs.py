# -*- coding: utf-8 -*-
"""
اجرای کارهای طولانی در پس‌زمینه (برای دکمه‌های داشبورد)

داشبورد هر کار (به‌روزرسانی داده، پایش زنده، ...) را به‌صورت یک فرآیند جدا از brs.py اجرا می‌کند،
تا با بستن یا رفرش صفحه مرورگر متوقف نشود. وضعیت هر کار در data/jobs/<نام>.json ثبت می‌شود.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from config import settings
from core.timeutils import now_tehran_iso

JOBS_DIR = settings.DATA_DIR / "jobs"
BRS_SCRIPT = settings.BASE_DIR / "brs.py"


def _state_file(name: str) -> Path:
    return JOBS_DIR / f"{name}.json"


def _read_state(name: str) -> Optional[Dict[str, Any]]:
    path = _state_file(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _is_alive(pid: Optional[int]) -> bool:
    if not pid or not psutil.pid_exists(pid):
        return False
    try:
        proc = psutil.Process(pid)
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE and "brs.py" in " ".join(proc.cmdline())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def job_status(name: str, tail_lines: int = 15) -> Dict[str, Any]:
    state = _read_state(name) or {}
    log_path = Path(state["log"]) if state.get("log") else None
    tail: List[str] = []
    if log_path and log_path.exists():
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-tail_lines:]
    return {
        "name": name,
        "running": _is_alive(state.get("pid")),
        "started_at": state.get("started_at"),
        "args": state.get("args", []),
        "log_tail": tail,
    }


def start_job(name: str, args: List[str]) -> bool:
    """اجرای brs.py با آرگومان‌های داده‌شده در پس‌زمینه. اگر همین کار در حال اجرا باشد False."""
    if job_status(name)["running"]:
        return False
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOG_DIR / f"job-{name}.log"
    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    with open(log_path, "w", encoding="utf-8") as log_file:
        proc = subprocess.Popen(
            [sys.executable, str(BRS_SCRIPT), *args],
            cwd=str(settings.BASE_DIR), stdout=log_file, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=flags,
        )
    _state_file(name).write_text(json.dumps({
        "pid": proc.pid, "args": args, "started_at": now_tehran_iso(), "log": str(log_path),
    }, ensure_ascii=False), encoding="utf-8")
    return True


def stop_job(name: str) -> bool:
    state = _read_state(name) or {}
    pid = state.get("pid")
    if not _is_alive(pid):
        return False
    proc = psutil.Process(pid)
    for child in proc.children(recursive=True):
        child.terminate()
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except psutil.TimeoutExpired:
        proc.kill()
    return True
