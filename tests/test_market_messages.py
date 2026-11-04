"""Only synthetic public-message data, temporary archives and fake transport."""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.market_messages import FALSE_FLAGS, message_request, project_messages
from core.timeutils import TEHRAN_TZ
from data_collector.tsetmc import TsetmcProvider
from ops.market_messages import (
    ARCHIVE_VERSION,
    SOURCE_FILES,
    SOURCE_FILES_V1,
    capture_message_window,
    load_message_window,
)

CODE = '123456789'
START = '2014-06-01'
END = '2014-09-01'


def row(day=20140728, identity=19029, title='عنوان آزمایشی', desc='توقف آزمایشی'):
    return dict(tseMsgIdn=identity, dEven=day, hEven=101333, tseTitle=title, tseDesc=desc, flow=0)


def body(rows):
    return json.dumps(dict(msg=rows), ensure_ascii=False).encode('utf-8')


def observation(content):
    return dict(source='tsetmc', ins_code=CODE, url=message_request(CODE, START, END),
                requested_at='2026-01-01T09:00:00+03:30', received_at='2026-01-01T09:00:01+03:30',
                http_status=200, response_bytes=len(content), body_sha256=hashlib.sha256(content).hexdigest(),
                content=content)


def test_only_selected_preholdout_text_can_leave_the_provider(monkeypatch):
    source = body([row(), row(20250321, 200, 'SEALED_TITLE', 'SEALED_CONTENT'),
                   row(20150901, 300, 'OUTSIDE_TITLE', 'OUTSIDE_CONTENT')])
    api = TsetmcProvider(realtime=True)
    monkeypatch.setattr(api, '_get_quote_context_json', lambda *a, **k: observation(source))
    try:
        result = api.get_historical_market_messages(CODE, START, END)
    finally:
        api.close()
    serialized = json.dumps(result, ensure_ascii=False)
    assert 'SEALED_' not in serialized and 'OUTSIDE_' not in serialized and 'content' not in result['receipt']
    assert result['projection']['messages'] == [row()]
    assert all(result['projection'][flag] is False for flag in FALSE_FLAGS)


def test_source_receipt_day_is_tehran_local_and_checked_before_row_text(monkeypatch):
    before = datetime(2014, 7, 27, 23, 30, tzinfo=TEHRAN_TZ).astimezone(timezone.utc).isoformat()
    after = datetime(2014, 7, 28, 0, 30, tzinfo=TEHRAN_TZ).astimezone(timezone.utc).isoformat()
    assert datetime.fromisoformat(after).date().isoformat() == '2014-07-27'
    assert project_messages(body([row()]), CODE, START, END, received_at=after)['messages'] == [row()]
    monkeypatch.setattr('core.market_messages._selected_row', lambda _: pytest.fail('future text was inspected'))
    with pytest.raises(ValueError, match='later than'):
        project_messages(body([row()]), CODE, START, END, received_at=before)
    with pytest.raises(ValueError, match='timezone-aware'):
        project_messages(body([row()]), CODE, START, END, received_at='2014-07-28T00:30:00')


def test_future_source_date_outside_requested_window_fails_before_text(monkeypatch):
    monkeypatch.setattr('core.market_messages._selected_row', lambda _: pytest.fail('future text was inspected'))
    with pytest.raises(ValueError, match='later than'):
        project_messages(body([row(20250321, 200, 'SEALED_TITLE', 'SEALED_CONTENT')]), CODE, START, END,
                         received_at='2020-01-01T10:00:00+03:30')


@pytest.mark.parametrize('bad', [True, None, 0, 20140230, '20140728', 20241301])
def test_unknown_date_anywhere_blocks_projection_without_reading_its_text(bad):
    with pytest.raises(ValueError):
        project_messages(body([row(), row(day=bad, title='must-not-be-used')]), CODE, START, END,
                         received_at='2026-01-01T09:00:01+03:30')


