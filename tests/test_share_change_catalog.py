"""گسست واقعی‌نما در داده مصنوعی؛ فهرست حق تکمیل تاریخچه یا انتشار تاریخی نمی‌سازد."""

import json
import socket
from pathlib import Path

import pytest

from config import settings
from ops.share_change_catalog import build_share_catalog, main
from ops.share_changes import capture_share_changes
from tests.test_share_changes import observation, row


class Provider:
    def __init__(self,rows):self.rows=rows
    def get_share_change_observation(self,_code):
        meta,body=observation(self.rows);return dict(meta,content=body)


@pytest.fixture
def archive(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path)
    def create(rows,start='2024-01-01',end='2024-04-01'):
        return capture_share_changes(Provider(rows),[dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300)],
            start=start,end_exclusive=end)['folder']
    return create


def test_row_coherence_is_distinct_from_chain_consistency_and_no_number_is_repaired(archive,monkeypatch):
    folder=archive([row(numberOfShareNew=25),row(dEven=20240301,numberOfShareOld=1000,numberOfShareNew=1400)])
    monkeypatch.setattr(socket,'create_connection',lambda *_a,**_k:pytest.fail('network called'))
    monkeypatch.setattr('database.db.DatabaseManager.__init__',lambda *_a,**_k:pytest.fail('database read'))
    result=build_share_catalog([folder]);issuer=result['issuers'][0]
    assert issuer['source_status']=='coherent_observed' and issuer['chain_status']=='discontinuous_observed'
    assert result['observed_chain_break_count']==1 and issuer['observed_chain_breaks'][0]['previous_new_shares']==25
    assert result['events'][0]['source_new_shares']==25 and result['events'][1]['source_old_shares']==1000
    assert result['observed_change_types']=={'decrease':1,'increase':1}
    assert not result['projection_eligible'] and not result['complete_history_verified']
    assert not result['accepted_for_signals'] and not result['economic_valuation_verified']


def test_no_observed_break_does_not_prove_complete_history(archive):
    folder=archive([row(),row(dEven=20240301,numberOfShareOld=1350,numberOfShareNew=1500)])
    result=build_share_catalog([folder])
    assert result['issuers'][0]['chain_status']=='no_break_observed'
    assert not result['complete_history_verified'] and result['observed_chain_break_count']==0


def test_empty_source_stays_in_the_denominator_without_zero_shares(archive):
    result=build_share_catalog([archive([])])
    assert result['issuer_count']==1 and result['observed_group_count']==0
    assert result['issuers'][0]['chain_status']=='not_assessable' and result['events']==[]


def test_duplicate_issuer_studies_cannot_inflate_catalog_coverage(archive):
    folder=archive([row()])
    with pytest.raises(ValueError,match='تکراری'):build_share_catalog([folder,folder])


def test_different_windows_cannot_be_combined_even_for_the_same_source(archive):
    one=archive([row()]);two=archive([row()],start='2024-02-01')
    with pytest.raises(ValueError,match='پنجره'):build_share_catalog([one,two])


def test_cli_cannot_overwrite_a_source_file_or_create_an_extra_source_artifact(archive):
    folder=Path(archive([row()]));before=(folder/'manifest.json').read_bytes()
    for target in (folder/'result.json',folder/'other.json'):
        with pytest.raises(ValueError,match='ورودی'):main(['--studies',str(folder),'--json',str(target)])
    assert (folder/'manifest.json').read_bytes()==before and not (folder/'other.json').exists()


def test_cli_writes_only_a_separate_private_report(archive,tmp_path):
    folder=archive([row()]);target=tmp_path/'private/catalog.json'
    main(['--studies',str(folder),'--json',str(target)])
    assert json.loads(target.read_bytes())['issuer_count']==1
