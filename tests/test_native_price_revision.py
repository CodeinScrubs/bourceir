"""Only synthetic public-market fixtures in temporary test databases; no network."""

import copy
import json

import pandas as pd
import pytest

from config import settings
from core.price_quality import json_price_row
from core.price_revision import SOURCE_FIELDS, VERSION, daily_request, revision_candidate
from core.timeutils import now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from database.price_revisions import apply_source_revisions
from features.provenance import raw_input_snapshot
from ops.price_revision import apply_revision, collect_revision, verified_revision
from ops.report_files import write_private_report
from ops.research_preflight import research_preflight
from tests.conftest import FakeSession
from tests.test_query_screener import bar, trades

CUTOFF = '2025-03-21'
RUN = 'NATIVEREV-' + '1' * 32


def issue(**changes):
    return dict(symbol='آزمون', symbol_id=1, ins_code='123', date='2024-02-03', phase='development',
                raw=json_price_row(bar(open_raw=None, high_raw=101) | changes))


def observation(**changes):
    row = bar()
    native = {source: row[field] for field, source in SOURCE_FIELDS.items()}
    native.update(insCode='123', dEven=20240203)
    native.update(changes)
    return dict(source='tsetmc', price_unit='Rial', ins_code='123', requested_date='2024-02-03',
                url=daily_request('123', '2024-02-03')[1], received_at=now_tehran_iso(),
                payload=dict(closingPriceDaily=native))


def candidate(row=None, obs=None, path=None):
    return revision_candidate(row or issue(), obs or observation(), trades() if path is None else path, cutoff=CUTOFF)


def test_dated_native_extreme_revision_needs_the_entire_trade_path():
    result = candidate()
    assert result['status'] == 'verified_native_trade_revision'
    assert result['before']['open_raw'] is None and result['before']['high_raw'] == 101
    assert result['after']['high_raw'] == 102 and result['after']['open_raw'] == 100
    assert result['changed_fields'] == ['high_raw', 'open_raw']
    assert all(result['reconciliation'].values())
    assert not result['historical_availability_verified'] and not result['source_independently_verified']
    assert 'event_confirmed' not in result and 'witness' not in result and not result['accepted_for_signals']
    pending = revision_candidate(issue(), observation(), None, cutoff=CUTOFF)
    assert pending['status'] == 'source_candidate_needs_trades'


@pytest.mark.parametrize('field,value,status', [('insCode', '999', 'native_identity_mismatch'),
    ('insCode', True, 'native_identity_mismatch'), ('dEven', 20261018, 'native_date_mismatch'),
    ('dEven', None, 'native_date_mismatch'), ('dEven', True, 'native_date_mismatch')])
def test_native_date_and_identity_checked_before_any_source_prices(field, value, status):
    class Guard(dict):
        def get(self, key, default=None):
            assert key in ('insCode', 'dEven'), 'financial field read before native binding'
            return super().get(key, default)
    obs = observation()
    obs['payload']['closingPriceDaily'] = Guard(insCode='123', dEven=20240203)
    obs['payload']['closingPriceDaily'][field] = value
    assert candidate(obs=obs)['status'] == status


@pytest.mark.parametrize('value', [True, False, None, '100', float('nan'), float('inf'), -1, 0, 2**53, 10**500])
def test_native_unrepresentable_unknown_or_boolean_values_are_not_repaired(value):
    assert candidate(obs=observation(priceFirst=value))['status'] == 'invalid_native_numeric'


@pytest.mark.parametrize('field', ['qTotTran5J', 'zTotTran'])
def test_fractional_native_quantities_are_rejected(field):
    assert candidate(obs=observation(**{field: 4.5}))['status'] == 'invalid_native_quantity'


@pytest.mark.parametrize('field', ['priceYesterday', 'pClosing'])
def test_trades_cannot_authorize_reference_or_official_price_replacement(field):
    assert candidate(obs=observation(**{field: 103}))['status'] == 'protected_reference_or_official_change'


