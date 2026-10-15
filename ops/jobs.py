# -*- coding: utf-8 -*-
"""
اجرای کارهای طولانی در پس‌زمینه (برای دکمه‌های داشبورد)

داشبورد هر کار (به‌روزرسانی داده، پایش زنده، ...) را به‌صورت یک فرآیند جدا از brs.py اجرا می‌کند،
تا با بستن یا رفرش صفحه مرورگر متوقف نشود. وضعیت هر کار در data/jobs/<نام>.json ثبت می‌شود.
"""

import json
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
    running = owned.poll() is None if owned is not None else _is_alive(state.get("pid"), state.get("created_at"))
    return {
        "name": name,
        "running": running,
        "returncode": owned.returncode if owned is not None else None,
        "started_at": state.get("started_at"),
        "args": state.get("args", []),
        "log_tail": tail,
    }


@contextmanager
def _start_lock(name):
    """OS lock, released after a crash; prevents check/start races across dashboard processes."""
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
    """اجرای brs.py با آرگومان‌های داده‌شده در پس‌زمینه. اگر همین کار در حال اجرا باشد False."""
    with _start_lock(name) as acquired:
        return _start_job_locked(name, args) if acquired else False


def _start_job_locked(name: str, args: List[str]) -> bool:
    if job_status(name)["running"]:
        return False
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    settings.LOG_DIR.mkdir(parents=True, exist_ok=True)
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
    state = _state_file(name)
    temporary = state.with_name(f'{state.stem}-{uuid.uuid4().hex}.tmp')
    try:
        temporary.write_text(json.dumps({
            "pid": proc.pid, "args": args, "started_at": now_tehran_iso(), "log": str(log_path),
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
    state = _read_state(name) or {}
    pid = state.get("pid")
    owned = _OWNED_PROCESSES.get(name)
    if owned is not None:
        if owned.poll() is not None:
            return False
        # در محیطی که psutil فضای PID دیگری می‌بیند، فقط handle متعلق به خودمان متوقف می‌شود.
        if not _is_alive(pid, state.get("created_at")):
            owned.terminate()
            try:
                owned.wait(timeout=10)
            except subprocess.TimeoutExpired:
                owned.kill()
                owned.wait(timeout=5)
            return True
    if not _is_alive(pid, state.get("created_at")):
        return False
    proc = psutil.Process(pid)
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
    if owned is not None:
        owned.wait(timeout=5)
    return True
