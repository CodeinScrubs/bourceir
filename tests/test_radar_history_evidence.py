"""The retained source body must be the body which produced the analyzed bars."""

import hashlib
import json

import pandas as pd
import pytest

from data_collector.daily_history import VERSION, history_request, normalize_observation
from ops import opportunity_radar as radar
from ops.report_files import read_private_report
from tests.test_opportunity_radar import (
    PublicNews,
    PublicProvider,
    identity,
)
from tests.test_opportunity_radar import (
    radar_root as radar_root,
)


class NativeProvider(PublicProvider):
    supports_history_observations=True

    def __init__(self, *, malformed=False, mismatch=False):
        super().__init__()
        self.malformed=malformed
        self.mismatch=mismatch

    def get_symbol_history(self,*args,**kwargs):
        raise AssertionError('A second request for normalized values is forbidden')

    def get_symbol_history_observation(self,code,last_n):
        self.calls.append(code)
        bound='999' if self.mismatch else code
        _,url=history_request(bound,last_n)
        rows=PublicProvider.get_symbol_history(PublicProvider(),code,last_n).copy()
        rows['trade_count']=1
        fields=dict(open_raw='priceFirst',high_raw='priceMax',low_raw='priceMin',close_raw='pDrCotVal',
                    close_final_raw='pClosing',ref_price_raw='priceYesterday',volume='qTotTran5J',
                    value='qTotCap',trade_count='zTotTran')
        native=[]
        for row in rows.to_dict('records'):
            native.append(dict(insCode=bound,dEven=int(row['date'].replace('-','')),
                **{target:row[source] for source,target in fields.items()}))
        native[-1]['priceFirst']=0  # Observed source defect must remain unknown.
        body=(b'{"closingPriceDaily":' if self.malformed else
              json.dumps(dict(closingPriceDaily=native)).encode())
        return dict(version=VERSION,source='tsetmc',price_unit='Rial',ins_code=bound,last_n=last_n,url=url,
            requested_at='2023-04-10T14:00:00+03:30',received_at='2023-04-10T14:00:01+03:30',
            normalization_as_of='2023-04-10T14:00:01+03:30',http_status=200,
            response_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest(),content=body)


def test_native_body_is_archived_before_zero_open_normalization_and_replays(radar_root):
    frozen=radar.activate(radar_root,seed=[identity()])
    provider=NativeProvider()
    out,_=radar.collect_cycle(radar_root,frozen,provider=provider,codal=PublicNews(),price_scan=True,bootstrap=True)
    assert len(provider.calls)==25
    assert all(row['status']=='invalid_current_bar' for row in out['rows'])
    folder=radar_root/out['artifacts_folder']
    evidence=read_private_report(folder/'history-1.json')
    assert evidence['status']=='native' and evidence['observation']['body_base64']
    assert 'history-1.json' in out['artifacts_sha256']
    checked=radar.verify(radar_root)
    assert checked['replayed_native_histories']==25 and checked['replayed_instruments']==25


@pytest.mark.parametrize('options,status',[
    (dict(malformed=True),'normalization_rejected'),(dict(mismatch=True),'request_mismatch')])
def test_bad_native_payload_retained_but_not_used_and_rejection_replays(radar_root,options,status):
    frozen=radar.activate(radar_root,seed=[identity()])
    out,_=radar.collect_cycle(radar_root,frozen,provider=NativeProvider(**options),codal=PublicNews(),
                              price_scan=True,bootstrap=True)
    folder=radar_root/out['artifacts_folder']
    assert read_private_report(folder/'history-1.json')['status']==status
    assert all(row['status']=='unavailable' for row in out['rows'])
    assert radar.verify(radar_root)['replayed_native_histories']==0


def test_legacy_provider_explicitly_lacks_raw_evidence(radar_root):
    frozen=radar.activate(radar_root,seed=[identity()])
    out,_=radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    folder=radar_root/out['artifacts_folder']
    assert read_private_report(folder/'history-1.json')==dict(status='unsupported',ins_code='1',observation=None)
    assert radar.verify(radar_root)['replayed_native_histories']==0


def test_archive_write_failure_prevents_cycle_commit(radar_root,monkeypatch):
    frozen=radar.activate(radar_root,seed=[identity()])
    original=radar.write_private_report
    def write(path,value):
        if path.name.startswith('history-'):
            raise OSError('disk write failed')
        return original(path,value)
    monkeypatch.setattr(radar,'write_private_report',write)
    with pytest.raises(OSError,match='disk write failed'):
        radar.collect_cycle(radar_root,frozen,provider=NativeProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    assert radar.status(radar_root)['latest'] is None


def test_concurrent_native_providers_cannot_cross_bind_response():
    class CrossBound(NativeProvider):
        def __init__(self):
            super().__init__(mismatch=True)
    receipts={}
    frames,problems=radar._histories([identity(1),identity(2)],None,CrossBound,
                                  archive=lambda code,item:receipts.update({code:item}))
    assert set(problems)=={'1','2'} and all(value is None for value in frames.values())
    assert all(item['status']=='request_mismatch' for item in receipts.values())


def test_same_native_response_deterministically_retains_unknown_open():
    observation=NativeProvider().get_symbol_history_observation('1',100)
    first=normalize_observation(observation)
    second=normalize_observation(observation)
    pd.testing.assert_frame_equal(first,second,check_exact=True)
    assert pd.isna(first.iloc[-1].open_raw)


def test_verify_rejects_changed_normalized_values_even_when_flags_would_stay_unknown(radar_root,monkeypatch):
    frozen=radar.activate(radar_root,seed=[identity()])
    radar.collect_cycle(radar_root,frozen,provider=NativeProvider(),codal=PublicNews(),price_scan=True,bootstrap=True)
    original=pd.read_parquet
    def altered(path,*args,**kwargs):
        frame=original(path,*args,**kwargs)
        frame.loc[frame.ins_code.eq('1'),'value']*=1.01
        return frame
    monkeypatch.setattr(pd,'read_parquet',altered)
    with pytest.raises(ValueError,match='Native history differs'):
        radar.verify(radar_root)
