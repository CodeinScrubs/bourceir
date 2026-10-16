"""فهرست قابل جست‌وجوی منابع عمومی و پیشنهادهای ضمیمه، بدون اجرای کد آن‌ها."""

import gzip
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from core.timeutils import now_tehran_iso
from data_collector.public_site import PublicSiteArchive, public_url

DIGITS = str.maketrans('۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩','01234567890123456789')
RULES = {
    'history_without_index': r'\[ih\]\s*\.\s*\w+',
    'assignment_in_if_review': r'\bif\s*\([^\n;]*?(?<![=!<>])=(?!=)',
    'external_execution_review': r'\b(?:eval\s*\(|exec\s*\(|subprocess\.|requests\.|fetch\s*\(|XMLHttpRequest\b)',
    'undefined_spelling_review': r'\b(?:LowesLow|V3D)\b',
    'invented_share_estimate_review': r'avg_val_20d\s*\*\s*120|tot_shares\s*=\s*max\(1e8',
    'relaxed_threshold_review': r'rally_dec\s*\*\s*0\.75|dip_dec\s*\*\s*0\.5',
    'unknown_to_zero_review': r'fillna\(0(?:\.0)?\)|max\(1(?:\.0)?\s*,',
}


def text_catalog(text):
    lines=text.splitlines()
    starts=[]
    for i,line in enumerate(lines):
        match=re.match(r'^فیلتر\s+شماره\s+([0-9۰-۹٠-٩]+)\b',line.strip())
        if match:
            starts.append((i,int(match[1].translate(DIGITS))))
    sections=[]
    for index,(start,number) in enumerate(starts):
        end=starts[index+1][0] if index+1<len(starts) else len(lines)
        body='\n'.join(lines[start:end])
        sections.append(dict(number=number,start_line=start+1,end_line=end,title=lines[start].strip(),
            sha256=hashlib.sha256(body.encode()).hexdigest(),signals_enabled=False))
    findings=[]
    for kind,pattern in RULES.items():
        for match in re.finditer(pattern,text):
            line=text.count('\n',0,match.start())+1
            findings.append(dict(kind=kind,line=line,excerpt=match[0][:180],
                status='static review candidate, not a compiler result or profitability finding'))
    return dict(lines=len(lines),sections=sections,findings=sorted(findings,key=lambda r:r['line']),
        untrusted_code_executed=False,all_filters_semantically_verified=False,profitability_verified=False)


def catalog_claim_files(files, output):
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    sources=[]
    for index,name in enumerate(files,1):
        data=Path(name).read_bytes()
        text=data.decode('utf-8-sig')
        digest=hashlib.sha256(data).hexdigest()
        (output/(digest+'.txt.gz')).write_bytes(gzip.compress(data,mtime=0))
        result=text_catalog(text)
        sources.append(dict(source=index,filename=Path(name).name,sha256=digest,**result))
    catalog=dict(created_at=now_tehran_iso(),sources=sources,source_count=len(sources),
        indexed_lines=sum(r['lines'] for r in sources),automatic_index_is_not_a_line_by_line_human_audit=True)
    (output/'claims-catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    return catalog


def catalog_site(archive_path):
    """تطبیق هش بدنه و ساخت فهرست محلی؛ متن صفحات، ادعای منبع است نه حقیقت بازار."""
    root=Path(archive_path)
    records=[json.loads(line) for line in (root/'sources.jsonl').read_text(encoding='utf-8').splitlines()]
    if not records:
        raise ValueError('آرشیو سایت خالی است')
    base=records[0]['url'].split('/robots.txt')[0].rstrip('/')+'/'
    # متن مشتق‌شده خوانده نمی‌شود؛ از بدنه‌ای که هش آن تطبیق دارد دوباره ساخته می‌شود.
    archive=PublicSiteArchive(base,root,verify_derived_text=False)
    latest={url:record for record in archive.records if (url:=public_url(record['url'],base))}
    pages=[]
    origins=defaultdict(list)
    for url,record in latest.items():
        if record.get('status')!=200 or record.get('error'):
            continue
        page={k:record.get(k) for k in ('category','title','sha256','body_file','captured_at','bytes')}
        page['url']=url
        if record.get('text_file'):
            path=(root/record['text_file']).resolve()
            if path.parent!=root.resolve():
                raise ValueError('مسیر متن استخراج‌شده نامعتبر است')
            # متن از بدنه دارای هش دوباره استخراج می‌شود؛ فایل متن مرجع اعتماد نیست.
            from data_collector.public_site import PublicPage
            parser=PublicPage()
            parser.feed(gzip.decompress((root/record['body_file']).read_bytes()).decode('utf-8',errors='replace'))
            text='\n'.join(parser.parts)
            page.update(visible_text_chars=len(text),http_shell_possible=len(text)<500,
                visible_code_marker=bool(re.search(r'true\s*==\s*function|def\s+\w+\s*\(',text)),
                formula_execution_verified=False,profitability_verified=False)
        pages.append(page)
        for host in record.get('referenced_origins',[]):
            origins[host].append(url)
    catalog=dict(created_at=now_tehran_iso(),base_url=base,pages=pages,
        categories=dict(Counter(p['category'] for p in pages)),referenced_origins=dict(origins),
        runtime_api_calls_verified=False,private_backend_verified=False,formula_profitability_verified=False)
    (root/'knowledge-catalog.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# نقشه منابع عمومی الگوسهام','',f'فهرست {len(pages)} نشانی موفق و یکتا پس از یکسان‌سازی URL؛ زمان {catalog["created_at"]}.',
        '', 'HTTP 200 یا وجود نام یک سرویس در JavaScript، اثبات کارکرد کامل آن سرویس، دسترسی به فرمول خصوصی یا سودآوری نیست.', '',
        '| بخش | تعداد |','|---|---:|']
    lines += [f'| {kind} | {count} |' for kind,count in catalog['categories'].items()]
    lines += ['', '| عنوان / نشانی | بخش | متن کوتاه یا پوسته احتمالی | نشانگر کد |','|---|---|---|---|']
    for p in pages:
        if p['category'] not in ('symbol','script'):
            title=(p.get('title') or p['url']).replace('|','/')
            lines.append(f'| [{title}]({p["url"]}) | {p["category"]} | {p.get("http_shell_possible")} | {p.get("visible_code_marker")} |')
    (root/'knowledge-map-fa.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    return catalog
