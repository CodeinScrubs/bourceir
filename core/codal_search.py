"""جست‌وجوی تاریخ‌دار کدال؛ کشف سند، تأیید انتشار یا حق دارنده نیست."""

import hashlib
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from core.codal_documents import public_document_url
from core.text import normalize_fa, normalize_symbol, to_en_digits
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date, jalali_to_iso, to_jalali

VERSION='historical-codal-search-v1'
MAX_BYTES=1048576
MAX_ROWS=100
FALSE_FLAGS=('historical_publication_verified','historical_identity_verified','complete_history_verified',
             'capital_change_verified','holder_credit_verified','feature_eligible','accepted_for_signals','profitability_proven')
OBSERVATION_KEYS={'source','url','requested_at','received_at','response_bytes','body_sha256'}
SOURCE_FIELDS={'AttachmentUrl','CompanyName','ExcelUrl','HasAttachment','HasExcel','HasHtml','HasPdf',
    'HasXbrl','IsEstimate','LetterCode','PdfUrl','PublishDateTime','SentDateTime','SuperVision','Symbol',
    'TedanUrl','Title','TracingNo','UnderSupervision','Url','XbrlUrl'}


def search_request(symbol, *, start, end_exclusive, cutoff, page=1, letter_type=None):
    if (not isinstance(symbol,str) or not normalize_symbol(symbol) or len(symbol)>64
            or any(c in symbol for c in ('\x00','\r','\n'))):
        raise ValueError('نماد صریح معتبر لازم است')
    if (any(not is_canonical_iso_date(d) for d in (start,end_exclusive,cutoff))
            or not start<end_exclusive<=cutoff):
        raise ValueError('پنجره مرتب پیش از مرز مهروموم لازم است')
    if type(page) is not int or not 1<=page<=100:
        raise ValueError('شماره صفحه باید یک تا صد باشد')
    if letter_type is not None and (type(letter_type) is not int or not 1<=letter_type<=999):
        raise ValueError('نوع نامه باید عدد صحیح مثبت یا نامعلوم باشد')
    symbol=normalize_symbol(symbol)
    if re.search(r'[\x00-\x1f\x7f]',symbol):
        raise ValueError('نماد دارای نویسه کنترل مجاز نیست')
    params=dict(Symbol=symbol,FromDate=to_jalali(start,fa_digits=False),
        ToDate=to_jalali(date.fromisoformat(end_exclusive)-timedelta(days=1),fa_digits=False),PageNumber=page)
    if letter_type is not None:params['LetterType']=letter_type
    return dict(symbol_fa=symbol,start=start,end_exclusive=end_exclusive,cutoff=cutoff,page=page,
        letter_type=letter_type,url='https://search.codal.ir/api/search/v2/q?'+urlencode(params))


class _Number(str):
    """توکن عددی JSON بدون تبدیل مالی، با حفظ تفاوت نوع رشته/عدد."""


def _pairs(items):
    result={}
    for key,value in items:
        if key in result:raise ValueError('کلید تکراری در JSON مجاز نیست')
        result[key]=value
    return result


def _constant(_value):
    raise ValueError('عدد نامتناهی در JSON مجاز نیست')


def _typed(value):
    if isinstance(value,_Number):return ['number',str(value)]
    if isinstance(value,dict):return ['object',[[k,_typed(v)] for k,v in sorted(value.items())]]
    if isinstance(value,list):return ['list',[_typed(v) for v in value]]
    return [type(value).__name__,value]


def _integer(value, *, zero=False):
    if not isinstance(value,_Number) or not re.fullmatch(r'[0-9]{1,18}',value):return None
    number=int(value)
    return number if number>=int(not zero) else None


def stamp(value):
    try:result=datetime.fromisoformat(value)
    except (ValueError,TypeError) as error:raise ValueError('زمان دارای منطقه زمانی لازم است') from error
    if result.tzinfo is None or result.utcoffset() is None:raise ValueError('زمان دارای منطقه زمانی لازم است')
    return result.astimezone(TEHRAN_TZ)


