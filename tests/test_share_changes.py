"""منبع و ارقام مصنوعی فقط در آزمون؛ ترتیب تاریخ، تعارض و دقت تعداد کنترل می‌شوند."""

import copy
import hashlib
import json

import pytest

from core import share_changes as source
from core.timeutils import now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from tests.test_dividend_evidence import Response
from tests.test_quote_context import SequenceSession

CODE='123'
START,END,CUTOFF='2024-01-01','2024-04-01','2025-03-21'


def row(**changes):
    result=dict(insCode=CODE,dEven=20240227,numberOfShareOld=1000,numberOfShareNew=1350,idn=0)
    result.update(changes)
    return result


def observation(rows=None,body=None):
    body=json.dumps({'instrumentShareChange':[row()] if rows is None else rows}).encode() if body is None else body
    clock=now_tehran_iso()
    return dict(source='tsetmc',ins_code=CODE,url=source.share_change_request(CODE)[1],requested_at=clock,
        received_at=clock,response_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest()),body


def assess(rows=None,body=None):
    meta,raw=observation(rows,body)
    return source.assess_share_changes(meta,raw,CODE,start=START,end_exclusive=END,cutoff=CUTOFF)


def test_native_counts_are_exact_observations_without_credit_or_adjusted_features():
    result=assess()
    assert result['status']=='coherent_observed' and len(result['events'])==1
    event=result['events'][0]
    assert event['share_multiplier']==dict(numerator=27,denominator=20)
    assert event['source_change_date']=='2024-02-27' and event['source_indices']==[0]
    assert all(result[k] is False for k in source.FALSE_FLAGS)
    assert result['original_publication_time'] is None
    assert not any(k in event for k in ['credit_date','ex_date','registration_date','adj_close'])


def test_large_native_numeric_literals_do_not_round_through_binary_float():
    raw=b'{"instrumentShareChange":[{"insCode":"123","dEven":20240227,"numberOfShareOld":9007199254740993.0,"numberOfShareNew":9007199254740994.0}]}'
    event=assess(body=raw)['events'][0]
    assert event['source_old_shares']==2**53+1 and event['source_new_shares']==2**53+2


def test_only_exact_duplicate_facts_collapse_and_conflicting_counts_remain_unresolved():
    a=assess([row(),row(numberOfShareOld='1000.0')])
    assert a['duplicate_rows']==1 and a['events'][0]['source_indices']==[0,1]
    b=assess([row(),row(numberOfShareNew=1400),row(dEven=20240305)])
    assert b['status']=='conflicting_response' and b['conflicting_dates']==['2024-02-27']
    assert len(b['records'])==3 and [e['source_change_date'] for e in b['events']]==['2024-03-05']
    facts=dict(status='supported_primary_fact',capital_multiplier=dict(numerator=27,denominator=20))
    assert source.compare_capital_ratio(b,facts,'2024-03-05')['status']=='not_comparable'


@pytest.mark.parametrize('bad',[0,-1,True,None,'NaN','Infinity','1.5','1,000','1e999',10**18+1])
def test_unknown_or_nonintegral_counts_do_not_become_a_ratio(bad):
    result=assess([row(numberOfShareOld=bad)])
    assert result['status']=='incomplete_response' and result['events']==[]


@pytest.mark.parametrize('field,value',[('insCode','456'),('insCode',0),('insCode',None),('dEven',20240230),('dEven',True)])
def test_missing_native_identity_or_date_never_falls_back_to_request_metadata(field,value,monkeypatch):
    monkeypatch.setattr(source,'_count',lambda *_:pytest.fail('financial interpretation before identity/date'))
    result=assess([row(**{field:value})])
    assert result['status'] in ('conflicting_response','incomplete_response') and result['events']==[]


def test_out_of_window_and_sealed_rows_are_removed_before_numeric_interpretation(monkeypatch):
    monkeypatch.setattr(source,'_count',lambda *_:pytest.fail('sealed or outside money interpreted'))
    result=assess([row(dEven=20250321,numberOfShareOld='poison'),row(dEven=20240100),
                   row(dEven=20240401,numberOfShareNew='poison'),row(dEven=20231231)])
    assert result['excluded_sealed_rows']==1 and result['excluded_outside_window_rows']==2
    assert result['invalid_date_rows']==1 and result['events']==[]


