"""ثبت پیش از دریافت و گزارش فقط‌خواندنیِ بازار؛ بدون تماس مستقیم با منبع داده."""

import hashlib
import html
import shutil
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from threading import local

from config import settings
from core.market_review import (
    MARKET_REVIEW_VERSION,
    STATUS_LABELS_FA,
    build_market_review,
    market_scope,
    registry_coverage,
)
from core.timeutils import now_tehran, now_tehran_iso, to_jalali
from data_collector.base import BaseDataProvider
from ops.report_files import private_report_path, read_private_report, write_private_report


def load_market_review(folder):
    """پیش از نمایش آرشیو، هش همه شاهدها کنترل می‌شود؛ کد محفوظ اجرا نمی‌شود."""
    folder = Path(folder).resolve()
    private_report_path(folder / 'report.json', forbidden_paths=[settings.DB_PATH])
    manifest = read_private_report(folder / 'manifest.json')
    files = manifest.get('files')
    if manifest.get('version') != MARKET_REVIEW_VERSION or not isinstance(files, dict) or 'report.json' not in files:
        raise ValueError('فهرست شواهد بازار نامعتبر است')
    for name, expected in files.items():
        candidate = folder / name
        target = candidate.resolve()
        if (Path(name).is_absolute() or '..' in Path(name).parts or candidate.is_symlink()
                or not target.is_relative_to(folder) or not target.is_file()):
            raise ValueError('مسیر شاهد بازار نامعتبر است')
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected:
            raise ValueError('هش شاهد بازار تغییر کرده است')
    report = read_private_report(folder / 'report.json')
    if report.get('version') != MARKET_REVIEW_VERSION or report.get('run_id') != folder.name:
        raise ValueError('هویت گزارش بازار با آرشیو یکسان نیست')
    return report


