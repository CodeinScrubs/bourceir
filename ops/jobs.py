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
_OWNED_PROCESSES: Dict[str, subprocess.Popen] = {}


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


def _is_alive(pid: Optional[int], created_at: Optional[float] = None) -> bool:
    if not pid or not psutil.pid_exists(pid):
        return False
    try:
        proc = psutil.Process(pid)
        command = proc.cmdline()
        return (proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
                and len(command) > 1 and Path(command[1]).resolve() == BRS_SCRIPT.resolve()
                and created_at is not None and abs(proc.create_time() - created_at) < 0.01)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def job_status(name: str, tail_lines: int = 15) -> Dict[str, Any]:
    state = _read_state(name) or {}
    log_path = Path(state["log"]) if state.get("log") else None
    tail: List[str] = []
    if log_path and log_path.exists():
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - 65536))
            lines = stream.read().decode("utf-8", errors="replace").splitlines()
        tail = lines[-tail_lines:]
    owned = _OWNED_PROCESSES.get(name)
    # handle خود فرایند قابل اتکاتر از جستجوی مجدد PID است (به‌ویژه در محیط‌های دارای PID namespace).
    running = owned.poll() is None if owned is not None else _is_alive(state.get("pid"), state.get("process_created_at"))
    return {
        "name": name,
        "running": running,
        "returncode": owned.returncode if owned is not None else None,
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
    _OWNED_PROCESSES[name] = proc
    try:
        created_at = psutil.Process(proc.pid).create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        created_at = None
    _state_file(name).write_text(json.dumps({
        "pid": proc.pid, "args": args, "started_at": now_tehran_iso(), "log": str(log_path),
        "process_created_at": created_at,
    }, ensure_ascii=False), encoding="utf-8")
    return True


def stop_job(name: str) -> bool:
    state = _read_state(name) or {}
    pid = state.get("pid")
    owned = _OWNED_PROCESSES.get(name)
    if owned is not None:
        if owned.poll() is not None:
            return False
        # در محیطی که psutil فضای PID دیگری می‌بیند، فقط handle متعلق به خودمان متوقف می‌شود.
        if not _is_alive(pid, state.get("process_created_at")):
            owned.terminate()
            try:
                owned.wait(timeout=10)
            except subprocess.TimeoutExpired:
                owned.kill()
                owned.wait(timeout=5)
            return True
    if not _is_alive(pid, state.get("process_created_at")):
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
