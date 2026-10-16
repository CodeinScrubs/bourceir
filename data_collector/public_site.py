"""آرشیو قابل ادامه صفحات عمومی؛ کد سایت و فیلترهای آن اجرا نمی‌شوند."""

import gzip
import hashlib
import json
import os
import re
import time
import uuid
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

import requests

from core.file_lock import exclusive_file
from core.timeutils import now_tehran_iso

AGENT = 'BRS-PublicResearch/0.7.10'
BLOCKED = re.compile(r'/(?:api|auth|login|logout|register|signup|signin|payment|checkout|account)(?:/|$)', re.I)


class PublicPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.skip = 0
        self.in_title = False
        self.parts, self.links, self.scripts, self.title = [], [], [], []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ('script', 'style'):
            self.skip += 1
        if tag == 'script' and attrs.get('src'):
            self.scripts.append(attrs['src'])
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag == 'title':
            self.in_title = True

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.skip = max(0, self.skip - 1)
        if tag == 'title':
            self.in_title = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def public_url(value, base):
    """فقط همان مبدأ؛ مسیر ورود/پرداخت، query و نام کاربری کنار گذاشته می‌شوند."""
    url, origin = urlsplit(urljoin(base, value)), urlsplit(base)
    path = unquote(url.path)
    if (url.scheme != 'https' or url.netloc != origin.netloc or url.username or url.password or
        url.query or BLOCKED.search(path) or re.search(r'%[\da-fA-F]{2}',path)):
        return None
    return urlunsplit((url.scheme, url.netloc, quote(path or '/',safe='/'), '', ''))


def category(url):
    path = unquote(urlsplit(url).path)
    if path.endswith('.js'):
        return 'script'
    if '/Namad/' in path:
        return 'symbol'
    if '/public-filters/' in path:
        return 'public_filter'
    if '/python-filters/' in path:
        return 'python_education'
    if '/filters/' in path:
        return 'filter_education'
    if path.endswith('/guide'):
        return 'guide'
    return 'other_public'


