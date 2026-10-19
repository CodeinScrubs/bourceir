"""آزمون کشف اسناد با داده مصنوعی؛ بدون شبکه یا داده حساب."""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

from core import codal_search as search
from core.timeutils import now_tehran_iso
from data_collector.codal import CodalProvider


def request(**changes):
    values=dict(start='2024-01-01',end_exclusive='2024-04-01',cutoff='2025-03-21')
    values.update(changes);return search.search_request('آزمون',**values)


def row(**changes):
    value=dict(TracingNo=123,Symbol='آزمون',CompanyName='ناشر آزمون',Title='آگهی آزمونی',LetterCode='ن-۶۷',
        PublishDateTime='1402/12/23 12:00:00',SentDateTime='1402/12/23 11:59:59',IsEstimate=False,
        Url='/Reports/Decision.aspx?LetterSerial=test',PdfUrl='/DownloadFile.aspx?hs=test',AttachmentUrl='')
    value.update(changes);return value


def observation(rows=None,*,payload=None,body=None,req=None):
    req=request() if req is None else req
    payload=dict(IsAttacker=False,Letters=[row()] if rows is None else rows,Page=1,Total=1) if payload is None else payload
    body=json.dumps(payload,ensure_ascii=False).encode() if body is None else body
    meta=dict(source='codal',url=req['url'],requested_at=now_tehran_iso(),received_at=now_tehran_iso(),
        response_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest())
    return meta,body


def assess(rows=None,**kwargs):
    meta,body=observation(rows,**kwargs);return search.assess_search(meta,body,kwargs.get('req') or request())


def test_request_uses_real_date_arithmetic_and_encodes_symbol_as_one_parameter():
    result=search.search_request('نماد&PageNumber=99',start='2024-01-01',end_exclusive='2024-04-01',cutoff='2025-03-21',page=2,letter_type=28)
    query=parse_qs(urlsplit(result['url']).query)
    assert query=={'Symbol':['نماد&PageNumber=99'],'FromDate':['1402/10/11'],'ToDate':['1403/01/12'],
                  'PageNumber':['2'],'LetterType':['28']}


@pytest.mark.parametrize('changes',[{'page':True},{'page':0},{'page':101},{'letter_type':True},{'letter_type':0},
    {'start':'2024-02-30'},{'start':'2024-04-01'},{'end_exclusive':'2025-03-22'},{'cutoff':'not-a-date'}])
def test_bad_request_is_rejected_before_network(changes):
    with pytest.raises(ValueError):request(**changes)


def test_real_metadata_fields_stay_claims_and_never_make_a_signal():
    result=assess()
    assert result['status']=='coherent_observed_metadata'
    letter,=result['letters']
    assert letter['source_published_at']=='2024-03-13T12:00:00+03:30'
    assert letter['source_sent_at']=='2024-03-13T11:59:59+03:30'
    assert letter['letter_code']=='ن-67' and letter['url'].startswith('https://codal.ir/Reports/')
    assert all(result[k] is False and letter[k] is False for k in search.FALSE_FLAGS)


@pytest.mark.parametrize('native,field',[('1404/01/01 10:00:00','excluded_sealed_rows'),
    ('1403/01/13 10:00:00','excluded_outside_window_rows'),('1402/10/10 10:00:00','excluded_outside_window_rows')])
def test_date_guard_precedes_title_counter_and_link_interpretation(monkeypatch,native,field):
    monkeypatch.setattr(search,'_letter',lambda *_a:pytest.fail('out-of-window metadata interpreted'))
    result=assess([row(PublishDateTime=native,Title={'future':'not interpreted'},TracingNo='invalid',Url='file:///secret')])
    assert result[field]==1 and result['letters']==[] and result['status']=='incomplete_response'


def test_identity_guard_precedes_other_metadata(monkeypatch):
    monkeypatch.setattr(search,'_letter',lambda *_a:pytest.fail('wrong issuer interpreted'))
    result=assess([row(Symbol='ناشر دیگر')])
    assert result['status']=='conflicting_response' and result['letters']==[]


