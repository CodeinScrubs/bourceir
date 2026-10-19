"""بازیابی مرجع با داده مصنوعی فقط در دیتابیس موقت؛ بدون شبکه یا حساب واقعی."""

import copy
import json

import pandas as pd
import pytest

from config import settings
from core.market_rules import backward_adjusted_close, corporate_action_factors
from core.price_quality import json_price_row
from core.price_reference import VERSION, reference_candidate, valid_reference_change
from data_collector.tsetmc import TsetmcProvider
from database.price_reference import apply_reference_recoveries
from features.provenance import raw_input_snapshot
from ops.reference_recovery import apply_references, collect_references, verified_references
from ops.report_files import write_private_report
from ops.research_preflight import research_preflight
from tests.conftest import FakeSession
from tests.test_native_price_revision import observation
from tests.test_query_screener import bar

CUTOFF = '2025-03-21'


def issue(**changes):
    return dict(symbol='آزمون', symbol_id=1, ins_code='123', date='2024-02-03', phase='development',
                raw=json_price_row(bar(ref_price_raw=None) | changes))


def history(**changes):
    out = observation(**changes)
    out['url'] = 'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDailyList/123/0'
    out['payload']['closingPriceDaily'] = [out['payload']['closingPriceDaily']]
    return out


def candidate(row=None, daily=None, listed=None):
    return reference_candidate(row or issue(), daily or observation(), listed or history(), cutoff=CUTOFF)


@pytest.mark.parametrize('reference', [None, 0, -1])
def test_only_unknown_or_nonpositive_reference_is_recovered(reference):
    out = candidate(issue(ref_price_raw=reference))
    assert out['status'] == 'verified_explicit_reference' and out['after']['ref_price_raw'] == 100
    assert out['changed_fields'] == ['ref_price_raw'] and out['both_paths_all_fields_equal']
    assert valid_reference_change(out['before'], out['after'])
    assert not out['historical_availability_verified'] and not out['economic_entitlements_verified']
    assert not out['source_independently_verified'] and not out['accepted_for_signals']


def test_known_positive_reference_and_multiple_price_defects_are_outside_scope():
    assert candidate(issue(ref_price_raw=99))['status'] == 'outside_reference_only_repair'
    assert candidate(issue(open_raw=None))['status'] == 'outside_reference_only_repair'
    assert candidate(issue(volume=4.5))['status'] == 'outside_reference_only_repair'


@pytest.mark.parametrize('which', ['daily', 'listed'])
@pytest.mark.parametrize('field,value,status', [('insCode', '999', 'native_identity_mismatch'),
    ('insCode', True, 'native_identity_mismatch'), ('dEven', 20261019, 'native_date_mismatch'),
    ('dEven', True, 'native_date_mismatch')])
def test_both_native_bindings_are_checked_before_either_financial_row(which, field, value, status):
    class Guard(dict):
        def get(self, key, default=None):
            assert key in ('insCode', 'dEven'), 'financial field read before both native bindings'
            return super().get(key, default)
    first, second = observation(), history()
    a, b = Guard(insCode='123', dEven=20240203), Guard(insCode='123', dEven=20240203)
    (a if which == 'daily' else b)[field] = value
    first['payload']['closingPriceDaily'] = a
    second['payload']['closingPriceDaily'] = [b]
    assert candidate(daily=first, listed=second)['status'] == status


@pytest.mark.parametrize('value', [True, False, None, '100', 0, -1, 2**53, 10**500, float('nan'), float('inf')])
def test_reference_must_be_an_explicit_safe_positive_number(value):
    assert candidate(daily=observation(priceYesterday=value), listed=history(priceYesterday=value))['status'] == 'native_numeric_invalid'


@pytest.mark.parametrize('field', ['priceYesterday', 'pClosing', 'priceFirst', 'qTotTran5J', 'zTotTran'])
def test_a_disagreement_in_any_source_field_is_not_hidden(field):
    assert candidate(listed=history(**{field: 900}))['status'] == 'native_paths_disagree'


@pytest.mark.parametrize('field', ['priceFirst', 'priceMax', 'pClosing', 'qTotTran5J', 'zTotTran'])
def test_agreement_cannot_authorize_any_other_raw_field_change(field):
    assert candidate(daily=observation(**{field: 900}), listed=history(**{field: 900}))['status'] == 'other_fields_changed_or_row_invalid'


@pytest.mark.parametrize('value', [[], [dict(insCode='123', dEven=20240203)] * 2])
def test_history_target_must_exist_exactly_once(value):
    listed = history()
    listed['payload']['closingPriceDaily'] = value
    assert candidate(listed=listed)['status'] == 'history_row_missing_or_ambiguous'


