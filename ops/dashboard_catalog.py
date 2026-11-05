"""Local capability and file catalogs. Never execute discovered modules or report HTML."""

import argparse
import ast
import json
import math
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from core.text import normalize_symbol

MAX_FILE_BYTES = 16 * 1024 * 1024
PREVIEW_BYTES = 256 * 1024
REPORT_EXTENSIONS = frozenset({'.html', '.htm', '.json', '.md', '.txt', '.csv', '.pdf', '.xlsx', '.zip', '.png', '.jpg', '.jpeg'})
COLLECTION_LABELS = {
    'market-reviews': 'گزارش جلسات بازار', 'market-queries': 'پرسش‌های بازار و ارزش شرکت',
    'stock-studies': 'مطالعه رشد نمادها', 'opportunity-radar': 'رادار فرصت‌ها و دفتر مشاهده',
    'farabi': 'گزارش‌های حساب فارابی', 'farabi-board': 'پایش صف و حجم فارابی',
    'farabi-timing': 'ثبت مشاهده و زمان‌بندی فارابی', 'daily-publications': 'رسید انتشار قیمت و حقیقی حقوقی',
    'rights-history': 'تاریخچه حق تقدم', 'rights-discovery': 'کشف پرونده‌های حق تقدم',
    'rights-daily-universe': 'جامعه روزانه حق تقدم', 'rights-daily-retrospective': 'بکتست تاریخی حق تقدم',
    'rights-retrospective-history': 'بازسازی تاریخچه حق تقدم', 'rights-cohort-inventories': 'پوشش پرونده‌های حق تقدم',
    'rights-registrations': 'ثبت افزایش سرمایه با آورده', 'capital-actions': 'اسناد افزایش سرمایه',
    'share-changes': 'تغییر تعداد کل سهام', 'dividend-evidence': 'شواهد سود نقدی',
    'dividend-reconciliation': 'تطبیق سود نقدی', 'bonus-share-cases': 'حسابداری سهام جایزه',
    'cash-dividend-cases': 'حسابداری دریافت سود نقدی', 'capital-capacity': 'هزینه و ظرفیت سرمایه',
    'queue-fall-studies': 'شکست صف خرید و افت همان روز', 'trade-archive': 'آرشیو ریزمعاملات',
    'price-rechecks': 'بازبینی قیمت‌های ناسازگار', 'price-revisions': 'اصلاح مستند قیمت',
    'price-recovery': 'بازیابی قیمت با شواهد', 'reference-recoveries': 'بازیابی قیمت مرجع',
    'codal-documents': 'اسناد و خلاصه اطلاعیه‌های کدال', 'codal-search': 'جست‌وجوی تاریخی کدال',
    'codal-tables': 'جدول‌های تولید و فروش کدال', 'codal-monthly-normalized': 'گزارش ماهانه با واحد مشخص',
    'codal-operating': 'عملکرد فروش و نرخ محصولات', 'codal-financial-scope': 'شواهد صورت مالی',
    'research': 'پژوهش روش‌های معاملاتی', 'research-exports': 'بسته‌های داده پژوهش',
    'instrument-history': 'مشخصات تاریخی نماد', 'instrument-states': 'وضعیت تاریخی نماد',
    'source-health': 'سلامت و محدودیت منابع', 'reviews': 'ممیزی‌ها و بررسی‌ها',
}


def library_label(relative):
    name = Path(relative).name
    return COLLECTION_LABELS.get(name, name)


@dataclass(frozen=True)
class Tool:
    key: str
    name: str
    description: str
    page: str
    group: str
    command: tuple[str, ...]
    access: str


COMMAND_PAGES = {
    'radar': 'radar', 'doctor': 'health', 'audit': 'health', 'publication-review': 'reports',
    'market-review': 'market', 'broker-import': 'broker', 'broker-report': 'broker',
    'broker-refresh-quotes': 'broker', 'update': 'home', 'features': 'health',
    'research': 'research', 'research-dip': 'research', 'research-portfolio': 'research',
    'research-flow': 'research', 'dip-diagnostics': 'reports', 'capital-study': 'capital',
    'capital-review': 'capital', 'instrument-history': 'health', 'instrument-history-review': 'health',
    'dividend-source': 'health', 'dividend-source-review': 'health', 'research-preflight': 'health',
    'trader-readiness': 'health', 'research-recheck': 'health', 'recover-prices': 'health',
    'research-probability': 'probability', 'research-uncertainty': 'probability', 'research-review': 'research',
    'intraday-audit': 'health', 'universe': 'home', 'stock-study': 'growth', 'rights-mark': 'tools',
    'query': 'query', 'archive-site': 'tools', 'catalog-sources': 'tools', 'live': 'live',
    'backup': 'health', 'codal': 'codal', 'codal-document': 'codal', 'codal-tables': 'codal',
    'codal-financial-cells': 'codal', 'codal-normalize': 'codal', 'codal-operating': 'codal',
}