class PublicSiteArchive:
    def __init__(self, base_url, output, *, interval=1., max_pages=4000, session=None, max_bytes=8_000_000,
                 verify_derived_text=True):
        origin = urlsplit(base_url)
        if origin.scheme != 'https' or not origin.hostname or origin.username or origin.password or origin.query:
            raise ValueError('نشانی HTTPS عمومی بدون رمز یا query لازم است')
        if interval < .5 or max_pages < 1 or max_bytes < 1:
            raise ValueError('فاصله حداقل نیم ثانیه و بودجه مثبت لازم است')
        if type(verify_derived_text) is not bool:
            raise ValueError('سیاست خواندن متن مشتق‌شده باید بولی باشد')
        self.verify_derived_text = verify_derived_text
        self.base = urlunsplit((origin.scheme, origin.netloc, '/', '', ''))
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.interval, self.max_pages, self.max_bytes = interval, max_pages, max_bytes
        self.session = session or requests.Session()
        self.records, self.last_request, self.stopped = [], None, None
        self.code_sha256 = hashlib.sha256(Path(__file__).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
        self.manifest = self.output / 'sources.jsonl'
        with exclusive_file(self.output / '.crawl.lock'):
            self._reload_records()

    def _reload_records(self):
        self._recover_pending()
        contents = self.manifest.read_bytes() if self.manifest.exists() else b''
        self.records = self._read_records(contents)
        self._manifest_size = len(contents)
        self._manifest_digest = hashlib.sha256(contents)

    def _validate_record(self, record):
        if not isinstance(record, dict) or not isinstance(record.get('url'), str):
            raise ValueError('قالب رکورد آرشیو صفحه نامعتبر است')
        if urlsplit(record['url']).netloc != urlsplit(self.base).netloc:
            raise ValueError('این آرشیو متعلق به مبدأ دیگری است')
        if record.get('record_id') is not None and (not isinstance(record['record_id'], str) or not record['record_id']):
            raise ValueError('شناسه رکورد آرشیو نامعتبر است')
        for field, digest in (('body_file', 'sha256'), ('text_file', 'text_sha256')):
            if not record.get(field):
                continue
            path = (self.output / record[field]).resolve()
            if path.parent != self.output.resolve():
                raise ValueError('مسیر فایل آرشیو صفحه نامعتبر است')
            if field == 'text_file' and (digest not in record or not self.verify_derived_text):
                continue  # متن مشتق‌شده قدیمی هش مستقل نداشته است؛ تأیید تازه‌ای برای آن نمی‌سازیم.
            with gzip.open(path, 'rb') as stream:
                data = stream.read(self.max_bytes + 1)
            if len(data) > self.max_bytes or hashlib.sha256(data).hexdigest() != record.get(digest):
                raise ValueError('آرشیو صفحه با هش ثبت‌شده سازگار نیست')

    def _read_records(self, contents):
        if contents and not contents.endswith(b'\n'):
            raise ValueError('انتهای فهرست ناقص است و رسید بازیابی ندارد؛ فایل حفظ شد')
        records, identities = [], set()
        for line in contents.decode('utf-8').splitlines():
            record = json.loads(line)
            self._validate_record(record)
            identity = record.get('record_id')
            if identity is not None:
                if not isinstance(identity, str) or not identity or identity in identities:
                    raise ValueError('شناسه رکورد آرشیو نامعتبر یا تکراری است')
                identities.add(identity)
            records.append(record)
        return records

    def _recover_pending(self):
        pending = self.output / '.pending-source-record.json'
        if not pending.exists():
            return
        receipt = json.loads(pending.read_bytes())
        if (not isinstance(receipt, dict) or receipt.get('version') != 'public-source-append-v1'
                or type(receipt.get('prefix_bytes')) is not int or receipt['prefix_bytes'] < 0
                or not isinstance(receipt.get('line'), str)):
            raise ValueError('رکورد در انتظار آرشیو معتبر نیست؛ فایل‌ها حفظ شدند')
        line = receipt['line'].encode('utf-8')
        record = json.loads(line)
        if (not line.endswith(b'\n') or len(line.splitlines()) != 1 or not isinstance(record, dict)
                or not record.get('record_id') or hashlib.sha256(line).hexdigest() != receipt.get('line_sha256')):
            raise ValueError('محتوای رکورد در انتظار با رسید سازگار نیست؛ فایل‌ها حفظ شدند')
        self._validate_record(record)
        contents = self.manifest.read_bytes() if self.manifest.exists() else b''
        offset = receipt['prefix_bytes']
        prefix, fragment = contents[:offset], contents[offset:]
        if (len(prefix) != offset or hashlib.sha256(prefix).hexdigest() != receipt.get('prefix_sha256')
                or not line.startswith(fragment)):
            raise ValueError('فهرست با پیشوند محفوظ یا رکورد در انتظار ناسازگار است؛ فایل‌ها حفظ شدند')
        prior = self._read_records(prefix)
        if any(row.get('record_id') == record['record_id'] for row in prior):
            raise ValueError('رکورد در انتظار قبلاً در پیشوند ثبت شده است؛ فایل‌ها حفظ شدند')
        if fragment != line:
            self._publish_bytes(self.manifest, prefix + line)
        pending.unlink()

    def _publish_bytes(self, path, data):
        temporary = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temporary.open('xb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, record):
        record['collector_code_sha256'] = self.code_sha256
        record['record_id'] = uuid.uuid4().hex
        line = (json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
        pending = self.output / '.pending-source-record.json'
        if pending.exists():
            raise ValueError('ثبت قبلی تکمیل نشده؛ آرشیو را با قفل مشترک دوباره باز کنید')
        self._validate_record(record)
        receipt = dict(version='public-source-append-v1', prefix_bytes=self._manifest_size,
                       prefix_sha256=self._manifest_digest.hexdigest(), line=line.decode('utf-8'),
                       line_sha256=hashlib.sha256(line).hexdigest())
        self._publish_bytes(pending, json.dumps(receipt, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        self._append_line(line)
        pending.unlink()
        self._manifest_size += len(line)
        self._manifest_digest.update(line)
        self.records.append(record)

    def _append_line(self, line):
        with self.manifest.open('ab') as stream:
            if stream.seek(0, 2) != self._manifest_size:
                raise ValueError('اندازه فهرست زیر قفل تغییر کرده است؛ فایل‌ها حفظ شدند')
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())

    def _get(self, url):
        elapsed = time.monotonic() - self.last_request if self.last_request is not None else self.interval
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_request = time.monotonic()
        record = dict(url=url, requested_at=now_tehran_iso(), category=category(url))
        try:
            response = self.session.get(url, timeout=(8, 20), headers={'User-Agent': AGENT},
                                        allow_redirects=False, stream=True)
            try:
                record.update(status=response.status_code, content_type=response.headers.get('Content-Type', ''))
                if response.status_code in (429, 503):
                    self.stopped = f'HTTP {response.status_code}'
                if 300 <= response.status_code < 400:
                    record['redirect'] = public_url(response.headers.get('Location', ''), url)
                    body = b''
                else:
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ValueError('پاسخ از سقف مجاز آرشیو بزرگ‌تر است')
                        chunks.append(chunk)
                    body = b''.join(chunks)
            finally:
                response.close()
            key = hashlib.sha256(body).hexdigest()
            body_file = key + '.body.gz'
            self._publish_bytes(self.output / body_file, gzip.compress(body, mtime=0))
            record.update(sha256=key, body_file=body_file, bytes=len(body), captured_at=now_tehran_iso())
            content = body.decode('utf-8', errors='replace')
            if response.status_code == 200 and 'html' in record['content_type']:
                parsed = PublicPage()
                parsed.feed(content)
                text = '\n'.join(parsed.parts).encode('utf-8')
                text_digest = hashlib.sha256(text).hexdigest()
                record.update(title=''.join(parsed.title).strip(), text_file=key+'.'+text_digest+'.text.gz', text_sha256=text_digest,
                    links=sorted({u for v in parsed.links if (u := public_url(v, url))}),
                    scripts=sorted({u for v in parsed.scripts if (u := public_url(v, url))}))
                self._publish_bytes(self.output / record['text_file'], gzip.compress(text, mtime=0))
            if record['category'] == 'script':
                record['referenced_origins'] = sorted({urlsplit(v).netloc for v in re.findall(r'(?:https?|wss)://[\w.-]+', content)})
        except (requests.RequestException, ValueError) as error:
            body = b''
            record.update(error=str(error), captured_at=now_tehran_iso())
        self._record(record)
        return record, body

    def crawl(self, *, progress=None):
        with exclusive_file(self.output/'.crawl.lock'):
            result = self._crawl(progress=progress)
            self._publish_bytes(self.output / 'coverage.json', json.dumps(result, ensure_ascii=False, indent=2).encode('utf-8'))
            return result

    def _crawl(self, *, progress=None):
        # شیء قدیمی نیز زیر همان قفل، نتیجه نویسنده قبلی و وضعیت توقف تازه را می‌خواند.
        self._reload_records()
        self.stopped = None
        robots_record, robots_body = self._get(urljoin(self.base, 'robots.txt'))
        robots = RobotFileParser()
        if robots_record.get('error') or robots_record.get('status') not in (200, 404):
            self.stopped = 'robots_unavailable'
            return self.summary()
        robots.parse(robots_body.decode('utf-8', errors='replace').splitlines() if robots_record['status'] == 200 else [])
        self.interval = max(self.interval, robots.crawl_delay(AGENT) or 0)
        sitemap_urls = robots.site_maps() or [urljoin(self.base, 'sitemap.xml')]
        queue, maps, sitemap_members = deque([self.base]), deque(sitemap_urls), set()
        seen_maps = set()
        while maps and not self.stopped:
            if len(seen_maps) >= 100:
                self.stopped = 'sitemap_budget'
                break
            address = public_url(maps.popleft(), self.base)
            if not address or address in seen_maps or not robots.can_fetch(AGENT, address):
                continue
            seen_maps.add(address)
            record, body = self._get(address)
            if record.get('status') != 200 or record.get('error'):
                continue
            try:
                root = ElementTree.fromstring(body)
                values = [e.text for e in root.iter() if e.tag.rsplit('}', 1)[-1] == 'loc' and e.text]
                if root.tag.rsplit('}', 1)[-1] == 'sitemapindex':
                    maps.extend(values)
                else:
                    sitemap_members.update(u for v in values if (u := public_url(v, self.base)))
                    queue.extend(sorted(sitemap_members))
            except ElementTree.ParseError:
                self._record(dict(url=address,category='sitemap',status=record['status'],
                    error='sitemap_parse_failed',captured_at=now_tehran_iso()))
        known, skipped = set(), set()
        for record in self.records:
            canonical = public_url(record['url'],self.base)
            if record.get('status') == 200 and not record.get('error'):
                if canonical:
                    known.add(canonical)
                queue.extend(u for v in record.get('links', []) if (u:=public_url(v,self.base)))
                queue.extend(u for v in record.get('scripts', []) if (u:=public_url(v,self.base)))
            if record.get('redirect'):
                queue.append(record['redirect'])
        attempted, fetched = set(known), 0
        while queue and not self.stopped:
            url = queue.popleft()
            if url in attempted:
                continue
            attempted.add(url)
            if not robots.can_fetch(AGENT, url):
                skipped.add(url)
                continue
            if fetched >= self.max_pages:
                self.stopped = 'page_budget'
                break
            record, _ = self._get(url)
            fetched += 1
            queue.extend(record.get('links', []))
            queue.extend(record.get('scripts', []))
            if record.get('redirect'):
                queue.append(record['redirect'])
            if progress and fetched % 25 == 0:
                progress(self.summary(sitemap_members, skipped))
        result = self.summary(sitemap_members, skipped)
        return result

    def summary(self, sitemap_members=(), skipped=()):
        latest = {u:r for r in self.records if (u:=public_url(r['url'],self.base))}
        success = {u for u, r in latest.items() if r.get('status') == 200 and not r.get('error')}
        return dict(base_url=self.base, evaluated_at=now_tehran_iso(), discovered=len(latest), successful=len(success),
            sitemap_urls=len(sitemap_members), sitemap_successful=len(set(sitemap_members) & success),
            sitemap_unresolved=sorted(set(sitemap_members)-success), skipped_by_robots=sorted(skipped),
            categories=dict(Counter(r['category'] for r in latest.values())), stopped=self.stopped,
            backend_access_verified=False, javascript_execution_verified=False, filter_profitability_verified=False)