@pytest.mark.parametrize('native',['1402/12/23','1402/12/23 99:00:00',None,20240313,'1402/12/23 12:00:00+03:30'])
def test_unknown_native_time_is_not_midnight_or_today(native):
    result=assess([row(PublishDateTime=native)])
    assert result['invalid_date_rows']==1 and result['status']=='incomplete_response' and result['letters']==[]


def test_historical_daylight_offset_is_preserved_and_fixed_fallback_is_rejected(monkeypatch):
    req=request(start='2016-07-01',end_exclusive='2016-08-01')
    result=assess([row(PublishDateTime='1395/04/26 10:00:00',SentDateTime='1395/04/26 09:59:59')],req=req)
    assert result['letters'][0]['source_published_at']=='2016-07-16T10:00:00+04:30'
    monkeypatch.setattr(search,'TEHRAN_TZ',timezone.utc)
    assert assess()['invalid_date_rows']==1


def test_ambiguous_and_nonexistent_historical_clocks_are_not_silently_selected():
    # زمان‌های متعارض از خود پایگاه منطقه زمانی نصب‌شده پیدا می‌شوند، نه افست حدسی.
    from datetime import timedelta

    from core.timeutils import to_jalali
    found={}
    for month in (3,9):
        start=datetime(2016,month,15)
        for hour in range(15*24):
            wall=start+timedelta(hours=hour,minutes=30)
            a=wall.replace(tzinfo=search.TEHRAN_TZ,fold=0);b=wall.replace(tzinfo=search.TEHRAN_TZ,fold=1)
            if a.utcoffset()==b.utcoffset():continue
            valid=[v for v in (a,b) if v.astimezone(timezone.utc).astimezone(search.TEHRAN_TZ).replace(tzinfo=None)==wall]
            found['ambiguous' if valid else 'nonexistent']=to_jalali(wall,with_time=True,fa_digits=False)+':00'
    assert set(found)=={'ambiguous','nonexistent'}
    for native in found.values():
        with pytest.raises(ValueError):search._native_stamp(native)


@pytest.mark.parametrize('tracing',[True,1.5,'123',None,0])
def test_tracing_number_is_an_exact_native_integer(tracing):
    assert assess([row(TracingNo=tracing)])['letters']==[]


@pytest.mark.parametrize('field,value',[('Total',False),('Total','1'),('Total',1.2),('Page',-1),('Page',True)])
def test_native_metadata_counters_are_not_coerced(field,value):
    payload=dict(IsAttacker=False,Letters=[row()],Page=1,Total=1);payload[field]=value
    with pytest.raises(ValueError):assess(payload=payload)


def test_same_tracing_number_with_different_native_metadata_is_a_conflict():
    result=assess([row(),row(Title='اصلاحیه')],payload=dict(IsAttacker=False,Letters=[row(),row(Title='اصلاحیه')],Page=1,Total=2))
    assert result['status']=='conflicting_response' and result['conflicting_tracing_numbers']==[123] and result['letters']==[]


def test_exact_metadata_duplicates_keep_all_origins_and_cross_page_conflicts_choose_no_winner():
    result=assess(payload=dict(IsAttacker=False,Letters=[row(),row()],Page=1,Total=2))
    assert result['duplicate_rows']==1 and result['letters'][0]['source_indices']==[{'page':1,'index':0},{'page':1,'index':1}]
    another=assess([row(Title='اصلاحیه')],req=request(page=2))
    combined=search.merge_letters([result,another])
    assert combined['letters']==[] and combined['conflicting_tracing_numbers']==[123]


def test_native_number_and_string_changes_cannot_hide_as_exact_duplicates():
    result=assess(payload=dict(IsAttacker=False,Letters=[row(UnderSupervision=0),row(UnderSupervision='0')],Page=1,Total=2))
    assert result['conflicting_tracing_numbers']==[123]


