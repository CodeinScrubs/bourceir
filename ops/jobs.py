# -*- coding: utf-8 -*-
"""
اجرای کارهای طولانی در پس‌زمینه (برای دکمه‌های داشبورد)

داشبورد هر کار (به‌روزرسانی داده، پایش زنده، ...) را به‌صورت یک فرآیند جدا از brs.py اجرا می‌کند،
تا با بستن یا رفرش صفحه مرورگر متوقف نشود. وضعیت هر کار در data/jobs/<نام>.json ثبت می‌شود.
"""

import json
import math
import os
import re
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional

import psutil

from config import settings
from core.timeutils import now_tehran_iso

JOBS_DIR = settings.DATA_DIR / "jobs"
BRS_SCRIPT = settings.BASE_DIR / "brs.py"
_OWNED_PROCESSES: Dict[str, subprocess.Popen] = {}


def _state_file(name: str) -> Path:
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', name):
        raise ValueError('نام وظیفه نامعتبر است')
    return JOBS_DIR / f"{name}.json"


def _read_state(name: str) -> Optional[Dict[str, Any]]:
    path = _state_file(name)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(state, dict) or type(state.get("pid")) is not int or state["pid"] <= 0:
        raise ValueError("شناسهٔ فرایند وظیفه نامعتبر است")
    created = state.get("created_at")
    if created is not None and (type(created) not in (int, float) or not math.isfinite(created) or created <= 0):
        raise ValueError("زمان ایجاد فرایند وظیفه نامعتبر است")
    args = state.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("آرگومان‌های وظیفه نامعتبرند")
    if state.get("started_at") is not None and not isinstance(state["started_at"], str):
        raise ValueError("زمان شروع وظیفه نامعتبر است")
    return state


def _job_process(state: Dict[str, Any]) -> Optional[psutil.Process]:
    """Return the checked process object so stopping it requires no second PID lookup."""
    if not state:
        return None
    try:
        proc = psutil.Process(state["pid"])
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return None
        created = state.get("created_at")
        if created is not None and abs(proc.create_time() - created) >= 0.01:
            return None
        command = proc.cmdline()
        if len(command) < 2 or Path(command[1]).resolve() != BRS_SCRIPT.resolve():
            return None
        if "args" in state and command[2:] != state["args"]:
            return None
        if created is None:
            raise ValueError("زمان ایجاد فرایند فعال ثبت نشده است")
        return proc
    except psutil.NoSuchProcess:
        return None


_IDENTITY_ERRORS = (OSError, ValueError, OverflowError, RuntimeError, psutil.AccessDenied)


def job_status(name: str, tail_lines: int = 15) -> Dict[str, Any]:
    owned = _OWNED_PROCESSES.get(name)
    owned_running = owned is not None and owned.poll() is None
    running = True if owned_running else None
    state = {}
    try:
        state = _read_state(name) or {}
        if owned_running:
            if state.get("pid") != owned.pid:
                state = {}
        else:
            running = _job_process(state) is not None
    except _IDENTITY_ERRORS:
        pass
    tail: List[str] = []
    if state and tail_lines > 0:
        log_path = settings.LOG_DIR / f"job-{name}.log"
        try:
            with log_path.open("rb") as stream:
                end = stream.seek(0, os.SEEK_END)
                size = min(end, 65536)
                stream.seek(end - size)
                tail = stream.read(size).decode("utf-8", errors="replace").splitlines()[-tail_lines:]
        except OSError:
            pass
    return {
        "name": name,
        "running": running,
        "returncode": (owned.returncode if running is False and owned is not None
                       and state.get("pid") == owned.pid else None),
        "started_at": state.get("started_at"),
        "args": state.get("args", []),
        "log_tail": tail,
    }


@contextmanager
def _job_lock(name):
    """Serialize start and stop for a job across dashboard processes."""
    path = _state_file(name).with_suffix('.lock')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if sys.platform.startswith('win'):
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, PermissionError):
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if sys.platform.startswith('win'):
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def start_job(name: str, args: List[str]) -> bool:
    """Run a job only when no previous worker is active and its status is known."""
    if isinstance(args, str):
        raise ValueError("آرگومان‌های وظیفه باید فهرستی از متن‌ها باشند")
    args = list(args)
    if not all(isinstance(arg, str) for arg in args):
        raise ValueError("آرگومان‌های وظیفه باید متن باشند")
    with _job_lock(name) as acquired:
        return _start_job_locked(name, args) if acquired else False


def _start_job_locked(name: str, args: List[str]) -> bool:
    if job_status(name, tail_lines=0)["running"] is not False:
        return False
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = settings.LOG_DIR / f"job-{name}.log"
    flags = 0
    if sys.platform.startswith("win"):
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    started_at = now_tehran_iso()
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
    state = _state_file(name)
    temporary = state.with_name(f'{state.stem}-{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps({
            "pid": proc.pid, "args": args, "started_at": started_at, "log": str(log_path),
            "created_at": created_at,
        }, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, state)
    except OSError:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        _OWNED_PROCESSES.pop(name, None)
        temporary.unlink(missing_ok=True)
        raise
    return True


def stop_job(name: str) -> bool:
    with _job_lock(name) as acquired:
        return _stop_job_locked(name) if acquired else False


def _stop_job_locked(name: str) -> bool:
    owned = _OWNED_PROCESSES.get(name)
    owned_running = owned is not None and owned.poll() is None
    proc = None
    try:
        state = _read_state(name) or {}
        if not owned_running or state.get("pid") == owned.pid:
            proc = _job_process(state)
    except _IDENTITY_ERRORS:
        pass
    if proc is None:
        if not owned_running:
            return False
        # در محیطی که psutil فضای PID دیگری می‌بیند، فقط handle متعلق به خودمان متوقف می‌شود.
        owned.terminate()
        try:
            owned.wait(timeout=10)
        except subprocess.TimeoutExpired:
            owned.kill()
            owned.wait(timeout=5)
        return True
    processes = proc.children(recursive=True) + [proc]
    for target in processes:
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            pass
    _, survivors = psutil.wait_procs(processes, timeout=5)
    for target in survivors:
        try:
            target.kill()
        except psutil.NoSuchProcess:
            pass
    _, survivors = psutil.wait_procs(survivors, timeout=5)
    if survivors:
        raise RuntimeError("برخی فرایندهای وظیفه متوقف نشدند؛ وضعیت وظیفه را بررسی کنید")
    if owned_running:
        owned.wait(timeout=5)
    return True
