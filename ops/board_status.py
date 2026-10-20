"""نمای فقط‌خواندنی پایش مستقل DOM؛ بدون اتصال شبکه یا حساب."""
import json
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from core.timeutils import now_tehran


def read_board_status(private_root, *, at=None):
    root = Path(private_root).resolve()
    at = at or now_tehran()
    if at.tzinfo is None:
        raise ValueError('زمان بررسی باید دارای منطقه زمانی باشد')

    def read(path):
        if path.stat().st_size > 2_000_000:
            raise ValueError('فایل وضعیت بیش از حد بزرگ است')
        value = json.loads(path.read_text(encoding='utf-8'),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('عدد نامعتبر')))
        if not isinstance(value, dict):
            raise ValueError('فایل وضعیت معتبر نیست')
        return value

    try:
        pointer = read(root / 'farabi-board' / 'current.json')
        archive = Path(pointer['archive']).resolve()
        if not archive.is_relative_to(root) or archive == root:
            raise ValueError('آرشیو باید در پوشه خصوصی باشد')
        url = urlsplit(pointer['url'])
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port
                or url.username or url.password or url.query or url.fragment or url.path):
            raise ValueError('نشانی پنل باید محلی باشد')
        status = read(archive / 'status.json')
        if status.get('version') != 'visible-board-alerts-v1' or type(status.get('running')) is not bool:
            raise ValueError('قرارداد وضعیت پایش شناخته‌شده نیست')
        stamp = datetime.fromisoformat(status['updated_at'])
        if stamp.tzinfo is None:
            raise ValueError('زمان وضعیت بدون منطقه زمانی است')
        age = (at-stamp).total_seconds()
        if not isinstance(status.get('boards'), list) or not isinstance(status.get('alerts'), list):
            raise ValueError('فهرست وضعیت تابلو موجود نیست')
        return dict(state='current' if status['running'] and 0 <= age <= 3.5 else 'stale',
                    age_seconds=age, url=pointer['url'], status=status)
    except FileNotFoundError:
        return dict(state='absent', url=None, status=None)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        return dict(state='invalid', url=None, status=None, reason=str(exc))