@pytest.mark.parametrize('changes',[{'Url':'https://evil.invalid/Decision.aspx'}, {'Cancelled':True},
    {'SentDateTime':'1402/12/23 12:00:01'},{'Title':None}])
def test_unsafe_or_uninterpreted_metadata_never_becomes_a_document(changes):
    result=assess([row(**changes)])
    assert result['letters']==[] and result['status']=='incomplete_response'


def test_verification_empty_and_counter_conflicts_are_distinct():
    assert assess(payload=dict(IsAttacker=True,Letters=[row()],Page=1,Total=1))['status']=='verification_required'
    empty=assess(payload=dict(IsAttacker=False,Letters=[],Page=0,Total=0))
    assert empty['status']=='no_observed_letter_in_window' and empty['complete_history_verified'] is False
    assert assess(payload=dict(IsAttacker=False,Letters=[row()],Page=0,Total=0))['status']=='conflicting_response'


@pytest.mark.parametrize('body',[b'{"IsAttacker":false,"Letters":[],"Page":0,"Total":0,"Total":1}',
    b'{"IsAttacker":false,"Letters":[],"Page":0,"Total":NaN}',b'not-json',b'[]'])
def test_malformed_and_ambiguous_json_is_rejected(body):
    with pytest.raises(ValueError):assess(body=body)


def test_receipt_hash_and_request_url_are_checked_before_parsing():
    meta,body=observation();meta['body_sha256']='0'*64
    with pytest.raises(ValueError):search.assess_search(meta,body,request())
    meta,body=observation();meta['url']='https://codal.ir/anything'
    with pytest.raises(ValueError):search.assess_search(meta,body,request())
    with pytest.raises(ValueError):search.assess_search(None,body,request())
    assert search.assess_search(None,None,request())['status']=='unavailable'


class Response:
    def __init__(self,url,body=b'{}',status=200,headers=None):
        self.url=url;self.body=body;self.status_code=status;self.closed=False
        self.headers={'Content-Type':'application/json; charset=utf-8',**(headers or {})}
    def iter_content(self,chunk_size):yield self.body
    def close(self):self.closed=True


class Session:
    def __init__(self,*,body=b'{}',status=200,headers=None,redirect=False):
        self.headers={};self.body=body;self.status=status;self.response_headers=headers;self.redirect=redirect;self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs));self.response=Response(url+'bad' if self.redirect else url,self.body,self.status,self.response_headers)
        return self.response


def test_provider_preserves_bounded_bytes_and_does_not_follow_redirects():
    session=Session();provider=CodalProvider(session=session)
    observed=provider.get_historical_search_observation('آزمون',start='2024-01-01',end_exclusive='2024-04-01')
    assert observed['content']==b'{}' and observed['body_sha256']==hashlib.sha256(b'{}').hexdigest()
    assert session.response.closed and session.calls[0][1]['allow_redirects'] is False
    assert session.calls[0][1]['timeout'][0]<=10 and session.calls[0][1]['stream'] is True


@pytest.mark.parametrize('kwargs',[{'status':302},{'status':206},{'headers':{'Content-Type':'text/html'}},
    {'redirect':True},{'headers':{'Content-Length':str(search.MAX_BYTES+1)}},{'body':b'x'*(search.MAX_BYTES+1)}])
def test_failed_or_unbounded_provider_response_returns_unknown(kwargs):
    session=Session(**kwargs);result=CodalProvider(session=session).get_historical_search_observation('آزمون',
        start='2024-01-01',end_exclusive='2024-04-01')
    assert result is None and session.response.closed


def test_network_failure_is_not_an_empty_history():
    class Failed(Session):
        def get(self,*_a,**_kw):raise requests.ConnectionError('test network failure')
    assert CodalProvider(session=Failed()).get_historical_search_observation('آزمون',start='2024-01-01',end_exclusive='2024-04-01') is None