@pytest.mark.parametrize('change', ['duplicate', 'clock', 'extra', 'bool_id', 'missing', 'nan'])
def test_selected_malformed_rows_fail_closed(change):
    data = row()
    rows = [data]
    if change == 'duplicate':
        rows.append(row())
    elif change == 'clock':
        data['hEven'] = 126099
    elif change == 'extra':
        data['unknown'] = 'x'
    elif change == 'bool_id':
        data['tseMsgIdn'] = True
    elif change == 'missing':
        del data['tseDesc']
    else:
        data['flow'] = float('nan')
    with pytest.raises(ValueError):
        project_messages(body(rows), CODE, START, END, received_at='2026-01-01T09:00:01+03:30')


@pytest.mark.parametrize('code,start,end', [(CODE, START, '2025-03-22'),
    (CODE, '2025-03-21', '2025-03-22'), ('000123', START, END),
    (True, START, END), (CODE, '2014-02-30', END)])
def test_invalid_scope_is_rejected_before_transport(monkeypatch, code, start, end):
    api = TsetmcProvider(realtime=True)
    monkeypatch.setattr(api, '_get_quote_context_json', lambda *a, **k: pytest.fail('network must not start'))
    try:
        with pytest.raises(ValueError):
            api.get_historical_market_messages(code, start, end)
    finally:
        api.close()


def test_archive_has_selected_text_and_code_but_no_full_response_or_backdating(tmp_path, monkeypatch):
    source = body([row(), row(20250321, 200, 'SEALED_TITLE', 'SEALED_CONTENT')])
    api = TsetmcProvider(realtime=True)
    monkeypatch.setattr(api, '_get_quote_context_json', lambda *a, **k: observation(source))
    try:
        result = capture_message_window(CODE, START, END, provider=api, root=tmp_path)
    finally:
        api.close()
    archive = load_message_window(result['folder'])
    assert archive['projection']['messages'] == [row()]
    assert datetime.fromisoformat(result['available_at']) >= datetime.fromisoformat(result['receipt']['received_at'])
    assert not result['complete_history_verified'] and not result['historical_publication_verified']
    folder = Path(result['folder'])
    for path in folder.rglob('*'):
        if path.is_file():
            assert b'SEALED_TITLE' not in path.read_bytes() and b'SEALED_CONTENT' not in path.read_bytes()
    assert not list(folder.rglob('*.html'))


def test_v2_hashes_expanded_sources_and_v1_remains_readable(tmp_path, monkeypatch):
    api = TsetmcProvider(realtime=True)
    monkeypatch.setattr(api, '_get_quote_context_json', lambda *a, **k: observation(body([row()])))
    try:
        result = capture_message_window(CODE, START, END, provider=api, root=tmp_path)
    finally:
        api.close()
    folder = Path(result['folder'])
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    assert manifest['version'] == ARCHIVE_VERSION == 'market-message-window-archive-v2'
    added = set(SOURCE_FILES) - set(SOURCE_FILES_V1)
    assert added == {'core/daily_publications.py', 'core/quote_context.py', 'ops/codal_documents.py'}
    assert set(manifest['files_sha256']) == {'projection.json'} | {'code/' + f for f in SOURCE_FILES}
    for name in added:
        target = folder / 'code' / name
        original = target.read_bytes()
        target.write_bytes(original + b' ')
        with pytest.raises(ValueError, match='hash'):
            load_message_window(folder)
        target.write_bytes(original)
    # Synthetic v1 fixture: old schema with precisely its original source set.
    manifest['version'] = 'market-message-window-archive-v1'
    for name in added:
        del manifest['files_sha256']['code/' + name]
        (folder / 'code' / name).unlink()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    loaded = load_message_window(folder)
    assert loaded['manifest']['version'] == 'market-message-window-archive-v1'
    assert loaded['projection']['messages'] == [row()]