# Domain aliases make English module names discoverable from the Persian UI.
# These are search terms, not claims about a tool's results or validation.
TOOL_SEARCH_TERMS = (
    (('rights',), 'حق تقدم آورده افزایش سرمایه بازگشایی تئوریک'),
    (('capital_actions', 'bonus'), 'افزایش سرمایه سهام جایزه اقدام شرکتی'),
    (('dividend',), 'سود نقدی تقسیم سود مجمع'),
    (('capital-study', 'capital-review'), 'ظرفیت سرمایه هزینه کارمزد نقدشوندگی'),
    (('codal',), 'کدال اطلاعیه صورت مالی بنیادی'),
    (('monthly', 'operating'), 'گزارش ماهانه تولید فروش نرخ محصول'),
    (('queue', 'board'), 'تابلو صف خرید صف فروش شکست صف حجم هشدار'),
    (('broker', 'farabi'), 'کارگزاری فارابی حساب سبد'),
    (('flow',), 'حقیقی حقوقی جریان پول سرانه قدرت خریدار'),
    (('research', 'retrospective'), 'پژوهش بکتست بک تست بک‌تست backtest'),
    (('anniversary',), 'بازده سالانه سبد هم وزن سالگرد'),
    (('surge', 'radar'), 'رادار شکار جهش رشد چند برابر'),
    (('expectancy', 'probability', 'uncertainty'), 'احتمال احتمالات امید بازده عدم قطعیت'),
    (('instrument', 'universe'), 'جامعه نماد مشخصات تاریخچه'),
    (('recovery', 'recover', 'recheck'), 'بازیابی بازبینی اصلاح داده'),
    (('source', 'health', 'doctor', 'readiness'), 'منبع سلامت آمادگی کیفیت داده'),
    (('trade_archive',), 'آرشیو ریز معاملات'),
)


def capability_catalog(base_dir):
    """Argparse is the CLI source of truth; module discovery parses syntax only."""
    from brs import build_parser

    parser = build_parser()
    subcommands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    tools = [Tool('cli:'+item.dest, item.dest, item.help or item.dest,
                  COMMAND_PAGES.get(item.dest, 'tools'), 'فرمان اصلی', ('brs.py', item.dest),
                  'صفحه مرتبط یا تنظیمات پیشرفته') for item in subcommands._choices_actions]
    base = Path(base_dir).resolve()
    for folder in ('ops', 'research'):
        for path in sorted((base / folder).glob('*.py')):
            if path.stem.startswith('_') or not path.stem.isidentifier() or not path.resolve().is_relative_to(base):
                continue
            try:
                tree = ast.parse(path.read_text(encoding='utf-8-sig'))
            except (OSError, UnicodeError, SyntaxError):
                continue
            main = any(isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                       and isinstance(node.test.left, ast.Name) and node.test.left.id == '__name__'
                       and len(node.test.ops) == 1 and isinstance(node.test.ops[0], ast.Eq)
                       and len(node.test.comparators) == 1 and isinstance(node.test.comparators[0], ast.Constant)
                       and node.test.comparators[0].value == '__main__' for node in tree.body)
            if not main:
                continue
            name = folder + '.' + path.stem
            doc = (ast.get_docstring(tree) or name).splitlines()[0]
            tools.append(Tool('module:'+name, name, doc, 'reports' if folder == 'research' else 'tools',
                              'پژوهش پیشرفته' if folder == 'research' else 'ابزار تخصصی',
                              ('-m', name), 'پیشرفته؛ راهنما و خط فرمان'))
    return tools


def filter_tools(tools, query='', group=None):
    words = normalize_symbol(query).casefold().split()
    found = []
    for tool in tools:
        if group is not None and tool.group != group:
            continue
        aliases = ' '.join(text for names, text in TOOL_SEARCH_TERMS if any(name in tool.name.casefold() for name in names))
        text = normalize_symbol(f'{tool.name} {tool.description} {tool.group} {aliases}').casefold()
        if all(word in text for word in words):
            found.append(tool)
    return found


