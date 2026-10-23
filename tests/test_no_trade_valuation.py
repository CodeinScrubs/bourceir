"""Synthetic native publications only; no source bytes or account data are operational fixtures."""
import base64
import copy
import hashlib
import json

import pytest

from core.daily_publications import publication_request
from core.no_trade_valuation import FALSE_FLAGS, parse_no_trade_publication, parse_no_trade_receipt

CODE='12345678901234567'
DAY='2024-01-02'
NOW='2024-04-01T14:00:00+03:30'


def receipt(day=DAY, *, native_changes=None, code=CODE, symbol='آزمون'):
    native=dict(insCode=code,dEven=int(day.replace('-','')),pClosing=100.,priceYesterday=100.,
        qTotTran5J=0,qTotCap=0,zTotTran=0)
    native.update(native_changes or {})
    body=json.dumps(dict(closingPriceDaily=native)).encode()
    source=dict(source='tsetmc',ins_code=code,requested_date=day,kind='price',price_unit='Rial',
        url=publication_request(code,day,'price')[1],requested_at=NOW,received_at=NOW,
        response_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest(),
        body_base64=base64.b64encode(body).decode())
    return dict(version='daily-publication-archive-v1',
        task=dict(symbol_id=1,symbol=symbol,ins_code=code,date=day,kind='price',instrument_type_code=300),
        attempted_at=NOW,completed_at=NOW,provider_error_type=None,source=source,parsed={'ignored':'cached'})


def publication(record):
    source=record['source']
    return dict(source,content=base64.b64decode(source['body_base64']))


def test_flat_mark_is_source_bound_but_never_a_candle_or_executable():
    raw=receipt()
    before=copy.deepcopy(raw)
    parsed=parse_no_trade_receipt(raw)
    assert parsed['status']=='observed_flat_mark' and parsed['flat_mark_eligible']
    assert parsed['official_close_rial']==parsed['reference_rial']==100
    assert parsed['factor']==1 and parsed['reported_volume']==parsed['reported_value_rial']==parsed['reported_trade_count']==0
    assert parsed['native_identity_verified'] and parsed['native_date_verified']
    assert all(parsed[key] is False for key in FALSE_FLAGS)
    assert 'open_raw' not in parsed and 'high_raw' not in parsed and 'low_raw' not in parsed
    assert before==raw


def test_derived_factor_uses_single_market_return_rule(monkeypatch):
    from core import market_rules
    actual=market_rules.pit_daily_returns
    seen=[]
    def checked(frame):
        seen.append(frame.to_dict('records'))
        return actual(frame)
    monkeypatch.setattr(market_rules,'pit_daily_returns',checked)
    assert parse_no_trade_receipt(receipt())['factor']==1
    assert seen==[[{'close_final_raw':100.,'ref_price_raw':100.}]]


@pytest.mark.parametrize('field',['pClosing','priceYesterday'])
@pytest.mark.parametrize('value',[0,None,101.])
def test_nonpositive_missing_or_nonflat_mark_never_gets_a_factor(field,value):
    parsed=parse_no_trade_receipt(receipt(native_changes={field:value}))
    assert parsed['status']=='unusable_official_mark' and not parsed['flat_mark_eligible']
    assert parsed['factor'] is None


@pytest.mark.parametrize('field',['pClosing','priceYesterday'])
@pytest.mark.parametrize('value',[True,-1,'100',2**53,float('nan'),float('inf')])
def test_invalid_native_mark_fails_closed(field,value):
    with pytest.raises(ValueError):
        parse_no_trade_receipt(receipt(native_changes={field:value}))


def test_positive_trading_response_is_not_a_zero_activity_mark():
    parsed=parse_no_trade_receipt(receipt(native_changes=dict(qTotTran5J=1,qTotCap=100,zTotTran=1,
        priceFirst=100,priceMin=100,priceMax=100,pDrCotVal=100)))
    assert parsed['status']=='observed_trading' and not parsed['flat_mark_eligible']
    assert parsed['factor'] is None


@pytest.mark.parametrize('field,value',[('insCode','0'),('dEven',20240101),('qTotTran5J',1),
    ('qTotCap',1),('zTotTran',1),('qTotTran5J',True)])
def test_native_binding_and_activity_must_match(field,value):
    with pytest.raises(ValueError):
        parse_no_trade_receipt(receipt(native_changes={field:value}))


@pytest.mark.parametrize('field,value',[('price_unit','Toman'),('body_sha256','0'*64),
    ('response_bytes',True),('requested_at','2024-04-02T14:00:00+03:30'),('received_at','2024-04-01T14:00:00')])
def test_receipt_source_provenance_is_required(field,value):
    raw=receipt(); raw['source'][field]=value
    with pytest.raises(ValueError):
        parse_no_trade_receipt(raw)


def test_cached_claims_do_not_override_bytes():
    raw=receipt(native_changes={'pClosing':102})
    raw['parsed']={'status':'observed_flat_mark','factor':1,'flat_mark_eligible':True,'accepted_for_signals':True}
    parsed=parse_no_trade_receipt(raw)
    assert not parsed['flat_mark_eligible'] and parsed['factor'] is None and not parsed['accepted_for_signals']


def test_unavailable_source_stays_unknown():
    raw=receipt(); raw['source']=None
    parsed=parse_no_trade_receipt(raw)
    assert parsed['status']=='source_unavailable' and not parsed['flat_mark_eligible']


@pytest.mark.parametrize('change',[{'date':'2024-02-30'},{'date':'2030-01-01'},{'ins_code':'01'},
    {'instrument_type_code':309},{'instrument_type_code':True},{'symbol':'ازمونح1'}])
def test_even_unavailable_receipts_must_have_valid_scope(change):
    raw=receipt(); raw['source']=None; raw['task'].update(change)
    with pytest.raises(ValueError):
        parse_no_trade_receipt(raw)


def test_direct_publication_parser_uses_explicit_time_bounds():
    raw=receipt()
    with pytest.raises(ValueError):
        parse_no_trade_publication(publication(raw),CODE,DAY,
            requested_after='2024-04-01T14:01:00+03:30',received_by='2024-04-01T14:02:00+03:30')
