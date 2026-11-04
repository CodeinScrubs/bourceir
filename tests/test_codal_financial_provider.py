"""Financial extraction uses the same bounded public fetch and verification gate."""

import pytest

import brs
from config import settings
from tests.test_codal_documents import Response, provider
from tests.test_codal_tables import URL, html, raw_report


def financial_html():
    report = raw_report()
    report['type'] = 6
    return html(report)


def test_financial_provider_extracts_declared_cells_without_private_page_state():
    body = financial_html().replace('</body>', '<input name="__VIEWSTATE" value="SECRET_MARKER"></body>')
    response = Response(body.encode('utf-8'))
    api, session = provider(response)
    result = api.get_document(URL, financial_statement=True)
    assert result['status'] == 'captured'
    assert result['financial_data']['report']['tracing_no'] == 123
    assert result['financial_sheets']['version'] == 'codal-financial-sheets-v1'
    assert b'SECRET_MARKER' not in result['content']
    assert len(session.calls) == 1 and response.closed


@pytest.mark.parametrize('body', [b'<html><body>CAPTCHA</body></html>',
                                   b'<html><body>\xd8\xaa\xd8\xa3\xdb\x8c\xdb\x8c\xd8\xaf \xda\xa9\xd8\xa7\xd8\xb1\xd8\xa8\xd8\xb1 CAPTCHA</body></html>'])
def test_financial_captcha_is_never_parsed_or_retried(body):
    response = Response(body)
    api, session = provider(response)
    result = api.get_document(URL, financial_statement=True)
    assert result['status'] == 'verification_required'
    assert 'financial_data' not in result and len(session.calls) == 1 and response.closed


def test_extraction_modes_are_boolean_and_mutually_exclusive_before_network():
    api, session = provider()
    with pytest.raises(ValueError):
        api.get_document(URL, financial_statement='yes')
    with pytest.raises(ValueError):
        api.get_document(URL, financial_statement=True, structured_tables=True)
    assert not session.calls


def test_wrong_report_type_and_pdf_fail_without_financial_payload():
    api, _ = provider(Response(html().encode('utf-8')))
    result = api.get_document(URL, financial_statement=True)
    assert result['status'] == 'error' and 'financial_data' not in result
    api, _ = provider(Response(b'%PDF-1.7\n%%EOF'))
    result = api.get_document(URL, financial_statement=True)
    assert result['status'] == 'error' and 'financial_data' not in result


def test_financial_cli_captures_through_public_provider_only(tmp_path, monkeypatch, capsys):
    api, session = provider(Response(financial_html().encode('utf-8')))
    monkeypatch.setattr('data_collector.codal.CodalProvider', lambda: api)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'must-not-exist.db')
    assert brs.main(['codal-financial-cells', '--url', URL, '--expected-tracing-no', '123']) == 0
    assert 'CODALFIN-' in capsys.readouterr().out
    assert len(session.calls) == 1
    assert not settings.DB_PATH.exists()