def render_html(report):
    def text(value):
        return html.escape('نامعلوم' if value is None else str(value))

    def numeric(value, percent=False, money=False):
        if value is None:
            return 'نامعلوم'
        if percent:
            return f'{value * 100:+.2f}٪'
        if money:
            return f'{value / 1e10:,.2f}'
        return f'{value:,.0f}'

    def table(headers, rows):
        def cell(value):
            raw = str(value)
            direction = ' dir="ltr" style="unicode-bidi:isolate"' if raw.lstrip('+-')[:1].isdigit() else ''
            return '<td' + direction + '>' + text(value) + '</td>'

        return '<table><tr>' + ''.join('<th>' + text(h) + '</th>' for h in headers) + '</tr>' + ''.join(
            '<tr>' + ''.join(cell(value) for value in row) + '</tr>' for row in rows) + '</table>'

    summary = report['summary']
    parts = [f'<h1>گزارش بازار {text(to_jalali(report["session_date"]))}</h1>',
             f'<p>زمان گردآوری: {text(report["observed_at"])}؛ نسخه {text(report["version"])}؛ وضعیت {text(report["status"])}</p>',
             '<p>دامنه: فقط سهام عادی بورس و فرابورس، بدون صندوق، بازار پایه، حق‌تقدم و تابلوی بلوکی. '
             'شاخص‌های رسمی دامنه متفاوت دارند. گزارش توصیفی است و پیشنهاد معامله ندارد.</p>',
             '<h2>شاخص‌های رسمی</h2>']
    parts.append(table(['بازار', 'شاخص کل', 'تغییر', 'درصد تغییر', 'شاخص هم‌وزن', 'درصد هم‌وزن', 'زمان فعالیت منبع'], [
        ['بورس' if item['flow'] == 1 else 'فرابورس', numeric(item['index_value']), numeric(item['index_change']),
         numeric(item['index_return'], percent=True), numeric(item['equal_weight_value']),
         numeric(item['equal_weight_return'], percent=True), item['provider_timestamp']]
        for item in report['official_overviews']]))
    parts.append('<p>زمان بالا، زمان فعالیت بازار در همان پاسخ است؛ ساعت مستقل انتشار شاخص تأیید نشده است.</p>')
    parts.append('<h2>پوشش و پهنای بازار</h2>')
    parts.append(table(['معیار', 'مقدار'], [
        ['کل نمادهای یکتای داخل دامنه دیده‌بان', summary['scope_count']],
        ['کندل معتبر معامله‌شده همین جلسه', summary['traded_count']],
        ['بازده معلوم / نامعلوم در کندل‌های معتبر', f'{summary["return_known_count"]} / {summary["return_unknown_count"]}'],
        ['مثبت / منفی / بدون تغییر', f'{summary["positive"]} / {summary["negative"]} / {summary["unchanged"]}'],
        ['درصد مثبت از بازده‌های معلوم', numeric(summary['positive_fraction'], percent=True)],
        ['میانه بازده پایانی', numeric(summary['median_closing_return'], percent=True)],
        ['ارزش معاملات پوشش‌داده‌شده (میلیارد تومان)', numeric(summary['value_rial'], money=True)],
        ['سهم ۱۰ نماد اول از ارزش معاملات معتبر', numeric(summary['top10_value_fraction'], percent=True)],
        ['روز تک‌قیمت صعودی / نزولی (قرینه، بدون تأیید صف)',
         f'{summary["one_price_up_proxy_count"]} / {summary["one_price_down_proxy_count"]}'],
        ['شمار صف تأییدشده و خالص پول حقیقی تاریخ‌دار', 'نامعلوم؛ از کندل یا حجم به ارزش واقعی تبدیل نشد']]))
    parts.append(table(['وضعیت داده', 'شمار'], [[STATUS_LABELS_FA.get(key, 'وضعیت نامعلوم'), value]
                                               for key, value in summary['status_counts'].items()]))
    coverage = report.get('registry_coverage')
    if coverage:
        parts.append('<h2>تطبیق با فهرست محلی نمادها</h2>')
        parts.append('<p>فهرست محلی ' + text(coverage['known_registry_count']) + ' شناسه دارد؛ '
                     + text(len(coverage['registry_not_observed'])) + ' شناسه در پاسخ جاری دیده نشد. '
                     'غیبت، توقف یا حذف قطعی نیست و آمار این نمادها صفر فرض نشده است.</p>')
        parts.append(table(['نماد غایب از پاسخ جاری', 'شناسه', 'وضعیت فعال در فهرست محلی'], [
            [row['symbol_fa'], row['ins_code'], row.get('is_active')] for row in coverage['registry_not_observed']]))
        if coverage.get('registry_inactive_but_observed'):
            parts.append('<p>در فهرست محلی غیرفعال ولی اکنون در دیده‌بان مشاهده‌شده: '
                         + text('، '.join(row['symbol_fa'] for row in coverage['registry_inactive_but_observed']))
                         + '. این تفاوت پیش از اتکا به جامعه محلی باید بررسی شود؛ پرچم حساب یا عضویت خودکار تغییر نکرد.</p>')
    parts.append('<h2>صنایع</h2><p>میانه بازده شرکت‌های معتبر هر صنعت است؛ بازده شاخص رسمی صنعت نیست. '
                 'تعدادهای کم و پوشش ناقص را در مقایسه لحاظ کنید.</p>')
    parts.append(table(['صنعت / کد', 'داخل دامنه', 'کندل معتبر', 'مثبت / منفی / ثابت', 'میانه بازده',
                        'ارزش معتبر (میلیارد تومان)'], [
        [str(item['name'] or 'نام نامعلوم') + ' / ' + str(item['code']), item['scope_count'], item['traded_count'],
         f'{item["positive"]} / {item["negative"]} / {item["unchanged"]}',
         numeric(item['median_closing_return'], percent=True), numeric(item['value_rial'], money=True)]
        for item in report['sectors']]))
    for key, title in [('top_value', 'بیشترین ارزش معاملات'), ('gainers', 'بیشترین بازده پایانی'),
                       ('losers', 'کمترین بازده پایانی'), ('largest_observed', 'بزرگ‌ترین ارزش شرکت مشاهده‌شده')]:
        parts.append('<h2>' + title + '</h2>')
        parts.append(table(['نماد', 'صنعت', 'پایانی (ریال)', 'بازده پایانی', 'بازده آخرین معامله',
                            'ارزش معاملات (میلیارد تومان)', 'ارزش شرکت مشاهده‌شده (میلیارد تومان)'], [
            [item['symbol'], item['sector_name'] or item['sector_code'] or 'نامعلوم', numeric(item['close_final_raw']),
             numeric(item['closing_return'], percent=True), numeric(item['last_return'], percent=True),
             numeric(item['value_rial'], money=True), numeric(item['market_cap_observed_rial'], money=True)]
            for item in report[key]]))
    parts.append('<details><summary>جدول کامل نمادها، پوشش و داده نامعلوم</summary>')
    parts.append(table(['نماد', 'صنعت', 'وضعیت', 'تاریخ منبع', 'پایانی (ریال)', 'بازده پایانی',
                        'ارزش معاملات (میلیارد تومان)'], [
        [row['symbol'], row['sector_name'] or row['sector_code'] or 'نامعلوم', STATUS_LABELS_FA.get(row['status'], 'وضعیت نامعلوم'),
         row.get('source_date') or 'نامعلوم', numeric(row.get('close_final_raw')),
         numeric(row['closing_return'], percent=True), numeric(row['value_rial'], money=True)]
        for row in sorted(report['instruments'], key=lambda row: row['symbol'])]))
    parts.append('</details>')
    parts.append('<h2>محدودیت‌ها و علت‌یابی</h2><p>رتبه‌های بالا فقط بین داده‌های معتبر پوشش‌داده‌شده‌اند. '
                 'تعداد سهام از دیده‌بان مشاهده شده و تاریخ اعتبار مستقل آن اثبات نشده است. '
                 'خالص ارزش خرید حقیقی از ضرب حجم در آخرین قیمت ساخته نشده است. '
                 'تک‌قیمت بودن، صف یا امکان پرشدن سفارش را اثبات نمی‌کند. '
                 'ریزش هم‌زمان سهم‌ها به‌تنهایی علت سیاسی، بنیادی یا خبری مشخصی را اثبات نمی‌کند. '
                 'این دوره برای فرضیه مشتق‌شده دیگر آزمون مستقل نیست؛ holdout باز نشده و روش پذیرفته‌شده نداریم.</p>')
    return ('<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8"><meta name="viewport" '
            'content="width=device-width, initial-scale=1"><title>BRS — گزارش بازار</title><style>'
            'body{font-family:Tahoma,Arial;background:#f1f5f9;color:#163947;line-height:1.9;margin:0}'
            'main{max-width:1300px;margin:auto;padding:24px;overflow-x:auto;background:white}'
            'h1,h2{color:#145d70}table{border-collapse:collapse;width:100%;margin:20px 0}'
            'td,th{border:1px solid #cbd5e1;padding:9px;text-align:right}th{background:#e7f1f5}'
            '</style><main>' + ''.join(parts) + '</main></html>')