@pytest.mark.parametrize('damage', ['partial', 'volume', 'count', 'value', 'open', 'identity', 'date', 'canceled_conflict'])
def test_no_partial_or_conflicting_trade_path_can_authorize_extreme_replacement(damage):
    path, obs = trades(), observation()
    rows = path['response']['tradeHistory']
    if damage == 'partial': rows.pop()
    elif damage == 'volume': rows[0]['qTitTran'] = 2
    elif damage == 'count': obs['payload']['closingPriceDaily']['zTotTran'] = 5
    elif damage == 'value': obs['payload']['closingPriceDaily']['qTotCap'] = 402
    elif damage == 'open': rows[0]['pTran'] = 99
    elif damage == 'identity': rows[0]['insCode'] = '999'
    elif damage == 'date': rows[0]['dEven'] = 20240204
    else: rows.append(rows[0] | dict(canceled=1))
    assert candidate(obs=obs, path=path)['status'] == 'unverified_full_trade_path'


@pytest.mark.parametrize('cutoff,day', [('2025-03-21', '2025-03-21'), ('2025-03-21', '2026-10-18'),
                                      ('bad', '2024-02-03'), ('2025-03-21', '2024-2-03')])
def test_sealed_or_noncanonical_dates_are_rejected_before_any_source_access(cutoff, day):
    class Guard(dict):
        def get(self, *_):
            raise AssertionError('sealed source read')
    with pytest.raises(ValueError):
        revision_candidate(issue() | dict(date=day), Guard(), Guard(), cutoff=cutoff)


@pytest.mark.parametrize('field,value', [('source', 'elsewhere'), ('price_unit', 'Toman'),
    ('url', 'http://cdn.tsetmc.com/other'), ('requested_date', '2024-02-04'), ('received_at', '2024-02-03T10:00:00')])
def test_envelope_mismatch_is_an_error(field, value):
    with pytest.raises(ValueError):
        candidate(obs=observation() | {field: value})


def test_dated_provider_preserves_body_date_without_patching_it_from_url():
    payload = observation(dEven=20261018)['payload']
    session = FakeSession({'GetClosingPriceDaily': payload})
    provider = TsetmcProvider(session=session)
    obs = provider.get_dated_daily_observation('123', '2024-02-03')
    assert obs['payload'] is payload and obs['payload']['closingPriceDaily']['dEven'] == 20261018
    assert obs['requested_date'] == '2024-02-03'
    assert candidate(obs=obs)['status'] == 'native_date_mismatch'


def test_volume_and_count_revision_requires_exact_summed_trades():
    row = issue(open_raw=100, high_raw=102, volume=1, trade_count=3)
    result = candidate(row)
    assert result['status'] == 'verified_native_trade_revision'
    assert result['changed_fields'] == ['trade_count', 'volume']
    assert result['after']['volume'] == result['after']['trade_count'] == 4


def test_explicit_official_close_outside_traded_range_is_preserved():
    row = issue(close_final_raw=200)
    result = candidate(row, observation(pClosing=200))
    assert result['status'] == 'verified_native_trade_revision'
    assert result['after']['close_final_raw'] == row['raw']['close_final_raw'] == 200


