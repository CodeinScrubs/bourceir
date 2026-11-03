"""Exercise the real provider/capture boundary using only temporary test evidence."""

from types import SimpleNamespace

import pytest

from config import settings
from data_collector.tsetmc import TsetmcProvider
from ops.share_changes import capture_share_changes, review_share_changes
from tests.test_dividend_evidence import Response
from tests.test_quote_context import SequenceSession
from tests.test_share_changes import END, START, observation


def scope():
    return [dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300)]


def test_real_provider_receipt_can_be_captured_and_independently_reviewed(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path)
    _,body = observation()
    provider = TsetmcProvider(session=SequenceSession([Response(body=body)]))
    result = capture_share_changes(provider,scope(),start=START,end_exclusive=END)
    assert result['cases'][0]['assessment']['status']=='coherent_observed'
    checked = review_share_changes(result['folder'])
    assert checked['artifacts_verified'] and checked['assessments_recomputed']
    assert checked['cases'][0]['assessment']['events'][0]['source_new_shares']==1350
    assert checked['actual_entitlements_verified'] is False


@pytest.mark.parametrize('field,value',[('http_status',201),('http_status','200'),('http_status',True),
    ('http_status',None),('unrecognized_transport','ignored')])
def test_unknown_status_or_extra_receipt_fields_still_fail_closed(tmp_path,monkeypatch,field,value):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path)
    meta,body = observation()
    native = dict(meta,content=body)
    native[field] = value
    provider = SimpleNamespace(get_share_change_observation=lambda _:native)
    result = capture_share_changes(provider,scope(),start=START,end_exclusive=END)
    assert result['cases'][0]['assessment']['status']!='coherent_observed'
    assert not result['actual_entitlements_verified']


def test_legacy_receipts_remain_valid_and_current_receipts_are_not_mutated(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path)
    originals = []
    def current_receipt(_):
        meta,body = observation()
        native = dict(meta,content=body,http_status=200)
        originals.append((native,dict(native)))
        return native
    def legacy_receipt(_):
        meta,body = observation()
        return dict(meta,content=body)
    current = capture_share_changes(SimpleNamespace(get_share_change_observation=current_receipt),
        scope(),start=START,end_exclusive=END)
    old = capture_share_changes(SimpleNamespace(get_share_change_observation=legacy_receipt),
        scope(),start=START,end_exclusive=END)
    for field in ('status','events','records'):
        assert current['cases'][0]['assessment'][field]==old['cases'][0]['assessment'][field]
    assert originals[0][0]==originals[0][1]
