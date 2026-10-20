"""متن و پیوند عمومی کدال؛ بدون اجرای اسکریپت یا تعبیر متن به واقعیت مالی."""

import re
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

DOCUMENT_VERSION = 'codal-document-v1'
CODAL_BASE = 'https://codal.ir/'
PUBLIC_HOSTS = frozenset({'codal.ir', 'www.codal.ir'})
REPORT_ONLY_PATHS = frozenset({'interimstatement.aspx', 'monthlyactivity.aspx', 'extraassembly.aspx'})
PUBLIC_PATHS = frozenset({'decision.aspx', 'attachment.aspx', 'downloadfile.aspx', *REPORT_ONLY_PATHS})


def public_document_url(value, *, base=CODAL_BASE):
    """فقط مسیر اسناد عمومی رسمی؛ URL حساب، نشانی داخلی و تغییر میزبان پذیرفته نمی‌شود."""
    if not isinstance(value, str) or not value or len(value) > 8192 or re.search(r'[\x00-\x20\\]', value):
        raise ValueError('پیوند عمومی سند کدال معتبر نیست')
    url = urlsplit(urljoin(base, value))
    if (url.scheme != 'https' or url.hostname not in PUBLIC_HOSTS or url.port not in (None, 443)
            or url.username is not None or url.password is not None):
        raise ValueError('فقط پیوند HTTPS اسناد عمومی codal.ir مجاز است')
    path = url.path.casefold()
    if path.rsplit('/', 1)[-1] not in PUBLIC_PATHS or path.count('/') not in (1, 2):
        raise ValueError('مسیر سند عمومی کدال مجاز نیست')
    if path.count('/') == 2 and not path.startswith('/reports/'):
        raise ValueError('مسیر سند عمومی کدال مجاز نیست')
    if path.rsplit('/', 1)[-1] in REPORT_ONLY_PATHS and not path.startswith('/reports/'):
        raise ValueError('مسیر گزارش مالی فقط زیر Reports مجاز است')
    if any(re.search(r'token|password|session|auth|viewstate|api_key', key, re.I)
           for key, _ in parse_qsl(url.query, keep_blank_values=True)):
        raise ValueError('پیوند دارای اطلاعات ورود مجاز نیست')
    return urlunsplit(('https', url.netloc.lower(), url.path, url.query, ''))


class _VisibleDocument(HTMLParser):
    """فقط متن قابل مشاهده؛ جدول سطربه‌سطر است و استخراج کامل عددی ادعا نمی‌شود."""

    def __init__(self, base):
        super().__init__(convert_charrefs=True)
        self.base, self.parts, self.links, self.stack = base, [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        hidden = (tag in {'head', 'script', 'style', 'noscript', 'svg', 'iframe'}
                  or 'hidden' in attrs or attrs.get('aria-hidden') == 'true'
                  or bool(re.search(r'display\s*:\s*none|visibility\s*:\s*hidden', attrs.get('style') or '', re.I)))
        skipped = hidden or (self.stack and self.stack[-1][1])
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'wbr'}:
            self.stack.append((tag, bool(skipped)))
        if skipped:
            return
        if tag in {'br', 'tr', 'p', 'div', 'table', 'li', 'h1', 'h2', 'h3'}:
            self.parts.append('\n')
        if tag in {'td', 'th'}:
            self.parts.append('\t')
        candidate = attrs.get('href') if tag == 'a' else None
        # ردیف پیوست رسمی یک window.open ثابت دارد؛ فقط رشته URL استخراج می‌شود، نه اجرای JavaScript.
        opened = re.fullmatch(r'''\s*window\.open\(\s*(['"])([^'"\r\n]+)\1\s*\)\s*;?\s*''',
                              attrs.get('onclick') or '')
        if not candidate and opened:
            candidate = opened.group(2)
        if candidate:
            try:
                link = public_document_url(candidate, base=self.base)
                if link not in self.links:
                    self.links.append(link)
            except ValueError:
                pass

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag in {'tr', 'p', 'div', 'table', 'li'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.stack or not self.stack[-1][1]:
            self.parts.append(data)


def visible_document(html, url):
    parser = _VisibleDocument(url)
    parser.feed(html)
    parser.close()
    lines = [re.sub(r'[ \r\f\v]+', ' ', line).strip() for line in ''.join(parser.parts).splitlines()]
    return '\n'.join(line for line in lines if line), parser.links


def is_verification_page(html):
    text = html.casefold().replace('ي', 'ی').replace('ك', 'ک')
    return any(marker in text for marker in (
        'captcha', 'challenge-platform', '__cf_chl', 'تأیید کاربر', 'تایید کاربر', 'کد امنیتی را وارد',
    ))


def pdf_has_valid_ending(body):
    """یادداشت کوتاه اسکنر به شکل comment مجاز است؛ HTML خطای سرور مجاز نیست.

    بایت‌های فایل عوض نمی‌شوند. این کنترل، اعتبار ساختار درونی یا امضای PDF نیست.
    """
    eof = body.rfind(b'%%EOF')
    if eof < 0:
        return False
    trailing = body[eof+5:]
    return len(trailing) <= 2048 and not re.sub(rb'%[^\r\n]*', b'', trailing).strip()