def collect_market_review(provider: BaseDataProvider, *, provider_factory=None, workers=1, output_root=None, registry=None):
    if type(workers) is not int or not 1 <= workers <= 4 or (workers > 1 and provider_factory is None):
        raise ValueError('تعداد دریافت هم‌زمان باید ۱ تا ۴ و هر worker دارای provider مستقل باشد')
    root = Path(output_root or settings.DATA_DIR / 'private' / 'market-reviews')
    folder = root / ('MARKET-' + uuid.uuid4().hex)
    private_report_path(folder / 'protocol.json', forbidden_paths=[settings.DB_PATH])
    folder.mkdir(parents=True, exist_ok=False)
    code_hashes = {}
    for name in ('core/market_review.py', 'ops/market_review.py', 'data_collector/base.py',
                 'data_collector/tsetmc.py', 'core/timeutils.py', 'core/text.py', 'config/default.yaml'):
        source = Path(__file__).resolve().parents[1] / name
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        code_hashes[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    protocol = dict(version=MARKET_REVIEW_VERSION, project_version=settings.VERSION,
                    registered_at=now_tehran_iso(), session_date=now_tehran().date().isoformat(),
                    scope_types=[300, 303], max_instruments=1000, workers=workers, source=provider.name,
                    max_logical_requests=1004, stop_after_consecutive_failures=5, code_sha256=code_hashes,
                    access_purpose='owner-requested descriptive session review; no method selection',
                    database_writes=False, holdout_opened=False, accepted_for_signals=False)
    write_private_report(folder / 'protocol.json', protocol)
    if registry is not None:
        write_private_report(folder / 'registry.json', {'instruments': registry})
    print(f'ثبت بررسی بازار: {folder.name}', flush=True)
    watch = provider.get_market_observation()
    write_private_report(folder / 'watch.json', watch if isinstance(watch, dict) else {'unavailable': True})
    scope, exclusions = market_scope(watch)
    if len(scope) > protocol['max_instruments']:
        raise ValueError('دامنه بیش از بودجه است؛ هیچ زیرمجموعه دلخواهی جایگزین نشد')
    write_private_report(folder / 'selection.json', dict(registered_at=now_tehran_iso(),
                         ins_codes=[item['metadata']['ins_code'] for item in scope], exclusions=exclusions))
    closings, failures, attempted, stopped = {}, 0, 0, False
    state = local()

    def fetch(code):
        if not hasattr(state, 'provider'):
            state.provider = provider_factory() if provider_factory else provider
        return state.provider.get_closing_observation(code)

    queue = iter(item['metadata']['ins_code'] for item in scope)
    pending = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        for _ in range(min(workers, len(scope))):
            code = next(queue)
            pending[executor.submit(fetch, code)] = code
        while pending:
            ready, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in ready:
                code = pending.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    result = dict(error_type=type(exc).__name__)
                attempted += 1
                available = isinstance(result, dict) and isinstance(result.get('candle'), dict)
                failures = 0 if available else failures + 1
                stopped |= failures >= protocol['stop_after_consecutive_failures']
                closings[code] = result if available else None
                write_private_report(folder / f'closing-{code}.json', result if isinstance(result, dict) else {'unavailable': True})
                if attempted % 50 == 0:
                    print(f'دریافت جلسه: {attempted}/{len(scope)}', flush=True)
                if not stopped:
                    next_code = next(queue, None)
                    if next_code:
                        pending[executor.submit(fetch, next_code)] = next_code
    overviews = [provider.get_market_overview(flow) for flow in (1, 2)]
    sectors = provider.get_sector_names() or {}
    write_private_report(folder / 'overviews.json', {'items': overviews})
    write_private_report(folder / 'sectors.json', sectors)
    report = build_market_review(watch, closings, overviews, sectors,
                                 session_date=protocol['session_date'], observed_at=now_tehran_iso())
    if registry is not None:
        report['registry_coverage'] = registry_coverage(watch, registry)
    report.update(run_id=folder.name, collection_attempted=attempted,
                  collection_unattempted=len(scope) - attempted,
                  collection_stopped_early=attempted < len(scope), code_sha256=code_hashes)
    write_private_report(folder / 'report.json', report)
    (folder / 'report.html').write_text(render_html(report), encoding='utf-8')
    inventory = {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(folder.rglob('*')) if path.is_file()}
    write_private_report(folder / 'manifest.json', dict(version=MARKET_REVIEW_VERSION, files=inventory))
    return dict(report=report, folder=str(folder))


def reanalyze_market_review(source_folder, *, registry=None, output_root=None):
    """بازحساب توصیفی از شاهد تأییدشده؛ دریافت تازه یا بازاجرای نرمال‌سازی منبع نیست."""
    source = Path(source_folder).resolve()
    original = load_market_review(source)
    files = read_private_report(source / 'manifest.json')['files']
    if not {'watch.json', 'selection.json', 'overviews.json', 'sectors.json'}.issubset(files):
        raise ValueError('بازحساب به آرشیو اصلی دریافت بازار نیاز دارد')
    root = Path(output_root or settings.DATA_DIR / 'private' / 'market-reviews')
    folder = root / ('MARKET-' + uuid.uuid4().hex)
    private_report_path(folder / 'protocol.json', forbidden_paths=[settings.DB_PATH])
    folder.mkdir(parents=True, exist_ok=False)
    protocol = dict(version=MARKET_REVIEW_VERSION, project_version=settings.VERSION, registered_at=now_tehran_iso(),
                    source_run_id=source.name, source_manifest_sha256=hashlib.sha256((source / 'manifest.json').read_bytes()).hexdigest(),
                    access_purpose='descriptive arithmetic reanalysis; no strategy selection',
                    network_used=False, database_writes=False, normalization_reexecuted=False,
                    holdout_opened=False, accepted_for_signals=False)
    write_private_report(folder / 'protocol.json', protocol)
    for name in (*files, 'manifest.json'):
        target = folder / 'source' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        if name in files and hashlib.sha256(target.read_bytes()).hexdigest() != files[name]:
            raise ValueError('شاهد حین کپی تغییر کرد')
    frozen = folder / 'source'
    for name in ('core/market_review.py', 'ops/market_review.py', 'core/timeutils.py', 'core/text.py'):
        target = folder / 'code' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((Path(__file__).resolve().parents[1] / name).read_bytes())
    # ورودی‌ها همان زمان دریافت قبلی را حفظ می‌کنند؛ تاریخ بازبینی جای تازگی بازار نمی‌نشیند.
    watch = read_private_report(frozen / 'watch.json')
    selection = read_private_report(frozen / 'selection.json')['ins_codes']
    if (not isinstance(selection, list) or any(not isinstance(code, str) or not code.isdigit() for code in selection)
            or len(selection) > 1000 or len(selection) != len(set(selection))):
        raise ValueError('انتخاب محفوظ شناسه‌ها نامعتبر است')
    closings = {code: read_private_report(frozen / f'closing-{code}.json')
                for code in selection if f'closing-{code}.json' in files}
    report = build_market_review(watch, closings, read_private_report(frozen / 'overviews.json')['items'],
                                 read_private_report(frozen / 'sectors.json'),
                                 session_date=original['session_date'], observed_at=original['observed_at'])
    if registry is not None:
        write_private_report(folder / 'registry.json', {'instruments': registry})
        report['registry_coverage'] = registry_coverage(watch, registry)
    report.update(run_id=folder.name, source_run_id=source.name, reanalyzed_at=now_tehran_iso(),
                  normalization_reexecuted=False, network_used=False)
    write_private_report(folder / 'report.json', report)
    (folder / 'report.html').write_text(render_html(report), encoding='utf-8')
    inventory = {path.relative_to(folder).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(folder.rglob('*')) if path.is_file()}
    write_private_report(folder / 'manifest.json', dict(version=MARKET_REVIEW_VERSION, files=inventory))
    return dict(report=report, folder=str(folder))