@pytest.fixture
def local(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    sid = db.upsert_symbol(dict(symbol_fa='آزمون', ins_code='123', instrument_type_code=300))
    row = issue() | dict(symbol_id=sid)
    db.upsert_daily_prices_df(sid, pd.DataFrame([row['raw']]))
    path = tmp_path / 'private' / 'preflight.json'
    write_private_report(path, research_preflight(db))
    return row, path, tmp_path


class Provider:
    name = 'tsetmc'
    def __init__(self, db, root):
        self.db, self.root = db, root

    def get_dated_daily_observation(self, code, day):
        plan = next((self.root / 'private' / 'price-revisions').glob('*/plan.json'))
        saved = json.loads(plan.read_text(encoding='utf-8'))
        assert saved['selected'][0]['date'] == day and saved['selected'][0]['ins_code'] == code
        assert not self.db.conn.in_transaction and saved['automatic_apply'] is False
        return observation()

    def get_trade_session(self, code, day):
        assert not self.db.conn.in_transaction
        return trades(ins_code=code, requested_date=day)


def collect(db, local, provider=None):
    _, path, root = local
    return collect_revision(db, from_preflight=path, provider=provider or Provider(db, root))


def test_register_review_atomic_apply_backup_and_idempotent_replay(db, local):
    row, _, root = local
    initial_hash = raw_input_snapshot(db, end_exclusive=CUTOFF)
    changes = db.conn.total_changes
    report, folder = collect(db, local)
    assert db.conn.total_changes == changes and report['statuses'] == {'verified_native_trade_revision': 1}
    assert report['daily_requests'] == report['trade_requests'] == 1 and not report['current_global_quality_verified']
    assert verified_revision(folder)[2][0][1]['after']['high_raw'] == 102
    applied, path = apply_revision(db, folder, review_note='Complete synthetic test-only source comparison')
    assert path.is_file() and applied['applied'] == 1 and applied['feature_rebuild_required']
    assert (db.db_path.parent / 'backups' / applied['backup_name']).is_file()
    current = json_price_row(db.get_daily_prices_df(row['symbol']).iloc[0])
    assert current['high_raw'] == 102 and current['open_raw'] == 100
    assert db.conn.execute('SELECT high_raw FROM daily_prices_revisions ORDER BY id DESC LIMIT 1').fetchone()[0] == 101
    assert raw_input_snapshot(db, end_exclusive=CUTOFF) != initial_hash
    again, _ = apply_revision(db, folder, review_note='Complete synthetic test-only source comparison')
    assert again['applied'] == 0 and again['already_applied'] == 1 and again['backup_name'] is None
    assert len(list((db.db_path.parent / 'backups').glob('*.db'))) == 1
    assert root == settings.DATA_DIR


@pytest.mark.parametrize('incoming', ['exact_before', 'after', 'new_source', 'mixed'])
def test_only_exact_old_source_version_is_preserved_on_later_ingestion(db, local, incoming):
    row, _, _ = local
    _, folder = collect(db, local)
    apply_revision(db, folder, review_note='Complete synthetic test-only source comparison')
    after = candidate(row)['after']
    raw = copy.deepcopy(row['raw'] if incoming in ('exact_before', 'mixed') else after)
    if incoming == 'new_source': raw['high_raw'] = 103
    if incoming == 'mixed': raw['open_raw'] = 100
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([raw]))
    actual = json_price_row(db.get_daily_prices_df(row['symbol']).iloc[0])
    assert actual == (after if incoming in ('exact_before', 'after') else raw)


@pytest.mark.parametrize('target', ['plan.json', 'result.json', 'observation-000.json', 'code/core/price_revision.py', 'trade'])
def test_tampered_archives_never_authorize_an_application(db, local, target):
    _, folder = collect(db, local)
    path = next((folder / 'trades').glob('*.gz')) if target == 'trade' else folder / target
    path.write_bytes(b'changed synthetic test-only evidence')
    with pytest.raises((ValueError, OSError, KeyError, TypeError)):
        apply_revision(db, folder, review_note='Tamper rejection test without production data')
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0
    assert db.get_daily_prices_df('آزمون').high_raw.iloc[0] == 101


def test_local_change_after_capture_is_not_overwritten(db, local):
    _, folder = collect(db, local)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET value=value+1')
    with pytest.raises(ValueError, match='تغییر'):
        apply_revision(db, folder, review_note='Reject a changed local source row in tests')
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0
    assert db.get_daily_prices_df('آزمون').high_raw.iloc[0] == 101