def help_command(tool):
    """Copyable help command only; no shell or execution of catalog entries."""
    return '.\\venv\\Scripts\\python.exe ' + ' '.join((*tool.command, '--help'))


def contained(root, relative):
    root = Path(root).resolve()
    relative = Path(relative)
    windows = PureWindowsPath(str(relative))
    if relative.is_absolute() or windows.drive or windows.root or '..' in relative.parts or '..' in windows.parts:
        raise ValueError('مسیر باید نسبی و داخل کتابخانه باشد')
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('پیوند فایل از کتابخانه خارج است')
    return path


def list_library(root, relative='', *, query='', offset=0, limit=100):
    """Read one directory, never recursively scan thousands of source documents."""
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError('حد صفحه‌بندی معتبر لازم است')
    root = Path(root).resolve()
    current = contained(root, relative)
    if not current.is_dir():
        raise ValueError('پوشه کتابخانه موجود نیست')
    words = normalize_symbol(query).casefold().split()
    entries, skipped = [], 0
    for child in current.iterdir():
        try:
            checked = contained(root, child.relative_to(root))
            if child.is_symlink() or checked != child.absolute() or child.name.startswith('.'):
                skipped += 1
                continue
            directory = checked.is_dir()
            if not directory and (not checked.is_file() or checked.suffix.lower() not in REPORT_EXTENSIONS):
                continue
            label = library_label(child.name)
            if not all(word in normalize_symbol(child.name+' '+label).casefold() for word in words):
                continue
            info = checked.stat()
            entries.append(dict(name=child.name, label=label, relative=child.relative_to(root).as_posix(), directory=directory,
                                size_bytes=None if directory else info.st_size, modified_ns=info.st_mtime_ns))
        except (OSError, ValueError):
            skipped += 1
    entries.sort(key=lambda row: (not row['directory'], -row['modified_ns'], row['name']))
    return dict(relative=current.relative_to(root).as_posix(), total=len(entries),
                entries=entries[offset:offset+limit], offset=offset, limit=limit, skipped=skipped)


def read_library_file(root, relative):
    path = contained(root, relative)
    if not path.is_file() or path.suffix.lower() not in REPORT_EXTENSIONS or path.name.startswith('.'):
        raise ValueError('این فایل گزارش قابل نمایش نیست')
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('این فایل بیش از ۱۶ مگابایت است؛ آن را از پوشه محلی باز کن')
    with path.open('rb') as stream:
        body = stream.read(MAX_FILE_BYTES + 1)
    if len(body) > MAX_FILE_BYTES:
        raise ValueError('فایل حین خواندن بزرگ شد')
    if contained(root, relative) != path:
        raise ValueError('مسیر فایل حین خواندن تغییر کرد')
    return path.name, body


def report_preview(name, body):
    """Preview JSON/text as data. HTML/PDF/spreadsheets remain downloads, never active embeds."""
    if len(body) > PREVIEW_BYTES:
        return 'download', None
    extension = Path(name).suffix.lower()
    if extension == '.json':
        try:
            def unique(pairs):
                values = {}
                for key, value in pairs:
                    if key in values:
                        raise ValueError('duplicate JSON key')
                    values[key] = value
                return values

            def finite_float(text):
                value = float(text)
                if not math.isfinite(value):
                    raise ValueError('nonfinite JSON value')
                return value

            return 'json', json.loads(body.decode('utf-8-sig'), object_pairs_hook=unique,
                                      parse_float=finite_float,
                                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON value')))
        except (UnicodeError, ValueError, RecursionError):
            return 'text', 'JSON معتبر نیست؛ فایل اصلی برای بررسی در دسترس است.'
    if extension in ('.txt', '.md', '.csv'):
        try:
            return 'text', body.decode('utf-8-sig')
        except UnicodeError:
            return 'download', None
    return 'download', None


def document_catalog(base_dir):
    base = Path(base_dir).resolve()
    paths = [base/'README.md', base/'workbench-guide.md', *sorted((base/'docs').rglob('*.md'))]
    result = []
    for path in paths:
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(base):
            continue
        try:
            with path.open('r', encoding='utf-8-sig') as stream:
                head = stream.read(8192)
            title = next((line.lstrip('# ').strip() for line in head.splitlines() if line.strip()), path.stem)
            result.append(dict(relative=path.relative_to(base).as_posix(), title=title))
        except (OSError, UnicodeError):
            continue
    return result
