"""The home desk must not turn a working process into proof of fresh data."""

import json
from datetime import datetime, timedelta

import pytest
from streamlit.testing.v1 import AppTest

from config import settings
from core.timeutils import TEHRAN_TZ
from ops.command_health import command_health, feature_store_state


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding='utf-8')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    private = settings.DATA_DIR/'private'
    private.mkdir(parents=True)
    at = datetime(2026, 11, 2, 10, 0, tzinfo=TEHRAN_TZ)
    return private, at


def health(private, at, radar=True, live=False):
    return command_health(private, radar_worker={'running':radar}, live_worker={'running':live}, at=at)


def test_missing_and_fresh_heartbeat_are_distinct_from_verified_market_data(setup):
    private, at = setup
    result = health(private, at)
    assert result['radar']['worker'] == 'running' and result['radar']['heartbeat'] == 'absent'
    assert result['radar']['heartbeat_fresh'] is False
    assert result['board_state'] == 'absent' and result['codal_document_source'] == 'absent'
    assert result['feature_store']['state'] == 'absent'
    write(private/'opportunity-radar/heartbeat.json', dict(heartbeat_at=at.isoformat(), status='degraded',
          price_session_date='2026-10-27', news_coverage={'window_scan_satisfied':False}))
    result = health(private, at)
    assert result['radar']['heartbeat_fresh'] is True
    assert result['radar']['recorded_status'] == 'degraded'
    assert result['radar']['price_session_date'] == '2026-10-27'
    assert result['radar']['news_window_complete'] is False
    assert result['source_freshness_verified'] is False and result['signal_accepted'] is False


def test_feature_store_checks_metadata_and_containment_without_claiming_integrity(setup, monkeypatch):
    from features.daily import FEATURE_SET_VERSION

    private, _ = setup
    data = private.parent
    store = data/'features'/FEATURE_SET_VERSION
    manifest = dict(version=FEATURE_SET_VERSION, manifest_format=2, rows=1, date_max='2026-11-02',
                    data_file='snapshots/test/daily.parquet', provenance=dict(parquet_sha256='0'*64))
    write(store/'manifest.json', manifest)
    assert feature_store_state(data)['state'] == 'invalid'  # missing Parquet
    target = store/'snapshots/test/daily.parquet'
    target.parent.mkdir(parents=True)
    target.write_bytes(b'not a verified Parquet file')
    state = feature_store_state(data)
    assert state['state'] == 'present_unverified' and state['date_max'] == '2026-11-02'
    assert not state['file_hash_verified'] and not state['raw_inputs_verified']
    for change in (dict(version='older'), dict(rows=0), dict(date_max='not-a-date'),
                   dict(data_file='../outside'), dict(provenance=dict(parquet_sha256='bad'))):
        write(store/'manifest.json', manifest | change)
        assert feature_store_state(data)['state'] == 'invalid'
    (store/'manifest.json').write_text('{"version": "a", "version": "b"}', encoding='utf-8')
    assert feature_store_state(data)['state'] == 'invalid'
    write(store/'manifest.json', manifest)
    def broken_path(*args, **kwargs):
        raise RuntimeError('unresolvable path')
    monkeypatch.setattr('features.store.feature_data_path', broken_path)
    assert feature_store_state(data)['state'] == 'invalid'


def test_offline_doctor_reports_missing_feature_manifest(setup, monkeypatch):
    from ops import doctor

    monkeypatch.setattr(doctor, 'check_python', lambda report: None)
    monkeypatch.setattr(doctor, 'check_packages', lambda report: None)
    monkeypatch.setattr(doctor, 'check_database', lambda report, **kwargs: None)
    report = doctor.run_doctor(include_network=False, read_only=True)
    check = next(item for item in report.checks if item.title == 'انبار فیچر')
    assert check.status == doctor.WARN and 'موجود نیست' in check.detail


def test_missing_feature_dependency_does_not_crash_the_health_check(setup, monkeypatch):
    import builtins

    private, _ = setup
    original = builtins.__import__
    def unavailable(name, *args, **kwargs):
        if name == 'features.daily':
            raise ModuleNotFoundError('feature dependency unavailable')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', unavailable)
    result = feature_store_state(private.parent)
    assert result['state'] == 'invalid' and result['version'] is None


def test_stale_future_naive_and_invalid_heartbeat_are_not_current(setup):
    private, at = setup
    path = private/'opportunity-radar/heartbeat.json'
    for timestamp in ((at-timedelta(seconds=181)).isoformat(), (at+timedelta(seconds=1)).isoformat(),
                      '2026-11-02T10:00:00', 'invalid'):
        write(path, dict(heartbeat_at=timestamp, price_session_date='bad'))
        result = health(private, at, radar=False)
        assert result['radar']['worker'] == 'stopped' and not result['radar']['heartbeat_fresh']
        assert result['radar']['price_session_date'] is None
    path.write_text('{"heartbeat_at":', encoding='utf-8')
    assert health(private, at)['radar']['heartbeat'] == 'invalid'


def test_board_status_and_codal_stop_fail_closed(setup):
    private, at = setup
    folder = private/'farabi-board/watch-test'
    write(private/'farabi-board/current.json', dict(url='http://127.0.0.1:1234', archive=str(folder)))
    write(folder/'status.json', dict(version='visible-board-alerts-v1', running=True,
          updated_at=at.isoformat(), boards=[{'ok':True}, {'ok':False}], alerts=[]))
    stop = private/'source-health/codal-documents/manual-stop.json'
    write(stop, dict(version='codal-documents-manual-stop-v1', blocked=True))
    result = health(private, at)
    assert result['board_state'] == 'current' and result['board_valid_extracts'] == 1
    assert result['codal_document_source'] == 'blocked'
    assert health(private, at+timedelta(seconds=4))['board_valid_extracts'] is None
    write(stop, dict(version='codal-documents-manual-stop-v1', blocked=False))
    assert health(private, at)['codal_document_source'] == 'invalid'
    with pytest.raises(ValueError, match='timezone'):
        health(private, at.replace(tzinfo=None))


def test_home_panel_surfaces_stopped_workers_and_blocked_documents(setup, monkeypatch):
    private, at = setup
    write(private/'source-health/codal-documents/manual-stop.json',
          dict(version='codal-documents-manual-stop-v1', blocked=True))
    monkeypatch.setattr('dashboard.command_center.now_tehran', lambda: at)
    monkeypatch.setattr('ops.jobs.job_status', lambda _: {'running':False})
    app = AppTest.from_string('from dashboard.command_center import command_center_panel\ncommand_center_panel()').run()
    assert not app.exception
    assert {m.label:m.value for m in app.metric} == {'فرایند رادار':'فعال بودن احراز نشد', 'پایش عمومی':'فعال بودن احراز نشد',
                                                   'پایش تابلو فارابی':'شروع نشده'}
    assert any('گردآوری اسناد کدال' in item.value for item in app.warning)
    assert any('انبار فیچر' in item.value and 'ساخته نشده' in item.value for item in app.warning)
    assert any('تازگی قیمت و صف' in item.value for item in app.caption)
    assert any('اجراهای دستی مستقل' in item.value for item in app.caption)
    assert any('مرورگر داخلی' in item.value and 'جداگانه' in item.value for item in app.caption)