def test_evidence_mutation_during_transaction_rolls_back_price_and_receipt(db, local, monkeypatch):
    _, folder = collect(db, local)
    from ops import price_revision
    original = price_revision.apply_source_revisions
    def changed(*args, **kwargs):
        callback = kwargs['before_commit']
        def mutate():
            (folder / 'observation-000.json').write_text('{}', encoding='utf-8')
            callback()
        return original(*args, **(kwargs | dict(before_commit=mutate)))
    monkeypatch.setattr(price_revision, 'apply_source_revisions', changed)
    with pytest.raises(ValueError):
        apply_revision(db, folder, review_note='Atomic rollback of changed archived test-only inputs')
    assert db.get_daily_prices_df('آزمون').high_raw.iloc[0] == 101
    assert db.conn.execute('SELECT count(*) FROM price_recoveries').fetchone()[0] == 0


def test_no_network_is_the_default_and_no_candidates_means_no_backup(db, local):
    report, folder = collect_revision(db, from_preflight=local[1])
    assert report['daily_requests'] == report['trade_requests'] == 0
    assert report['statuses'] == {'source_unavailable': 1}
    applied, _ = apply_revision(db, folder, review_note='No source available in this temporary test')
    assert applied['applied'] == 0 and applied['backup_name'] is None


def test_missing_trade_source_is_preserved_and_never_applied(db, local):
    class Unavailable(Provider):
        def get_trade_session(self, *_):
            return None
    result, folder = collect(db, local, Unavailable(db, local[2]))
    assert result['trade_requests'] == 1 and result['statuses'] == {'unverified_full_trade_path': 1}
    assert verified_revision(folder)[2] == []
    applied, _ = apply_revision(db, folder, review_note='Missing trade response must never authorize a repair')
    assert applied['applied'] == 0


def test_future_rows_are_outside_the_selection_and_database_repair(db, local):
    row, source, _ = local
    future = row['raw'] | dict(date=CUTOFF)
    db.upsert_daily_prices_df(row['symbol_id'], pd.DataFrame([future]))
    write_private_report(source, research_preflight(db))
    result, folder = collect(db, local)
    assert result['selected_rows'] == 1
    apply_revision(db, folder, review_note='Only the development row may change in this temporary database')
    actual = db.get_daily_prices_df(row['symbol'], start_date=CUTOFF).iloc[0]
    assert actual.high_raw == 101 and pd.isna(actual.open_raw)


def test_new_source_code_prevents_old_application_without_changing_archive(db, local, monkeypatch):
    _, folder = collect(db, local)
    old = (folder / 'result.json').read_bytes()
    monkeypatch.setattr('ops.price_revision.code_identity', lambda: {'other': 'code'})
    with pytest.raises(ValueError, match='کد'):
        apply_revision(db, folder, review_note='Old code identities cannot silently apply a new model')
    assert (folder / 'result.json').read_bytes() == old


@pytest.mark.parametrize('options', [dict(max_rows=True), dict(max_rows=51), dict(offset=-1), dict(include_warmup=1)])
def test_selection_options_checked_before_database_or_file_access(options):
    with pytest.raises(ValueError):
        collect_revision(None, from_preflight='not-read.json', **options)


@pytest.mark.parametrize('fields', [('ref_price_raw',), ('close_final_raw',), ('date',)])
def test_database_guard_blocks_changes_outside_trade_provable_fields(db, local, fields):
    row = local[0]
    found = candidate(row)
    for field in fields:
        found['after'][field] = '2024-02-04' if field == 'date' else 999
        found['changed_fields'].append(field)
    with pytest.raises(ValueError):
        apply_source_revisions(db, RUN, [(row, found)], cutoff=CUTOFF,
                               evidence=dict(version=VERSION), before_commit=lambda: None)
    assert db.get_daily_prices_df('آزمون').high_raw.iloc[0] == 101
