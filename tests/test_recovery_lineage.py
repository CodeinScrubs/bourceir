"""Legacy recovered values remain visible without being promoted to verified history."""

import copy
import json

import pytest

from core.price_recovery import recovery_candidate
from core.recovery_lineage import inspect_recovery_lineage
from database.price_recovery import apply_recoveries
from tests.test_price_recovery import CUTOFF
from tests.test_price_recovery import stored as stored
from tests.test_query_screener import trades


def legacy_evidence():
    return dict(code={'core/price_recovery.py':'a'*64,'ops/price_recovery.py':'b'*64,
                      'data_collector/trade_archive.py':'c'*64},
                source_independently_verified=False,historical_availability_verified=False)


def apply_legacy(db, candidate, evidence=None):
    return apply_recoveries(db,'RECOVER-'+'a'*32,[candidate],cutoff=CUTOFF,
                            evidence=legacy_evidence() if evidence is None else evidence)


def test_retained_trade_only_origin_is_a_separate_blocker_without_reading_account(db,stored):
    apply_legacy(db,stored)
    statements=[]
    db.conn.set_trace_callback(statements.append)
    before=db.conn.total_changes
    report=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    db.conn.set_trace_callback(None)
    assert report['legacy_trade_receipts']==1
    assert report['retained_all_rows']==1 and report['retained_partial_rows']==0
    assert report['retained_development_rows']==1 and report['retained_prior_rows']==0
    assert report['status']=='unresolved' and not report['review_clear']
    assert not report['source_conflict_count_known'] and not report['historical_availability_verified']
    assert db.conn.total_changes==before
    assert not any(prefix in sql.lower() for sql in statements for prefix in ('broker_','paper_','journal_','personal_'))


def test_superseded_recovered_values_are_not_silently_counted_as_current(db,stored):
    apply_legacy(db,stored)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET open_raw=99,open=99 WHERE symbol_id=?',(stored[0]['symbol_id'],))
    report=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    assert report['superseded_rows']==1 and report['retained_all_rows']==0
    assert report['review_clear'] and report['status']=='no_retained_trade_only_rows'
    assert not report['independent_source_correctness_verified']


def test_partially_retained_endpoints_remain_unresolved(db,stored):
    issue=copy.deepcopy(stored[0]);issue['raw']['close_raw']=None
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET close_raw=NULL,close=NULL WHERE symbol_id=?',(issue['symbol_id'],))
    candidate=recovery_candidate(issue,trades(),cutoff=CUTOFF)
    apply_legacy(db,(issue,candidate))
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET open_raw=99,open=99 WHERE symbol_id=?',(issue['symbol_id'],))
    report=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    assert report['retained_partial_rows']==1 and not report['review_clear']


def test_unknown_receipt_provenance_is_unresolved_not_zero(db,stored):
    apply_legacy(db,stored,evidence={'unrecognized_test_evidence':True})
    report=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    assert report['unrecognized_receipts']==1 and not report['review_clear']


@pytest.mark.parametrize('version', ['native-price-revision-v1', 'explicit-price-reference-v1'])
def test_broken_known_version_receipt_does_not_disappear(db, stored, version):
    apply_legacy(db, stored, evidence={'version': version})
    report = inspect_recovery_lineage(db, cutoff=CUTOFF, development_start='2012-03-20')
    assert report['malformed_receipts'] == 1
    assert not report['review_clear'] and report['status'] == 'unresolved'