def _native_stamp(value):
    if type(value) is not str:raise ValueError('زمان بومی باید متن باشد')
    text=to_en_digits(value).strip()
    if not re.fullmatch(r'[0-9]{4}/[0-9]{2}/[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}',text):
        raise ValueError('زمان کامل بومی کدال لازم است')
    # افست ثابت جای تغییر ساعت تابستانی تاریخی ایران نمی‌نشیند.
    if not isinstance(TEHRAN_TZ,ZoneInfo) or TEHRAN_TZ.key!='Asia/Tehran':
        raise ValueError('داده منطقه زمانی تاریخی تهران در دسترس نیست')
    wall=datetime.fromisoformat(jalali_to_iso(text)).replace(tzinfo=None)
    candidates=[]
    for fold in (0,1):
        local=wall.replace(tzinfo=TEHRAN_TZ,fold=fold)
        back=local.astimezone(timezone.utc).astimezone(TEHRAN_TZ)
        if back.replace(tzinfo=None)==wall:candidates.append(local)
    if len({d.utcoffset() for d in candidates})!=1:
        raise ValueError('ساعت بومی در تغییر ساعت تاریخی یکتا نیست')
    return candidates[0].isoformat()


def _letter(row,published):
    sent=None if row.get('SentDateTime') in (None,'') else _native_stamp(row['SentDateTime'])
    if sent is not None and stamp(sent)>stamp(published):raise ValueError('زمان ارسال بعد از انتشار است')
    tracing=_integer(row.get('TracingNo'))
    if tracing is None:raise ValueError('شناسه رهگیری عدد صحیح بومی لازم است')
    title=row.get('Title');company=row.get('CompanyName');code=row.get('LetterCode')
    if type(title) is not str or not normalize_fa(title) or len(title)>2048:
        raise ValueError('عنوان متنی محدود لازم است')
    if company is not None and (type(company) is not str or len(company)>512):
        raise ValueError('نام ناشر متنی محدود لازم است')
    if code is not None and (type(code) is not str or len(code)>64):
        raise ValueError('کد نامه متنی محدود لازم است')
    links={}
    for source,target in [('Url','url'),('PdfUrl','pdf_url'),('AttachmentUrl','attachment_url')]:
        value=row.get(source)
        links[target]=None if value in (None,'') else public_document_url(value)
    if not links['url'] and not links['pdf_url']:raise ValueError('پیوند عمومی سند لازم است')
    native=json.dumps(_typed(row),ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return dict(tracing_no=tracing,symbol_fa=normalize_symbol(row['Symbol']),
        company_name=normalize_fa(company) if company else None,title=normalize_fa(title),
        letter_code=to_en_digits(normalize_fa(code)) if code else None,
        source_published_at=published,source_sent_at=sent,
        source_estimate=row.get('IsEstimate') if type(row.get('IsEstimate')) is bool else None,
        native_metadata_sha256=hashlib.sha256(native).hexdigest(),**links,**dict.fromkeys(FALSE_FLAGS,False))


def merge_letters(pages):
    """ردیف متعارض همان رهگیری، برنده انتخاب نمی‌کند؛ همه منشأها محفوظ‌اند."""
    groups=defaultdict(list)
    for page in pages:
        for record in page.get('records',[]):
            if record.get('status')=='observed_metadata':
                groups[record['letter']['tracing_no']].append((page['requested_page'],record))
    letters=[];conflicts=[];duplicates=0
    for tracing,items in sorted(groups.items()):
        if len({r['letter']['native_metadata_sha256'] for _,r in items})!=1:
            conflicts.append(tracing);continue
        duplicates+=len(items)-1
        letters.append(dict(items[0][1]['letter'],source_indices=[dict(page=p,index=r['source_index']) for p,r in items]))
    return dict(letters=letters,conflicting_tracing_numbers=conflicts,duplicate_rows=duplicates)


def assess_search(observation,content,request):
    if not isinstance(request,dict) or set(request)!={'symbol_fa','start','end_exclusive','cutoff','page','letter_type','url'}:
        raise ValueError('قرارداد درخواست کامل نیست')
    expected=search_request(request['symbol_fa'],**{k:request[k] for k in ('start','end_exclusive','cutoff','page','letter_type')})
    if json.dumps(request,sort_keys=True)!=json.dumps(expected,sort_keys=True):
        raise ValueError('قرارداد درخواست برابر نیست')
    result=dict(version=VERSION,requested_page=request['page'],status='unavailable',records=[],letters=[],
        row_count=None,total_claim=None,native_page_claim=None,invalid_date_rows=0,excluded_sealed_rows=0,
        excluded_outside_window_rows=0,conflicting_tracing_numbers=[],duplicate_rows=0,**dict.fromkeys(FALSE_FLAGS,False))
    if observation is None:
        if content is not None:raise ValueError('بدنه بدون رسید مجاز نیست')
        return result
    if (not isinstance(observation,dict) or set(observation)!=OBSERVATION_KEYS or observation['source']!='codal'
            or observation['url']!=request['url'] or type(observation['response_bytes']) is not int
            or not isinstance(content,bytes) or not 0<len(content)<=MAX_BYTES
            or observation['response_bytes']!=len(content) or hashlib.sha256(content).hexdigest()!=observation['body_sha256']):
        raise ValueError('بدنه یا هویت رسید جست‌وجو ناسازگار است')
    if stamp(observation['received_at'])<stamp(observation['requested_at']):raise ValueError('دریافت پیش از درخواست است')
    try:
        payload=json.loads(content.decode('utf-8-sig'),parse_int=_Number,parse_float=_Number,
            parse_constant=_constant,object_pairs_hook=_pairs)
    except (ValueError,UnicodeError,RecursionError) as error:raise ValueError('JSON جست‌وجو معتبر نیست') from error
    if not isinstance(payload,dict) or set(payload)!={'IsAttacker','Letters','Page','Total'}:
        raise ValueError('قرارداد ریشه جست‌وجو پشتیبانی نشده است')
    if payload['IsAttacker'] is True:
        result['status']='verification_required';return result
    if payload['IsAttacker'] is not False:raise ValueError('حالت تأیید انسانی معلوم نیست')
    rows=payload['Letters'];total=_integer(payload['Total'],zero=True);page=_integer(payload['Page'],zero=True)
    if not isinstance(rows,list) or len(rows)>MAX_ROWS or total is None or page is None:
        raise ValueError('فهرست محدود و شمار بومی جست‌وجو لازم است')
    result.update(row_count=len(rows),total_claim=total,native_page_claim=page)
    for index,row in enumerate(rows):
        try:published=_native_stamp(row.get('PublishDateTime')) if isinstance(row,dict) else None
        except (ValueError,TypeError,OverflowError):published=None
        if published is None:
            result['invalid_date_rows']+=1;result['records'].append(dict(source_index=index,status='unknown_native_date'));continue
        day=published[:10]
        if day>=request['cutoff']:result['excluded_sealed_rows']+=1;continue
        if not request['start']<=day<request['end_exclusive']:result['excluded_outside_window_rows']+=1;continue
        record=dict(source_index=index,source_published_at=published)
        if set(row)-SOURCE_FIELDS:record['status']='unsupported_native_fields'
        elif type(row.get('Symbol')) is not str or normalize_symbol(row['Symbol'])!=request['symbol_fa']:
            record['status']='native_identity_conflict'
        else:
            try:record.update(status='observed_metadata',letter=_letter(row,published))
            except (ValueError,TypeError,OverflowError,RecursionError):record['status']='invalid_letter_metadata'
        result['records'].append(record)
    result.update(merge_letters([result]))
    counts=Counter(r['status'] for r in result['records'])
    if total<len(rows) or counts['native_identity_conflict'] or result['conflicting_tracing_numbers']:
        result['status']='conflicting_response'
    elif (result['invalid_date_rows'] or result['excluded_sealed_rows'] or result['excluded_outside_window_rows']
          or counts['unsupported_native_fields'] or counts['invalid_letter_metadata']):
        result['status']='incomplete_response'
    else:result['status']='coherent_observed_metadata' if result['letters'] else 'no_observed_letter_in_window'
    return result
