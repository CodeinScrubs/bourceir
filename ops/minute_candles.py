"""Collect public market evidence and publish a CSV only after daily reconciliation."""

import hashlib
from pathlib import Path
from tempfile import mkdtemp

from config import settings
from core.minute_candles import VERSION, closed_trade_day, reconcile_session
from core.price_revision import daily_request
from core.timeutils import now_tehran_iso
from data_collector.trade_archive import TradeArchive
from ops.query_report import export_csv
from ops.report_files import private_report_path, write_private_report


def export_minute_session(provider, symbol, requested_day, *, refresh=False):
    if (symbol is None or symbol.get('instrument_type_code') not in (300, 303)
            or symbol['symbol_fa'][-1:].isdigit()):
        raise ValueError('نماد یکتای سهام عادی بورس یا فرابورس در فهرست BRS لازم است')
    day = closed_trade_day(requested_day)
    code, _ = daily_request(symbol.get('ins_code'), day)
    root = private_report_path(settings.DATA_DIR / 'private' / 'minute-candles' / 'audit.json').parent
    root.mkdir(parents=True, exist_ok=True)
    staging = Path(mkdtemp(prefix=f'.{day}-{code}-', dir=root))
    daily = provider.get_dated_daily_observation(code, day)
    report = dict(version=VERSION, date=day, ins_code=code, symbol=symbol['symbol_fa'],
                  generated_at=now_tehran_iso(), daily_evidence_file=None, trade_archive_file=None, csv_file=None)
    tape = None
    if daily is not None:
        daily_path = write_private_report(staging / 'daily-source.json', daily, forbidden_paths=(settings.DB_PATH,))
        report.update(daily_evidence_file=daily_path.name,
                      daily_evidence_sha256=hashlib.sha256(daily_path.read_bytes()).hexdigest())
        archive_root = private_report_path(root.parent / 'trade-archive' / 'record.json').parent
        archived, archive_path = TradeArchive(archive_root).capture(provider, code, day, refresh=refresh)
        report.update(trade_archive_file=archive_path.relative_to(root.parent).as_posix(),
                      trade_archive_status=archived['status'])
        tape = archived.get('observation')
    try:
        bars, result = reconcile_session(code, day, daily, tape)
    except ValueError as exc:
        bars, result = [], dict(status='invalid_source', bar_count=0, detail=str(exc))
    report.update(result)
    if report['status'] == 'reconciled' and bars:
        target = staging / 'candles.csv'
        export_csv(target, bars, tuple(bars[0]))
        report.update(csv_file=target.name, csv_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    write_private_report(staging / 'audit.json', report, forbidden_paths=(settings.DB_PATH,))
    folder = staging.rename(staging.with_name(staging.name[1:]))
    return folder, report