@pytest.mark.parametrize('change',[
    dict(source='other'),dict(ins_code='456'),dict(url='https://example.org/'),dict(response_bytes=True),
    dict(body_sha256='0'*64),dict(requested_at='2026-01-01T10:00:00'),
    dict(requested_at='2999-01-01T10:00:00+03:30')])
def test_receipt_is_checked_before_counts(change,monkeypatch):
    meta,body=observation();meta.update(change)
    monkeypatch.setattr(source,'_count',lambda *_:pytest.fail('invalid receipt money interpreted'))
    with pytest.raises(ValueError):source.assess_share_changes(meta,body,CODE,start=START,end_exclusive=END,cutoff=CUTOFF)


@pytest.mark.parametrize('body',[b'<html>no</html>',b'{"instrumentShareChange":null}',
    b'{"instrumentShareChange":[],"instrumentShareChange":[]}',
    b'{"instrumentShareChange":[{"dEven":NaN}]}'])
def test_invalid_json_or_schema_is_not_empty_history(body):
    with pytest.raises(ValueError):assess(body=body)


def test_empty_history_is_not_proof_of_zero_shares_or_no_capitalization():
    result=assess([])
    assert result['status']=='no_observed_event_in_window' and result['events']==[]
    assert not result['complete_history_verified']
    assert source.assess_share_changes(None,None,CODE,start=START,end_exclusive=END,cutoff=CUTOFF)['status']=='unavailable'


def test_unbounded_row_counts_and_bytes_are_rejected_before_count_interpretation(monkeypatch):
    monkeypatch.setattr(source,'_count',lambda *_:pytest.fail('oversized response money interpreted'))
    with pytest.raises(ValueError):assess([row() for _ in range(source.MAX_ROWS+1)])
    with pytest.raises(ValueError):assess(body=b' '*(source.MAX_BYTES+1))


def test_share_contraction_and_unchanged_count_are_source_facts_without_synthetic_actions():
    contracted=assess([row(numberOfShareNew=500)])['events'][0]
    assert contracted['share_multiplier']==dict(numerator=1,denominator=2)
    unchanged=assess([row(numberOfShareNew=1000)])['events'][0]
    assert unchanged['share_change_ratio']==dict(numerator=0,denominator=1)


def test_unknown_correction_or_revocation_fields_are_not_silently_ignored(monkeypatch):
    monkeypatch.setattr(source,'_count',lambda *_:pytest.fail('unsupported row money interpreted'))
    result=assess([row(cancelled=True)])
    assert result['status']=='incomplete_response' and result['events']==[]


def test_unknown_response_status_is_not_reported_as_empty_history():
    with pytest.raises(ValueError):assess(body=b'{"instrumentShareChange":[],"error":"unavailable"}')


def test_ratio_agreement_does_not_promote_issuer_event_class_or_holder_credit():
    facts=dict(status='supported_primary_fact',capital_multiplier=dict(numerator=27,denominator=20))
    result=source.compare_capital_ratio(assess(),facts,'2024-02-27')
    assert result['status']=='source_ratios_agree'
    assert not result['same_corporate_event_verified'] and not result['ordinary_share_class_verified']
    assert all(result[k] is False for k in source.FALSE_FLAGS)
    different=copy.deepcopy(facts);different['capital_multiplier']=dict(numerator=2,denominator=1)
    assert source.compare_capital_ratio(assess(),different,'2024-02-27')['status']=='source_ratios_differ'
    assert source.compare_capital_ratio(assess(),facts,'2024-02-28')['status']=='missing_explicit_native_event'


def test_provider_retains_native_bytes_with_fixed_https_url_and_no_redirects():
    _,body=observation()
    response=Response(body=body);session=SequenceSession([response]);provider=TsetmcProvider(session=session)
    result=provider.get_share_change_observation(CODE)
    assert result['content']==body and result['body_sha256']==hashlib.sha256(body).hexdigest()
    url,kwargs=session.calls[0]
    assert url==source.share_change_request(CODE)[1] and kwargs['allow_redirects'] is False and kwargs['stream'] is True


@pytest.mark.parametrize('code',[0,True,None,'0123','123/../x'])
def test_invalid_identifier_never_requests_network(code):
    session=SequenceSession([]);provider=TsetmcProvider(session=session)
    with pytest.raises(ValueError):provider.get_share_change_observation(code)
    assert not session.calls
