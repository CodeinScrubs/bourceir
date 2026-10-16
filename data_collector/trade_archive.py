"""نسخه‌های مستقل ریزمعاملات عمومی؛ هیچ معامله حساب یا سفارش ساخته نمی‌شود."""

import gzip
import hashlib
import json
import os
import uuid
from pathlib import Path

from core.query_screener import iso_day
from core.timeutils import now_tehran_iso


class TradeArchive:
    def __init__(self, root, *, max_bytes=128_000_000):
        if isinstance(max_bytes,bool) or not isinstance(max_bytes,int) or not 1024 <= max_bytes <= 256_000_000:
            raise ValueError('سقف آرشیو باید بین ۱۰۲۴ بایت و ۲۵۶ مگابایت باشد')
        self.max_bytes=max_bytes
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _key(self, ins_code, trade_date):
        if not str(ins_code).isdigit() or len(str(ins_code)) > 30:
            raise ValueError('شناسه نماد نامعتبر است')
        return f'{ins_code}_{iso_day(trade_date).isoformat()}_'

    def load(self, path):
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('فایل بیرون از آرشیو است')
        with gzip.open(path, 'rb') as stream:
            data = stream.read(self.max_bytes+1)
        if len(data)>self.max_bytes:
            raise ValueError('حجم آرشیو از سقف خواندن بیشتر است؛ این پیام به معنی خرابی هش نیست')
        if hashlib.sha256(data).hexdigest() != path.name.split('_')[-1].split('.')[0]:
            raise ValueError('هش آرشیو ریزمعاملات سازگار نیست')
        record = json.loads(data)
        prefix = self._key(record['ins_code'], record['trade_date'])
        if not path.name.startswith(prefix):
            raise ValueError('نام فایل با شناسه و تاریخ محتوا سازگار نیست')
        return record

    def capture(self, provider, ins_code, trade_date, *, refresh=False):
        prefix = self._key(ins_code, trade_date)
        if not refresh:
            for path in sorted(self.root.glob(prefix+'*.json.gz'), key=lambda p: p.stat().st_mtime_ns, reverse=True):
                cached = self.load(path)
                if cached['status'] == 'ok' and ((cached.get('observation') or {}).get('response') or {}).get('tradeHistory'):
                    return cached, path
        record = dict(ins_code=str(ins_code), trade_date=trade_date, attempted_at=now_tehran_iso(), status='unavailable', observation=None)
        if provider is not None:
            try:
                observation = provider.get_trade_session(str(ins_code), trade_date)
                if observation is not None:
                    rows=(observation.get('response') or {}).get('tradeHistory')
                    record.update(status='ok' if isinstance(rows,list) and rows else 'empty', observation=observation)
            except (NotImplementedError, ValueError, OSError) as error:
                record['error_type'] = type(error).__name__
        data = json.dumps(record, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(',', ':')).encode()
        if len(data)>self.max_bytes:
            record.update(status='oversized',observation=None,unpreserved_record_bytes=len(data),
                unpreserved_record_sha256=hashlib.sha256(data).hexdigest(),raw_payload_preserved=False)
            data=json.dumps(record,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode()
        sha = hashlib.sha256(data).hexdigest()
        final = self.root / (prefix+sha+'.json.gz')
        temporary = self.root / ('.'+uuid.uuid4().hex+'.tmp')
        # فایل کامل به نام مبتنی بر محتوا منتشر می‌شود؛ فهرست مشترک قابل بازنویسی نداریم.
        with temporary.open('xb') as stream:
            stream.write(gzip.compress(data, mtime=0))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, final)
        return record, final
