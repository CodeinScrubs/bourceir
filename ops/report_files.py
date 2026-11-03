"""گزارش JSON خصوصی و اتمی؛ ورودی خود یا فایل دیتابیس را بازنویسی نمی‌کند."""

import json
import math
import os
import tempfile
import time
from pathlib import Path

from config import settings

_WINDOWS = os.name == 'nt'
_LOCK_RETRY_DELAYS = (.01, .02, .04, .08)


def _retry_windows_file_lock(operation):
    """Retry only bounded Windows sharing/access denials, preserving the first error."""
    original = None
    for delay in (*_LOCK_RETRY_DELAYS, None):
        try:
            return operation()
        except PermissionError as error:
            if not _WINDOWS or getattr(error, 'winerror', None) not in (5, 32, 33):
                raise
            if original is None:
                original = error
            if delay is None:
                raise original
            time.sleep(delay)


def json_report_path(path, *, forbidden_paths=()):
    target = Path(path).resolve()
    if target.suffix.lower() != '.json':
        raise ValueError('خروجی گزارش باید فایل JSON باشد')
    for source in forbidden_paths:
        source = Path(source).resolve()
        if target == source or (target.exists() and source.exists() and target.samefile(source)):
            raise ValueError('گزارش نمی‌تواند فایل ورودی یا دیتابیس را بازنویسی کند')
    return target


def private_report_path(path, *, forbidden_paths=()):
    target = json_report_path(path, forbidden_paths=forbidden_paths)
    if not target.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('خروجی گزارش باید فایل JSON داخل data/private/ باشد')
    return target


def write_json_report(path, payload, *, forbidden_paths=()):
    target = json_report_path(path, forbidden_paths=forbidden_paths)
    body = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    primary_error = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.' + target.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        _retry_windows_file_lock(lambda: os.replace(temporary, target))
        temporary = None  # A successful atomic move leaves no source file to clean up.
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if temporary is not None:
            try:
                _retry_windows_file_lock(lambda: temporary.unlink(missing_ok=True))
            except OSError as error:
                if primary_error is None:
                    raise
                primary_error.add_note(f'Temporary JSON cleanup also failed ({type(error).__name__}); '
                                       f'temporary file may remain: {temporary}')
    return target


def write_private_report(path, payload, *, forbidden_paths=()):
    target = private_report_path(path, forbidden_paths=forbidden_paths)
    return write_json_report(target, payload, forbidden_paths=forbidden_paths)


def read_private_report(path, *, max_bytes=16 * 1024 * 1024):
    """خواندن UTF-8 مستقل از locale؛ کلید تکراری و عدد نامتناهی مبهم پذیرفته نمی‌شود."""
    if type(max_bytes) is not int or max_bytes < 1:
        raise ValueError('سقف اندازه گزارش باید عدد صحیح مثبت باشد')
    target = private_report_path(path, forbidden_paths=(settings.DB_PATH,))
    with target.open('rb') as stream:
        body = stream.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('گزارش از سقف اندازه مجاز بزرگ‌تر است')

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('کلید تکراری در گزارش JSON')
            result[key] = value
        return result

    def nonfinite(_):
        raise ValueError('عدد نامتناهی در گزارش JSON')

    try:
        report = json.loads(body.decode('utf-8'), object_pairs_hook=pairs, parse_constant=nonfinite)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError('گزارش UTF-8/JSON معتبر نیست') from exc
    if not isinstance(report, dict):
        raise ValueError('ریشه گزارش JSON باید یک شیء باشد')
    stack = [report]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
        elif isinstance(value, float) and not math.isfinite(value):
            nonfinite(value)
    return report