def insert_other_receipt(db, stored, version, *, damage=None):
    issue, candidate = stored
    if version == 'native-price-revision-v1':
        prefix = 'NATIVEREV'
        paths = ('core/price_revision.py', 'ops/price_revision.py')
        after, fields = copy.deepcopy(candidate['after']), candidate['changed_fields'][:]
    else:
        prefix = 'REFPRICE'
        paths = ('core/price_reference.py', 'ops/reference_recovery.py')
        after, fields = copy.deepcopy(issue['raw']), ['ref_price_raw']
        after['ref_price_raw'] += 1
    before = copy.deepcopy(issue['raw'])
    evidence = dict(version=version, plan_sha256='a'*64, result_sha256='b'*64,
                    artifacts_sha256={'plan.json': 'a'*64, 'observation-000.json': 'c'*64},
                    code={path: 'd'*64 for path in paths}, review_note='Local test review of known evidence',
                    source_independently_verified=False, historical_availability_verified=False)
    run_id = prefix + '-' + 'e'*32
    if damage == 'missing_code': evidence['code'].pop(paths[0])
    elif damage == 'bad_artifact': evidence['artifacts_sha256']['observation-000.json'] = 'bad'
    elif damage == 'bad_plan': evidence['artifacts_sha256']['plan.json'] = 'f'*64
    elif damage == 'unsupported_claim': evidence['historical_availability_verified'] = True
    elif damage == 'bad_fields': fields = ['close_raw']
    elif damage == 'wrong_run': run_id = 'RECOVER-' + 'e'*32
    elif damage == 'bad_date': after['date'] = '2024-02-04'
    elif damage == 'boolean_unchanged': before['value'] = after['value'] = True
    elif damage == 'overflow_unchanged': before['value'] = after['value'] = 1e300
    before_json, after_json = json.dumps(before), json.dumps(after)
    if damage == 'overflow_unchanged':
        before_json, after_json = before_json.replace('1e+300', '1e999'), after_json.replace('1e+300', '1e999')
    row = db.conn.execute('SELECT id FROM daily_prices WHERE symbol_id=? AND date=?',
                          (issue['symbol_id'], issue['date'])).fetchone()
    with db.conn:
        db.conn.execute('INSERT INTO price_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('f'*64, run_id, issue['symbol_id'], row['id'], issue['date'], '2024-02-05T12:00:00+03:30',
             before_json, after_json, json.dumps(fields), json.dumps(evidence)))


@pytest.mark.parametrize('version', ['native-price-revision-v1', 'explicit-price-reference-v1'])
def test_well_formed_other_receipt_is_recognized_but_not_certified(db, stored, version):
    insert_other_receipt(db, stored, version)
    report = inspect_recovery_lineage(db, cutoff=CUTOFF, development_start='2012-03-20')
    assert report['review_clear'] and report['malformed_receipts'] == 0
    assert not report['independent_source_correctness_verified']
    assert not report['historical_availability_verified']


@pytest.mark.parametrize('damage', ['missing_code', 'bad_artifact', 'bad_plan', 'unsupported_claim',
                                    'bad_fields', 'wrong_run', 'bad_date', 'boolean_unchanged', 'overflow_unchanged'])
def test_known_version_receipt_requires_consistent_metadata_and_change(db, stored, damage):
    insert_other_receipt(db, stored, 'native-price-revision-v1', damage=damage)
    report = inspect_recovery_lineage(db, cutoff=CUTOFF, development_start='2012-03-20')
    assert report['malformed_receipts'] == 1 and not report['review_clear']


def test_invalid_current_endpoint_is_not_counted_as_a_clean_supersession(db,stored):
    apply_legacy(db,stored)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET open_raw=0,open=0 WHERE symbol_id=?',(stored[0]['symbol_id'],))
    report=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    assert report['malformed_receipts']==1 and not report['review_clear']


def test_future_receipt_payload_is_not_read_by_preholdout_lineage(db,stored):
    apply_legacy(db,stored)
    prior=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    row=db.conn.execute('SELECT * FROM price_recoveries').fetchone()
    with db.conn:
        db.conn.execute('INSERT INTO price_recoveries VALUES(?,?,?,?,?,?,?,?,?,?)',
            ('future-test', 'future', row['symbol_id'],row['daily_price_id'],'2026-01-01',row['applied_at'],
             'not-json','not-json','not-json','not-json'))
    after=inspect_recovery_lineage(db,cutoff=CUTOFF,development_start='2012-03-20')
    assert after==prior


@pytest.mark.parametrize('bad_cutoff',['2026-01-01','bad',None])
def test_lineage_never_reads_past_the_sealed_cutoff(db,bad_cutoff):
    with pytest.raises(ValueError):
        inspect_recovery_lineage(db,cutoff=bad_cutoff,development_start='2012-03-20')


def test_readiness_is_blocked_by_legacy_origin_even_if_numeric_contract_passes(db,stored,monkeypatch):
    from ops.trader_readiness import collect_readiness
    from tests.test_research_preflight import store

    store(db,symbol='بازار')
    apply_legacy(db,stored)
    monkeypatch.setattr('ops.trader_readiness.inspect_environment',lambda *_:dict(ready=True))
    report=collect_readiness(db)
    assert report['development']['input_contract_passed']
    assert report['engineering_and_input_contract_passed']
    assert report['status']=='blocked' and not report['recovery_lineage']['review_clear']
    assert 'legacy_trade_only_recovery_retained' in report['blockers']
    assert not report['accepted_for_signals']