@pytest.mark.parametrize('kind', ['projection', 'source', 'promote', 'backdate',
                                  'future_archived', 'future_available', 'outer_rehash'])
def test_review_detects_tampering_and_rejects_scope_or_flag_promotion(tmp_path, monkeypatch, kind):
    source = body([row()])
    api = TsetmcProvider(realtime=True)
    monkeypatch.setattr(api, '_get_quote_context_json', lambda *a, **k: observation(source))
    try:
        result = capture_message_window(CODE, START, END, provider=api, root=tmp_path)
    finally:
        api.close()
    folder = Path(result['folder'])
    manifest_path = folder / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    projection = folder / 'projection.json'
    if kind == 'projection':
        projection.write_bytes(projection.read_bytes() + b' ')
    elif kind == 'source':
        code = folder / 'code' / 'core' / 'market_messages.py'
        code.write_bytes(code.read_bytes() + b' ')
    elif kind == 'promote':
        manifest['complete_history_verified'] = True
    elif kind == 'backdate':
        manifest['available_at'] = '2025-01-01T00:00:00+03:30'
    elif kind == 'future_archived':
        manifest['archived_at'] = manifest['available_at'] = '2099-01-01T00:00:00+03:30'
    elif kind == 'future_available':
        manifest['available_at'] = '2099-01-01T00:00:00+03:30'
    else:
        data = json.loads(projection.read_text(encoding='utf-8'))
        data['messages'][0]['dEven'] = 20250321
        projection.write_text(json.dumps(data), encoding='utf-8')
        manifest['files_sha256']['projection.json'] = hashlib.sha256(projection.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError):
        load_message_window(folder)


def test_source_failure_never_becomes_empty_history_or_creates_archive(tmp_path):
    class Unavailable:
        def get_historical_market_messages(self, *_):
            return None
    result = capture_message_window(CODE, START, END, provider=Unavailable(), root=tmp_path)
    assert result['status'] == 'unavailable' and not result['complete_history_verified']
    assert not list(tmp_path.iterdir())


def test_archive_rejects_selected_message_later_than_receipt_before_publication(tmp_path):
    source = body([row()])
    projected = project_messages(source, CODE, START, END, received_at='2026-01-01T09:00:01+03:30')
    receipt = observation(source)
    receipt.pop('content')
    receipt['requested_at'] = receipt['received_at'] = '2014-07-27T18:00:00+00:00'

    class Provider:
        def get_historical_market_messages(self, *_):
            return {'receipt': receipt, 'projection': projected}

    with pytest.raises(ValueError, match='outside the window'):
        capture_message_window(CODE, START, END, provider=Provider(), root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_rehashed_archive_cannot_move_receipt_before_native_message_day(tmp_path, monkeypatch):
    source = body([row()])
    api = TsetmcProvider(realtime=True)
    monkeypatch.setattr(api, '_get_quote_context_json', lambda *a, **k: observation(source))
    try:
        result = capture_message_window(CODE, START, END, provider=api, root=tmp_path)
    finally:
        api.close()
    manifest_path = Path(result['folder']) / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['receipt']['requested_at'] = manifest['receipt']['received_at'] = '2014-07-27T18:00:00+00:00'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='outside the window'):
        load_message_window(result['folder'])


def test_falsey_injected_provider_never_starts_network_or_is_closed(tmp_path, monkeypatch):
    class FalseyProvider:
        calls = 0
        closed = False

        def __bool__(self):
            return False

        def get_historical_market_messages(self, *_):
            self.calls += 1
            return None

        def close(self):
            self.closed = True

    provider = FalseyProvider()
    monkeypatch.setattr('data_collector.tsetmc.TsetmcProvider',
                        lambda **_: pytest.fail('default provider must not start'))
    result = capture_message_window(CODE, START, END, provider=provider, root=tmp_path)
    assert result['status'] == 'unavailable' and provider.calls == 1 and not provider.closed
    assert not list(tmp_path.iterdir())
