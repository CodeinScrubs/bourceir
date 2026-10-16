"""گزارش JSON خصوصی و اتمی؛ ورودی خود یا فایل دیتابیس را بازنویسی نمی‌کند."""

import json
import os
import tempfile
from pathlib import Path

from config import settings


def private_report_path(path, *, forbidden_paths=()):
    target = Path(path).resolve()
    if not target.is_relative_to((settings.DATA_DIR / 'private').resolve()) or target.suffix.lower() != '.json':
        raise ValueError('خروجی گزارش باید فایل JSON داخل data/private/ باشد')
    for source in forbidden_paths:
        source = Path(source).resolve()
        if target == source or (target.exists() and source.exists() and target.samefile(source)):
            raise ValueError('گزارش نمی‌تواند فایل ورودی یا دیتابیس را بازنویسی کند')
    return target


def write_private_report(path, payload, *, forbidden_paths=()):
    target = private_report_path(path, forbidden_paths=forbidden_paths)
    body = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                         prefix='.' + target.name + '.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return target