def test_official_close_outside_traded_range_is_kept_without_inventing_a_reference():
    out = candidate(issue(close_final_raw=500), observation(pClosing=500), history(pClosing=500))
    assert out['status'] == 'verified_explicit_reference'
    assert out['after']['close_final_raw'] == 500


@pytest.mark.parametrize('field', ['qTotTran5J', 'zTotTran'])
def test_matching_fractional_trade_quantities_are_still_invalid(field):
    assert candidate(daily=observation(**{field: 4.5}), listed=history(**{field: 4.5}))['status'] == 'other_fields_changed_or_row_invalid'


@pytest.mark.parametrize('key,value', [('price_unit', 'Toman'), ('source', 'other'), ('ins_code', '999'),
    ('requested_date', '2024-02-04'), ('url', 'https://example.com'), ('received_at', '2024-02-03T10:00:00')])
def test_envelope_and_aware_receipt_are_required(key, value):
    with pytest.raises(ValueError):
        candidate(listed=history() | {key: value})


def test_holdout_boundary_is_checked_before_source_access():
    class Guard(dict):
        def get(self, *_):
            raise AssertionError('sealed source read')
    with pytest.raises(ValueError):
        reference_candidate(issue() | dict(date=CUTOFF), Guard(), Guard(), cutoff=CUTOFF)


def test_history_provider_excludes_unselected_financial_fields_before_archiving():
    class Guard(dict):
        def get(self, key, default=None):
            assert key == 'dEven'
            return super().get(key, default)
        def __getitem__(self, key):
            assert key == 'dEven'
            return super().__getitem__(key)
    native = observation()['payload']['closingPriceDaily']
    rows = [Guard(dEven=20250321), native, Guard(dEven=True), Guard(dEven=20261019)]
    provider = TsetmcProvider(session=FakeSession({'GetClosingPriceDailyList': dict(closingPriceDaily=rows)}))
    out = provider.get_dated_history_observation('123', '2024-02-03')
    assert out['received_row_count'] == 4 and out['payload']['closingPriceDaily'] == [native]
    assert out['response_scope'] == 'selected_native_rows' and out['unselected_numeric_rows_not_archived']


@pytest.fixture
def local(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    row = issue() | dict(symbol_id=sid)
    db.upsert_daily_prices_df(sid, pd.DataFrame([bar(date='2024-02-02'), row['raw'], bar(date=CUTOFF)]))
    path = tmp_path / 'private/preflight.json'
    write_private_report(path, research_preflight(db))
    return row, path, tmp_path


class Provider:
    name = 'tsetmc'
    def __init__(self, db, root):
        self.db, self.root = db, root
    def get_dated_daily_observation(self, code, day):
        plan = next((self.root / 'private/reference-recoveries').glob('*/plan.json'))
        assert json.loads(plan.read_bytes())['automatic_apply'] is False
        assert not self.db.conn.in_transaction
        return observation()
    def get_dated_history_observation(self, code, day):
        assert not self.db.conn.in_transaction
        return history()


def collect(db, local):
    return collect_references(db, from_preflight=local[1], provider=Provider(db, local[2]))


def test_registered_replay_atomic_application_display_rebuild_and_idempotence(db, local):
    row, _, root = local
    original = raw_input_snapshot(db, end_exclusive=CUTOFF)
    future = dict(db.conn.execute('SELECT * FROM daily_prices WHERE date=?', (CUTOFF,)).fetchone())
    writes = db.conn.total_changes
    report, folder = collect(db, local)
    assert db.conn.total_changes == writes and report['statuses'] == {'verified_explicit_reference': 1}
    assert report['daily_requests'] == report['history_requests'] == 1
    assert verified_references(folder)[2][0][1]['after']['ref_price_raw'] == 100
    applied, _ = apply_references(db, folder, review_note='Explicit test-only native references from both source paths')
    assert applied['applied'] == 1 and applied['display_rows_recomputed'] == 3
    assert (db.db_path.parent / 'backups' / applied['backup_name']).is_file()
    frame = db.get_daily_prices_df(row['symbol'], raw_only=False)
    assert frame.ref_price_raw.notna().all()
    assert frame.adj_close.to_list() == backward_adjusted_close(frame).round(4).to_list()
    assert frame.adjustment_factor.to_list() == corporate_action_factors(frame).to_list()
    after_future = dict(db.conn.execute('SELECT * FROM daily_prices WHERE date=?', (CUTOFF,)).fetchone())
    assert {k:v for k,v in future.items() if k not in ('adj_close','adjustment_factor')} == {
        k:v for k,v in after_future.items() if k not in ('adj_close','adjustment_factor')}
    assert raw_input_snapshot(db, end_exclusive=CUTOFF) != original
    revisions = db.conn.execute('SELECT * FROM daily_prices_revisions').fetchall()
    assert len(revisions) == 1 and revisions[0]['ref_price_raw'] is None
    again, _ = apply_references(db, folder, review_note='Explicit test-only native references from both source paths')
    assert again['applied'] == again['display_rows_recomputed'] == 0 and again['already_applied'] == 1
    assert len(list((db.db_path.parent / 'backups').glob('*.db'))) == 1 and root == settings.DATA_DIR


@pytest.mark.parametrize('incoming', ['before', 'after', 'changed_other', 'changed_reference'])
def test_exact_old_source_is_preserved_and_new_information_is_not_masked(db, local, incoming):
    row = local[0]
    _, folder = collect(db, local)
    apply_references(db, folder, review_note='Temporary database reference restoration test')
    after = candidate(row)['after']
    raw = copy.deepcopy(row['raw'] if incoming in ('before', 'changed_other') else after)
    if incoming == 'changed_other': raw['high_raw'] = 105
    if incoming == 'changed_reference': raw['ref_price_raw'] = 99
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([raw]))
    expected = after if incoming in ('before','after') else raw
    # Normal ingestion coalesces an absent reference but still admits the changed other field.
    if incoming == 'changed_other': expected['ref_price_raw'] = 100
    assert json_price_row(db.get_daily_prices_df(row['symbol'], start_date=row['date'], end_date=row['date']).iloc[0]) == expected


