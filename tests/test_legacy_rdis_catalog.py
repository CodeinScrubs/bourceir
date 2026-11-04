import hashlib
import json

import pytest

from core.legacy_rdis_catalog import MAX_BYTES, parse_legacy_catalog
from ops import legacy_rdis_catalog as catalog_ops


def form(company='<option value="1">شركت یک</option>',
         report='<option value="4">گزارش افزایش سرمایه</option>',
         years='<option value="1380">1380</option>'):
    return (f'<select name="CompanyID"><option value="0"></option>{company}</select>'
            f'<select name="AnnouncementTypeID">{report}</select>'
            f'<select name="AnYear">{years}</select>').encode('cp1256', errors='xmlcharrefreplace')


def test_preserves_historical_choices_without_claiming_events_or_membership():
    result = parse_legacy_catalog(form())
    assert result['companies'] == [{'source_id': '1', 'label': 'شرکت یک'}]
    assert result['years'] == [{'source_id': '1380', 'label': '1380'}]
    assert result['capital_increase_events_count'] is None
    assert result['historical_exchange_membership_verified'] is False
    assert result['symbol_or_instrument_identity_verified'] is False
    assert result['report_availability_or_completeness_verified'] is False


@pytest.mark.parametrize('content', [
    b'', b'x' * (MAX_BYTES + 1),
    form(company='<option value="1">الف</option><option value="1">ب</option>'),
    form(company='<option value="1" value="2">الف</option>'),
    form(company='<option value="1">الف</option><option value="2"></option>'),
    form(years='<option value="1380">1381</option>'),
    form().replace(b'</select>', b'', 1),
], ids=['empty', 'oversize', 'duplicate_company', 'duplicate_attribute', 'missing_name',
        'ambiguous_year', 'unclosed_select'])
def test_rejects_incomplete_or_ambiguous_source(content):
    with pytest.raises(ValueError):
        parse_legacy_catalog(content)


def test_offline_catalogue_replays_source_and_rejects_a_changed_page(tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_ops.settings, 'DATA_DIR', tmp_path)
    source = tmp_path/'private'/'source'
    source.mkdir(parents=True)
    body = form()
    (source/'page.html').write_bytes(body)
    receipt = dict(archive_url='https://web.archive.org/web/20110103014730id_/'
                               'http://www.rdis.ir/CMPAnnouncements.asp',
                   retrieved_at='2020-01-01T00:00:00+03:30', status=200,
                   sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
                   historical_completeness_verified=False)
    (source/'receipt.json').write_text(json.dumps(receipt), encoding='utf-8')
    report = catalog_ops.build_catalog(source)
    assert report['catalogue']['company_count'] == 1
    assert report['source_page_sha256'] == receipt['sha256']
    assert not report['network_requested'] and not report['original_source_authenticity_verified']
    (source/'page.html').write_bytes(body+b'changed')
    with pytest.raises(ValueError, match='differ'):
        catalog_ops.build_catalog(source)
