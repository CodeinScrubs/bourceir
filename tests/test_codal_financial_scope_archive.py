"""Offline replay of synthetic metadata archives; never read a financial account."""

import hashlib
from pathlib import Path

import pytest

from config import settings
from ops import codal_financial_scope as scope
from ops import codal_search as search
from ops.report_files import read_private_report, write_private_report
from tests.test_codal_search import row
from tests.test_codal_search_archive import INSTRUMENT, Provider


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    monkeypatch.setattr(settings,'DB_PATH',tmp_path/'account.db')
    monkeypatch.setattr(search.time,'sleep',lambda _:None)
    native = row(LetterCode='ن-10',Title='اطلاعات و صورت‌های مالی میاندوره‌ای دوره ۹ ماهه منتهی به ۱۴۰۲/۰۹/۳۰ (حسابرسی نشده)')
    result = search.capture_search(Provider({1:[native],2:[]}),INSTRUMENT,
        start='2024-01-01',end_exclusive='2024-04-01',max_pages=3)
    return Path(result['folder'])


def test_exact_offline_replay_is_independent_of_the_original_search(source,monkeypatch):
    monkeypatch.setattr('requests.sessions.Session.request',lambda *_a,**_k:pytest.fail('No network'))
    captured=scope.capture_financial_scope(source)
    (source/'result.json').write_text('original unavailable',encoding='utf-8')
    reviewed=scope.review_financial_scope(captured['folder'])
    assert reviewed['exact_source_replay'] and reviewed['result']==captured['result']
    assert reviewed['result']['catalog']['scope_claim_counts']=={'issuer_unqualified':1}
    assert reviewed['result']['available_at']==reviewed['result']['calculated_at']
    assert not reviewed['result']['database_read'] and not reviewed['result']['holdout_opened']
    assert not settings.DB_PATH.exists()


def test_rehashed_scope_change_is_detected_by_source_replay(source):
    captured=scope.capture_financial_scope(source)
    folder=Path(captured['folder'])
    result=read_private_report(folder/'result.json')
    result['catalog']['records'][0]['scope_claim']='named_entity'
    write_private_report(folder/'result.json',result)
    manifest=read_private_report(folder/'manifest.json')
    manifest['files_sha256']['result.json']=hashlib.sha256((folder/'result.json').read_bytes()).hexdigest()
    write_private_report(folder/'manifest.json',manifest)
    with pytest.raises(ValueError,match='exact source replay'):
        scope.review_financial_scope(folder)


@pytest.mark.parametrize('change',['missing','extra','changed_code','changed_raw'])
def test_unbound_inventory_and_preserved_code_or_raw_change_are_rejected(source,change):
    captured=scope.capture_financial_scope(source)
    folder=Path(captured['folder'])
    if change=='missing':
        (folder/'result.json').unlink()
    elif change=='extra':
        (folder/'extra.json').write_text('{}',encoding='utf-8')
    elif change=='changed_code':
        (folder/'code/core/codal_financial_scope.py').write_text('# changed',encoding='utf-8')
    else:
        (folder/'input'/source.name/'raw/page-001.json').write_text('{}',encoding='utf-8')
    with pytest.raises((ValueError,FileNotFoundError)):
        scope.review_financial_scope(folder)


def test_account_and_nondesignated_paths_are_rejected(tmp_path):
    with pytest.raises(ValueError,match='designated'):
        scope.capture_financial_scope(tmp_path/'account.db')


def test_preexisting_raw_reassessment_changes_do_not_get_relabelled_original(source,monkeypatch):
    original=search._code
    monkeypatch.setattr(search,'_code',lambda name:original(name)+b'\n# current decoder change')
    captured=scope.capture_financial_scope(source)
    reassessment=captured['result']['search_reassessment']
    assert not reassessment['original_code_matches_current']
    assert reassessment['all_page_assessments_and_summary_equal_preserved']
    assert not reassessment['original_review_rerun'] and not reassessment['archived_code_executed']
