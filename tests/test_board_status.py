import json
from datetime import datetime, timedelta

import pytest

from core.timeutils import TEHRAN_TZ
from ops.board_status import CURRENT_BOARD_VERSION, read_board_status


def fixture(tmp_path):
    root = tmp_path / 'private'
    archive = root / 'farabi-board' / 'watch-test'
    archive.mkdir(parents=True)
    at = datetime(2026, 10, 20, 10, 0, tzinfo=TEHRAN_TZ)
    pointer = dict(url='http://127.0.0.1:12345', archive=str(archive))
    status = dict(version='visible-board-alerts-v1', running=True, updated_at=at.isoformat(), boards=[], alerts=[])
    (root / 'farabi-board' / 'current.json').write_text(json.dumps(pointer), encoding='utf-8')
    (archive / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    return root, archive, at, pointer, status


def test_absent_status(tmp_path):
    assert read_board_status(tmp_path)['state'] == 'absent'


def test_dashboard_current_version_matches_watcher_engine():
    from pathlib import Path

    engine = Path(__file__).resolve().parents[1] / 'core' / 'board_alerts_v3.mjs'
    assert f"export const version = '{CURRENT_BOARD_VERSION}';" in engine.read_text(encoding='utf-8')


def test_current_stale_future_and_stopped(tmp_path):
    root, archive, at, _, status = fixture(tmp_path)
    assert read_board_status(root, at=at)['state'] == 'current'
    assert read_board_status(root, at=at+timedelta(seconds=4))['state'] == 'stale'
    assert read_board_status(root, at=at-timedelta(seconds=1))['state'] == 'stale'
    status['running'] = False
    (archive / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    assert read_board_status(root, at=at)['state'] == 'stale'


@pytest.mark.parametrize('version,expected', [
    ('visible-board-alerts-v1', 'current'),
    ('visible-board-alerts-v2', 'current'),
    ('visible-board-alerts-v3', 'current'),
    ('visible-board-alerts-unknown', 'invalid'),
    (None, 'invalid'),
])
def test_known_board_versions_and_unknown_version(tmp_path, version, expected):
    root, archive, at, _, status = fixture(tmp_path)
    status['version'] = version
    (archive / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    assert read_board_status(root, at=at)['state'] == expected


@pytest.mark.parametrize('version,warning_expected', [
    ('visible-board-alerts-v1', True),
    ('visible-board-alerts-v2', True),
    ('visible-board-alerts-v3', False),
])
def test_dashboard_warns_if_a_previous_watcher_is_still_running(tmp_path, monkeypatch, version, warning_expected):
    from streamlit.testing.v1 import AppTest

    from config import settings
    from core.timeutils import now_tehran_iso

    root, archive, _, _, status = fixture(tmp_path)
    status.update(version=version, updated_at=now_tehran_iso())
    (archive / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    monkeypatch.setattr(settings, 'DATA_DIR', root.parent)
    app = AppTest.from_string('from dashboard.board_alerts import board_alert_panel\nboard_alert_panel()').run()
    assert not app.exception
    assert any('نسخه قبلی' in warning.value for warning in app.warning) is warning_expected


def test_dashboard_missing_version_stays_explicit_without_crashing(isolated_board_monitor):
    from streamlit.testing.v1 import AppTest

    isolated_board_monitor(dict(state='current', url='http://127.0.0.1:1',
                               status=dict(boards=[], alerts=[])))
    app = AppTest.from_string('from dashboard.board_alerts import board_alert_panel\nboard_alert_panel()').run()
    assert not app.exception
    assert any('نسخه پایش نامعلوم' in warning.value for warning in app.warning)


@pytest.mark.parametrize('url', ['http://evil.test:12345', 'http://127.0.0.1:12345/?token=secret',
                                 'http://user@127.0.0.1:12345', 'https://127.0.0.1:12345'])
def test_only_local_panel_links(tmp_path, url):
    root, _, at, pointer, _ = fixture(tmp_path)
    pointer['url'] = url
    (root / 'farabi-board' / 'current.json').write_text(json.dumps(pointer), encoding='utf-8')
    assert read_board_status(root, at=at)['state'] == 'invalid'


def test_archive_must_be_private_and_timestamps_aware(tmp_path):
    root, archive, at, pointer, status = fixture(tmp_path)
    status['updated_at'] = '2026-10-20T10:00:00'
    (archive / 'status.json').write_text(json.dumps(status), encoding='utf-8')
    assert read_board_status(root, at=at)['state'] == 'invalid'
    pointer['archive'] = str(tmp_path)
    (root / 'farabi-board' / 'current.json').write_text(json.dumps(pointer), encoding='utf-8')
    assert read_board_status(root, at=at)['state'] == 'invalid'


def test_dashboard_monitor_uses_selected_data_directory_not_source_checkout(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from config import settings
    from core.timeutils import now_tehran_iso

    source = tmp_path / 'source'
    runtime = tmp_path / 'runtime'
    for directory, symbol in [(source / 'data', 'مبدأآزمون'), (runtime, 'اجرا‌آزمون')]:
        root = directory / 'private'
        archive = root / 'farabi-board/watch-test'
        archive.mkdir(parents=True)
        (root / 'farabi-board/current.json').write_text(json.dumps(dict(
            url='http://127.0.0.1:12345', archive=str(archive))), encoding='utf-8')
        (archive / 'status.json').write_text(json.dumps(dict(
            version='visible-board-alerts-v1', running=True, updated_at=now_tehran_iso(),
            boards=[dict(key='TEST00000001', symbol=symbol, ok=True, last_price=100, volume=1000)], alerts=[])),
            encoding='utf-8')
    monkeypatch.setattr(settings, 'BASE_DIR', source)
    monkeypatch.setattr(settings, 'DATA_DIR', runtime)
    app = AppTest.from_string('from dashboard.board_alerts import board_alert_panel\nboard_alert_panel()').run()
    assert not app.exception
    assert app.dataframe[0].value['نماد'].tolist() == ['اجرا‌آزمون']