@pytest.mark.parametrize('target', ['plan.json','result.json','observation-000.json','code/core/price_reference.py'])
def test_modified_archive_cannot_change_prices(db, local, target):
    _, folder = collect(db, local)
    (folder / target).write_bytes(b'changed test-only archive')
    with pytest.raises((ValueError, OSError, KeyError, TypeError)):
        apply_references(db, folder, review_note='Reject test-only changed source evidence')
    assert db.conn.execute('SELECT COUNT(*) FROM price_recoveries').fetchone()[0] == 0


def test_evidence_change_rolls_back_raw_reference_receipt_and_display(db, local, monkeypatch):
    _, folder = collect(db, local)
    before = [dict(r) for r in db.conn.execute('SELECT * FROM daily_prices')]
    from ops import reference_recovery
    original = reference_recovery.apply_reference_recoveries
    def mutate(*args, **kwargs):
        callback = kwargs['before_commit']
        def changed():
            (folder / 'observation-000.json').write_text('{}', encoding='utf-8')
            callback()
        return original(*args, **(kwargs | dict(before_commit=changed)))
    monkeypatch.setattr(reference_recovery, 'apply_reference_recoveries', mutate)
    with pytest.raises(ValueError):
        apply_references(db, folder, review_note='Atomic reference and chart rollback in a temporary database')
    assert [dict(r) for r in db.conn.execute('SELECT * FROM daily_prices')] == before
    assert db.conn.execute('SELECT COUNT(*) FROM daily_prices_revisions').fetchone()[0] == 0
    assert db.conn.execute('SELECT COUNT(*) FROM price_recoveries').fetchone()[0] == 0


def test_no_source_default_does_not_repair_or_create_backup(db, local):
    report, folder = collect_references(db, from_preflight=local[1])
    assert report['statuses'] == {'source_unavailable': 1}
    assert report['daily_requests'] == report['history_requests'] == 0
    applied, _ = apply_references(db, folder, review_note='Source intentionally absent in temporary test')
    assert applied['applied'] == 0 and applied['backup_name'] is None


def test_one_missing_path_keeps_the_outcome_unknown(db, local):
    class Unavailable(Provider):
        def get_dated_history_observation(self, *_):
            return None
    report, folder = collect_references(db, from_preflight=local[1], provider=Unavailable(db, local[2]))
    assert report['statuses'] == {'source_unavailable': 1} and verified_references(folder)[2] == []
    assert db.conn.execute('SELECT COUNT(*) FROM price_recoveries').fetchone()[0] == 0


def test_changed_local_price_or_positive_replacement_is_rejected(db, local):
    _, folder = collect(db, local)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET value=value+1 WHERE date=?', (local[0]['date'],))
    with pytest.raises(ValueError):
        apply_references(db, folder, review_note='Concurrent local price mutation must not be overwritten')
    selected = candidate(local[0])
    selected['before']['ref_price_raw'] = 90
    with pytest.raises(ValueError):
        apply_reference_recoveries(db, 'REFPRICE-'+'1'*32, [(local[0],selected)], cutoff=CUTOFF,
                                   evidence=dict(version=VERSION), before_commit=lambda: None)
