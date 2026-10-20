"""برداشت عمومی محدود، حفظ زمان واقعی مشاهده و کشف دستکاری آرشیو؛ فقط داده آزمون."""

import hashlib
import json
from datetime import datetime

import pytest
import requests

from core.codal_documents import visible_document
from data_collector.codal import CodalProvider
from ops.codal_documents import capture_codal_document, load_codal_document, recent_codal_documents

URL = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=test'
HTML = '<html><head><title>عنوان</title></head><body><table><tr><td>فروش</td><td>۱۲۳ ریال</td></tr></table></body></html>'


class Response:
    def __init__(self, body=HTML.encode(), status=200, headers=None):
        self.body, self.status_code, self.headers, self.closed = body, status, headers or {}, False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('test')

    def iter_content(self, chunk_size):
        for index in range(0, len(self.body), chunk_size):
            yield self.body[index:index+chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses, self.headers, self.calls = list(responses), {}, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def provider(*responses):
    session = Session(*responses)
    return CodalProvider(session=session), session


@pytest.mark.parametrize('url', [
    'https://evil.test/Reports/Decision.aspx', 'http://codal.ir/Reports/Decision.aspx',
    'https://codal.ir.evil.test/Reports/Decision.aspx', 'https://user:pass@codal.ir/Reports/Decision.aspx',
    'https://codal.ir:8443/Reports/Decision.aspx', 'https://127.0.0.1/Reports/Decision.aspx',
    'https://codal.ir/Login.aspx', 'https://codal.ir/Reports/../Decision.aspx',
    URL+'&access_token=secret', URL+'&%74oken=secret', 'https://codal.ir\\@evil.test/Reports/Decision.aspx',
    'https://codal.ir/Reports/Decision.aspx\r\nAuthorization:secret',
])
def test_untrusted_urls_rejected_before_network(url):
    api, session = provider()
    with pytest.raises(ValueError):
        api.get_document(url)
    assert session.calls == []


def test_url_from_listing_preserves_real_absolute_and_relative_document_links():
    from tests.test_codal_ai import RAW_LETTER

    result = CodalProvider.normalize_letter(dict(RAW_LETTER, Url=URL,
                                                AttachmentUrl='/Reports/Attachment.aspx?LetterSerial=test',
                                                PdfUrl='DownloadFile.aspx?hs=test&ft=1005'))
    assert result['url'] == URL
    assert result['attachment_url'].startswith('https://codal.ir/Reports/Attachment.aspx?')
    assert result['pdf_url'] == 'https://codal.ir/DownloadFile.aspx?hs=test&ft=1005'


def test_hidden_inputs_scripts_and_session_fields_are_never_in_visible_archive():
    html = HTML.replace('</body>', '<script>token=secret</script><input name="__VIEWSTATE" value="secret">'
                       '<div hidden>password=secret</div><p style="display:none">session=secret</p>'
                       '<a href="DownloadFile.aspx?hs=test">پیوست</a><a href="https://evil.test">غیررسمی</a></body>')
    text, links = visible_document(html, URL)
    assert '۱۲۳ ریال' in text and 'فروش' in text
    assert 'secret' not in text and '__VIEWSTATE' not in text
    assert links == ['https://codal.ir/Reports/DownloadFile.aspx?hs=test']


def test_static_attachment_download_row_is_extracted_without_executing_javascript():
    html = '''<html><body><table>
        <tr onclick="window.open('DownloadFile.aspx?id=safe-id');"><td>پیوست رسمی</td></tr>
        <tr onclick="window.open('https://evil.test/file.pdf');"><td>غیررسمی</td></tr>
        <tr onclick="window.open('DownloadFile.aspx?id=other'); fetch('/secret');"><td>اسکریپت</td></tr>
        </table></body></html>'''
    _, links = visible_document(html, 'https://codal.ir/Reports/Attachment.aspx?LetterSerial=test')
    assert links == ['https://codal.ir/Reports/DownloadFile.aspx?id=safe-id']


def test_redirect_to_external_is_never_followed_or_archived(tmp_path):
    response = Response(status=302, headers={'Location': 'https://evil.test/file.pdf'})
    api, session = provider(response)
    result = capture_codal_document(URL, provider=api, root=tmp_path)
    assert result['status'] == 'error' and len(session.calls) == 1 and response.closed
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('body,status', [
    ('<html><body>تأیید کاربر CAPTCHA</body></html>'.encode(), 'verification_required'),
    (b'%PDF-1.7 incomplete', 'error'), (b'{"error":"unavailable"}', 'error'),
    (b'<html><body><script>dynamic only</script></body></html>', 'error'),
])
def test_missing_document_never_creates_success(tmp_path, body, status):
    response = Response(body)
    api, session = provider(response)
    result = capture_codal_document(URL, provider=api, root=tmp_path)
    assert result['status'] == status and len(session.calls) == 1 and response.closed
    assert 'content' not in result and not list(tmp_path.iterdir())


def test_download_limit_closes_response():
    response = Response(b'<html><body>'+b'x'*10000+b'</body></html>')
    api, _ = provider(response)
    result = api.get_document(URL, max_bytes=1000)
    assert result['status'] == 'error' and response.closed


def test_partial_http_response_is_not_archived(tmp_path):
    response = Response(status=206)
    api, _ = provider(response)
    assert capture_codal_document(URL, provider=api, root=tmp_path)['status'] == 'error'
    assert response.closed and not list(tmp_path.iterdir())


def test_redirect_limit_is_bounded_and_all_responses_close():
    responses = [Response(status=302, headers={'Location': URL}) for _ in range(4)]
    api, session = provider(*responses)
    assert api.get_document(URL)['status'] == 'error'
    assert len(session.calls) == 4 and all(row.closed for row in responses)


@pytest.mark.parametrize('route',['InterimStatement.aspx','MonthlyActivity.aspx','ExtraAssembly.aspx'])
def test_official_new_report_redirect_can_be_archived(tmp_path,route):
    target='/Reports/'+route+'?LetterSerial=test'
    redirect=Response(status=302,headers={'Location':target});body=Response()
    api,session=provider(redirect,body)
    result=capture_codal_document(URL,provider=api,root=tmp_path)
    assert result['status']=='captured' and result['effective_url']=='https://codal.ir'+target
    assert len(session.calls)==2 and all(c[1]['allow_redirects'] is False for c in session.calls)
    assert redirect.closed and body.closed and not result['text_extraction_complete']


@pytest.mark.parametrize('route',['InterimStatement.aspx','MonthlyActivity.aspx','ExtraAssembly.aspx'])
def test_official_new_report_root_path_is_not_assumed_public(route):
    api,session=provider()
    with pytest.raises(ValueError):api.get_document('https://codal.ir/'+route+'?LetterSerial=test')
    assert session.calls==[]


@pytest.mark.parametrize('route',['InterimStatement.aspx','MonthlyActivity.aspx','ExtraAssembly.aspx'])
@pytest.mark.parametrize('unsafe',['https://evil.test/Reports/','https://codal.ir/Reports/'])
def test_official_new_report_does_not_allow_external_or_session_redirects(route,unsafe):
    target=unsafe+route+'?LetterSerial=test'+('&session=secret' if 'codal.ir' in unsafe else '')
    redirect=Response(status=302,headers={'Location':target});api,session=provider(redirect)
    result=api.get_document(URL)
    assert result['status']=='error' and len(session.calls)==1 and redirect.closed


def test_revisions_keep_both_contents_with_real_capture_time_and_check_tampering(tmp_path):
    api, _ = provider(Response(), Response(HTML.replace('۱۲۳', '۴۵۶').encode()))
    a = capture_codal_document(URL, provider=api, root=tmp_path)
    b = capture_codal_document(URL, provider=api, root=tmp_path)
    assert a['snapshot_id'] != b['snapshot_id'] and a['content_sha256'] != b['content_sha256']
    assert len(recent_codal_documents(root=tmp_path)) == 2
    assert datetime.fromisoformat(a['available_at']) >= datetime.fromisoformat(a['observed_at'])
    assert not a['numeric_facts_reviewed'] and not a['publication_time_verified']
    assert not a['document_identity_verified'] and not a['text_extraction_complete']
    folder = tmp_path / a['snapshot_id']
    assert load_codal_document(folder)['content_sha256'] == a['content_sha256']
    (folder / 'report.txt').write_text('دستکاری', encoding='utf-8')
    with pytest.raises(ValueError, match='اثر انگشت'):
        load_codal_document(folder)
    assert recent_codal_documents(root=tmp_path)[1]['status'] == 'invalid'


@pytest.mark.parametrize('trailer', [b'', b'\r\n', b'\r%Scanner\r\n'])
def test_pdf_archived_as_bytes_without_claiming_numeric_extraction(tmp_path, trailer):
    content = b'%PDF-1.7\n' + b'x'*64 + b'\n%%EOF' + trailer
    api, _ = provider(Response(content))
    row = capture_codal_document(URL, provider=api, root=tmp_path)
    assert row['format'] == 'pdf' and row['content_sha256'] == hashlib.sha256(content).hexdigest()
    assert not row['numeric_facts_reviewed']


def test_pdf_followed_by_a_server_error_is_not_a_clean_document(tmp_path):
    content = b'%PDF-1.4\n' + b'x'*64 + b'\n%%EOF\n<html>Server Error</html>'
    api, _ = provider(Response(content))
    result = capture_codal_document(URL, provider=api, root=tmp_path)
    assert result['status'] == 'error' and result['error_reason'] == 'invalid_pdf_ending'
    assert not list(tmp_path.iterdir())


def test_archiving_an_old_response_never_backdates_availability(tmp_path, monkeypatch):
    from ops import codal_documents

    api, _ = provider(Response())
    result = api.get_document(URL)
    result.update(requested_at='2020-01-01T09:00:00+03:30', observed_at='2020-01-01T09:00:01+03:30')
    monkeypatch.setattr(api, 'get_document', lambda url: result)
    monkeypatch.setattr(codal_documents, 'now_tehran_iso', lambda: '2026-10-15T10:00:00+03:30')
    row = capture_codal_document(URL, provider=api, root=tmp_path)
    assert row['available_at'] == '2026-10-15T10:00:00+03:30'


def test_old_or_naive_availability_and_traversal_are_rejected(tmp_path):
    api, _ = provider(Response())
    row = capture_codal_document(URL, provider=api, root=tmp_path)
    folder = tmp_path / row['snapshot_id']
    path = folder / 'manifest.json'
    original = json.loads(path.read_text(encoding='utf-8'))
    for changes in ({'available_at': '2020-01-01T09:00:00+03:30'},
                    {'available_at': '2026-10-15T09:00:00'}, {'content_file': '../report.txt'}):
        path.write_text(json.dumps(dict(original, **changes)), encoding='utf-8')
        with pytest.raises(ValueError):
            load_codal_document(folder)


def test_source_change_during_capture_does_not_publish(tmp_path, monkeypatch):
    api, _ = provider(Response())
    original = api.get_document

    def changed(url):
        result = original(url)
        # تغییر فایل واقعی پروژه مجاز نیست؛ خواندن منبع محفوظ را در آزمون مختل می‌کنیم.
        from pathlib import Path
        read = Path.read_bytes
        monkeypatch.setattr(Path, 'read_bytes', lambda path: b'changed' if path.name == 'codal.py' else read(path))
        return result

    monkeypatch.setattr(api, 'get_document', changed)
    with pytest.raises(RuntimeError, match='کد دریافت'):
        capture_codal_document(URL, provider=api, root=tmp_path)
    assert not list(tmp_path.iterdir())


def test_archive_panel_shows_valid_and_corrupt_versions_without_network(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from config import settings

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    api, _ = provider(Response())
    row = capture_codal_document(URL, provider=api)
    script = ('import streamlit as st\nfrom dashboard.codal_documents import document_archive_panel\n'
              'document_archive_panel(lambda *args: st.caption("وظیفه آزمون"))')
    app = AppTest.from_string(script).run()
    assert not app.exception and len(app.get('download_button')) == 1
    app.text_input[0].set_value('https://evil.test/test').run()
    assert not app.exception and app.warning
    app.text_input[0].set_value(URL).run()
    assert not app.exception and any('وظیفه آزمون' in value.value for value in app.caption)
    (tmp_path / 'private/codal-documents' / row['snapshot_id'] / 'report.txt').write_text('bad', encoding='utf-8')
    app.run()
    assert not app.exception and any('ناسازگار' in value.value for value in app.warning)
    assert len(app.get('download_button')) == 0
