"""فقط پاسخ ساختگی در آزمون و فایل‌های موقت؛ هیچ داده عملیاتی نمونه ساخته نمی‌شود."""

import json
from types import SimpleNamespace

import pytest

from config import settings
from core.instrument_history import assess_instrument_history, history_request
from core.timeutils import now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from ops import instrument_history as archive
from tests.conftest import FOOLAD_INS, FakeSession

DAY = '2024-01-02'


def observation(info=None, *, code=FOOLAD_INS, day=DAY):
    return dict(source='tsetmc', url=history_request(code, day)[1], ins_code=code, requested_date=day,
        received_at=now_tehran_iso(), payload=dict(instrumentHistory=info if info is not None else
            dict(insCode=code, lVal18AFC='فولاد', zTitad=1000000., baseVol=1000, lastDate=20260101)))


@pytest.fixture
def local_archive(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2025-03-21')
    return [dict(symbol_fa='فولاد', ins_code=FOOLAD_INS, instrument_type_code=300)]


def provider():
    return TsetmcProvider(session=FakeSession({'GetInstrumentHistory': observation()['payload']}))


def test_url_date_and_last_date_never_become_verified_historical_date():
    row = assess_instrument_history(observation(), FOOLAD_INS, DAY)
    assert row['shares_outstanding'] == 1000000 and row['base_volume'] == 1000
    assert row['date_attestation'] == 'request_url_only'
    assert row['identity_attestation'] == 'response_matches_request'
    assert row['original_publication_time'] is None
    assert not row['feature_eligible'] and not row['historical_membership_verified'] and not row['accepted_for_signals']


@pytest.mark.parametrize('value', [None, 0, -5, True, 1.5, float('inf'), float('nan'), float(2**53), 'NaN', '1,000'])
def test_unknown_or_unsafe_quantities_remain_unknown(value):
    raw = observation()['payload']['instrumentHistory']
    raw.update(zTitad=value, baseVol=value)
    row = assess_instrument_history(observation(raw), FOOLAD_INS, DAY)
    assert row['shares_outstanding'] is None and row['base_volume'] is None
    assert not row['candidate_fields_complete']


@pytest.mark.parametrize('key,value,status', [('insCode', '123', 'identity_conflict'),
    ('insCode', True, 'identity_conflict'), ('dEven', 20240103, 'date_conflict'), ('dEven', True, 'date_conflict')])
def test_conflicting_source_does_not_supply_candidate_values(key, value, status):
    raw = observation()['payload']['instrumentHistory']
    raw[key] = value
    result = assess_instrument_history(observation(raw), FOOLAD_INS, DAY)
    assert result['status'] == status and result['base_volume'] is None and result['shares_outstanding'] is None


def test_explicit_date_matches_still_do_not_attest_original_publication_or_entitlements():
    raw = dict(insCode=FOOLAD_INS, dEven=20240102, zTitad='10000000000000001', baseVol=1)
    row = assess_instrument_history(observation(raw), FOOLAD_INS, DAY)
    assert row['date_attestation'] == 'response_matches_request' and row['shares_outstanding'] == 10000000000000001
    assert row['base_volume'] == 1 and not row['feature_eligible']


def test_missing_body_identity_is_not_filled_from_request_and_bad_schema_stays_visible():
    result = assess_instrument_history(observation(dict(insCode='0', zTitad=1000, baseVol=1)), FOOLAD_INS, DAY)
    assert result['identity_attestation'] == 'request_url_only' and not result['feature_eligible']
    assert assess_instrument_history(observation({}), FOOLAD_INS, DAY)['status'] == 'invalid_schema'


def test_observation_envelope_and_receipt_clock_must_match_request():
    for field, value in [('url', 'https://other.invalid/'), ('ins_code', '123'), ('received_at', '2024-01-01T10:00:00')]:
        altered = dict(observation(), **{field: value})
        with pytest.raises(ValueError):
            assess_instrument_history(altered, FOOLAD_INS, DAY)


@pytest.mark.parametrize('code,day', [('0', DAY), ('../123', DAY), (True, DAY), ('۱۲۳', DAY),
                                   (FOOLAD_INS, '2024-1-2'), (FOOLAD_INS, '9999-01-01')])
def test_provider_rejects_invalid_or_future_request_without_a_network_call(code, day):
    source = provider()
    with pytest.raises(ValueError):
        source.get_instrument_history_observation(code, day)
    assert not source.session.calls


def test_provider_keeps_payload_identity_and_date_separate_and_none_on_failure():
    source = provider()
    actual = source.get_instrument_history_observation(FOOLAD_INS, DAY)
    assert actual['payload'] == observation()['payload'] and actual['requested_date'] == DAY
    assert source.session.calls == [history_request(FOOLAD_INS, DAY)[1]]
    assert TsetmcProvider(session=FakeSession({})).get_instrument_history_observation(FOOLAD_INS, DAY) is None


def test_capture_registers_first_retains_failed_attempts_and_is_replayable_offline(local_archive):
    class Source:
        calls = 0
        def get_instrument_history_observation(self, code, day):
            folder = next((settings.DATA_DIR / 'private/instrument-history').glob('HISTORY-*'))
            assert archive.read_private_report(folder / 'plan.json')['requests']
            self.calls += 1
            return observation(code=code, day=day) if self.calls == 1 else None
    result = archive.capture_instrument_history(Source(), local_archive, [DAY, '2024-01-03'])
    loaded = archive.load_instrument_history(result['folder'])
    assert len(loaded['cases']) == 2 and loaded['status_counts'] == {'observed_unverified': 1, 'unavailable': 1}
    assert loaded['candidate_fields_complete'] == 1 and loaded['artifacts_verified']
    assert loaded['cases'][1]['assessment']['shares_outstanding'] is None and not loaded['feature_eligible']


@pytest.mark.parametrize('dates', [['2025-03-21'], [DAY, DAY], ['2024-1-2'], []])
def test_invalid_or_sealed_dates_never_reach_provider(local_archive, dates):
    class Source:
        def get_instrument_history_observation(self, *_): pytest.fail('Invalid request reached provider')
    with pytest.raises(ValueError):
        archive.capture_instrument_history(Source(), local_archive, dates)


def test_request_budget_and_instrument_type_are_enforced_before_fetch(local_archive):
    source = provider()
    with pytest.raises(ValueError):
        archive.capture_instrument_history(source, local_archive * 21, [DAY])
    with pytest.raises(ValueError):
        archive.capture_instrument_history(source, [dict(local_archive[0], instrument_type_code=305)], [DAY])
    assert not source.session.calls


@pytest.mark.parametrize('kind', ['raw', 'code', 'summary', 'omitted_case', 'promoted', 'duplicate_request'])
def test_tampered_or_rehashed_archive_does_not_bypass_review(local_archive, kind):
    result = archive.capture_instrument_history(provider(), local_archive, [DAY, '2024-01-03'])
    folder = archive.Path(result['folder'])
    manifest = archive.read_private_report(folder / 'manifest.json')
    if kind == 'raw':
        path = folder / 'observation-000.json'
        raw = archive.read_private_report(path)
        raw['observation']['payload']['instrumentHistory']['baseVol'] *= 10
        archive.write_private_report(path, raw)
        manifest['observations'][path.name] = archive._sha(path)
    elif kind == 'code':
        with (folder / 'code/core/instrument_history.py').open('a', encoding='utf-8') as stream: stream.write('\nchanged\n')
    elif kind == 'duplicate_request':
        plan = archive.read_private_report(folder / 'plan.json')
        plan['requests'][1] = plan['requests'][0]
        archive.write_private_report(folder / 'plan.json', plan)
        manifest['plan_sha256'] = archive._sha(folder / 'plan.json')
    else:
        saved = archive.read_private_report(folder / 'result.json')
        if kind == 'summary': saved['candidate_fields_complete'] = 999
        if kind == 'omitted_case': saved['cases'].pop()
        if kind == 'promoted': saved['feature_eligible'] = True
        archive.write_private_report(folder / 'result.json', saved)
        manifest['result_sha256'] = archive._sha(folder / 'result.json')
    archive.write_private_report(folder / 'manifest.json', manifest)
    with pytest.raises(ValueError):
        archive.load_instrument_history(folder)


def test_interrupted_fetch_preserves_registered_plan_without_publishing_complete_result(local_archive):
    class Source:
        def get_instrument_history_observation(self, *_): raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        archive.capture_instrument_history(Source(), local_archive, [DAY])
    folder = next((settings.DATA_DIR / 'private/instrument-history').glob('HISTORY-*'))
    assert (folder / 'plan.json').exists() and not (folder / 'manifest.json').exists()
    assert archive.read_private_report(folder / 'status.json')['status'] == 'failed_or_interrupted'


def test_cli_capture_does_not_write_existing_database_or_account_tables(db, local_archive, monkeypatch, capsys):
    import brs
    db.upsert_symbol(local_archive[0])
    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    monkeypatch.setattr('data_collector.tsetmc.TsetmcProvider', provider)
    before = db.table_counts()
    assert brs.cmd_instrument_history(SimpleNamespace(symbols='فولاد', dates=DAY)) == 0
    saved = json.loads(capsys.readouterr().out)
    assert brs.cmd_instrument_history_review(SimpleNamespace(study=saved['folder'])) == 0
    assert db.table_counts() == before and not saved['feature_eligible']


def test_history_panel_shows_unknown_date_without_invented_values(local_archive):
    from streamlit.testing.v1 import AppTest
    def render():
        from dashboard.instrument_history import show_instrument_history
        show_instrument_history()
    empty = AppTest.from_function(render).run()
    assert not empty.exception and empty.info
    archive.capture_instrument_history(provider(), local_archive, [DAY])
    app = AppTest.from_function(render).run()
    assert not app.exception and 'فیچر یا بکتست نیست' in app.warning[0].value
    assert app.dataframe[0].value.iloc[0]['شاهد تاریخ'] == 'فقط نشانی درخواست'
