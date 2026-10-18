"""آزمون داده ساختاریافته فقط با داده آزمون و پوشه موقت؛ بدون شبکه یا حساب."""

import copy
import json
from datetime import datetime

import pytest

import brs
from config import settings
from core.codal_tables import extract_monthly_tables, numeric_candidate
from ops.codal_tables import capture_codal_tables, load_codal_tables
from tests.test_codal_documents import Response, provider

URL = 'https://codal.ir/Reports/Decision.aspx?LetterSerial=test-monthly'


def raw_report():
    def cell(address, row, column, value, dtype=None, **kw):
        return dict(address=address, rowSequence=row, columnSequence=column, rowSpan=1, colSpan=1,
                    value=value, dataTypeName=dtype, isVisible=True, cellGroupName='Body', **kw)
    return dict(type=58, period=1, tracingNo=123, title_Fa='گزارش تولیدی آزمون',
                periodEndToDate='1403/09/30', yearEndToDate='1403/12/30', publishDateTime='1403/10/01 12:00:00',
                isConsolidated=False, isAudited=False,
                sheets=[dict(code=1, title_Fa='برگه آزمون', tables=[dict(code=1, title_Fa='فروش', cells=[
                    cell('A1', 1, 1, 'مبلغ فروش (میلیون ریال)'), cell('B1', 1, 2, 'نرخ فروش (ریال)'),
                    cell('A2', 2, 1, '۰', 'Integer'), cell('B2', 2, 2, '', 'Float'),
                    cell('A3', 3, 1, '۱٬۲۳۴٫۵', 'Float'), cell('B3', 3, 2, '1,23', 'Integer'),
                ])])])


def html(raw=None, *, prefix='', suffix=''):
    data = json.dumps(raw_report() if raw is None else raw, ensure_ascii=False)
    return '<html><body><p>واحدها در جدول ذکر شده‌اند</p><script>' + prefix + 'var datasource = ' + data + ';' + suffix + '</script></body></html>'


@pytest.mark.parametrize('value,expected', [('۰', '0'), ('۱۲۳', '123'), ('۱٬۲۳۴٫۵', '1234.5'),
    ('(1,234)', '-1234'), ('+12.50', '12.50'), ('', None), ('-', None), ('1,23', None),
    ('1e3', None), ('12%', None), ('NaN', None), (None, None), (True, None), ('1 234', None)])
def test_numbers_remain_unitless_decimal_candidates_with_explicit_unknowns(value, expected):
    assert numeric_candidate(value) == expected


def test_extract_keeps_headers_scales_periods_zero_and_unknown_separate():
    output = extract_monthly_tables(html())
    cells = output['tables'][0]['cells']
    assert cells[0]['raw_value'] == 'مبلغ فروش (میلیون ریال)'
    assert cells[1]['raw_value'] == 'نرخ فروش (ریال)'
    assert cells[2]['numeric_candidate'] == '0' and cells[3]['numeric_candidate'] is None
    assert cells[4]['numeric_candidate'] == '1234.5' and cells[5]['numeric_candidate'] is None
    assert output['numeric_candidate_count'] == 2 and output['cell_count'] == 6
    assert output['report']['period_end_jalali'] == '1403/09/30'
    assert output['report']['is_consolidated'] is False
    assert not output['units_and_period_columns_verified'] and not output['accepted_for_features']
    assert not output['accepted_for_signals'] and not output['formulas_executed']


def test_javascript_comments_and_strings_do_not_become_datasource_and_formulas_never_execute():
    prefix = '// var datasource = evil();\n/* var datasource = 0; */\nconst note = "var datasource = false;";\n'
    raw = raw_report()
    raw['sheets'][0]['tables'][0]['cells'][3]['formula'] = 'fetch("https://evil.test")'
    output = extract_monthly_tables(html(raw, prefix=prefix, suffix='throw new Error("never execute");'))
    assert output['tables'][0]['cells'][3]['formula_present']
    assert output['tables'][0]['cells'][3]['numeric_candidate'] is None
    assert 'evil.test' not in json.dumps(output)


@pytest.mark.parametrize('mutation', ['type', 'period', 'duplicate_cell', 'duplicate_table', 'coordinate',
                                     'visibility', 'bad_date', 'no_sheets', 'nonobject_cell'])
def test_unsupported_or_inconsistent_structure_fails_closed(mutation):
    raw = raw_report()
    table = raw['sheets'][0]['tables'][0]
    if mutation == 'type':
        raw['type'] = 59
    elif mutation == 'period':
        raw['period'] = True
    elif mutation == 'duplicate_cell':
        table['cells'].append(copy.deepcopy(table['cells'][0]))
    elif mutation == 'duplicate_table':
        raw['sheets'][0]['tables'].append(copy.deepcopy(table))
    elif mutation == 'coordinate':
        table['cells'][0]['rowSequence'] = 1.5
    elif mutation == 'visibility':
        table['cells'][0]['isVisible'] = 'true'
    elif mutation == 'bad_date':
        raw['periodEndToDate'] = '1403/13/01'
    elif mutation == 'no_sheets':
        raw['sheets'] = []
    elif mutation == 'nonobject_cell':
        table['cells'][0] = None
    with pytest.raises(ValueError):
        extract_monthly_tables(html(raw))


@pytest.mark.parametrize('body', [
    '<html><body>var datasource = {}</body></html>',
    html(suffix='var datasource = {};'),
    html().replace('"tracingNo": 123', '"tracingNo": 123, "tracingNo": 999'),
    html().replace('var datasource = ', 'var datasource = JSON.parse('),
    html().replace(';</script>', ' || evil();</script>'),
    html().replace('"tracingNo": 123', '"tracingNo": NaN'),
    html(prefix='const pattern = /', suffix='/;'),
])
def test_non_json_duplicate_or_executable_initializer_rejected(body):
    with pytest.raises(ValueError):
        extract_monthly_tables(body)


def test_hidden_unknown_and_missing_values_are_preserved_without_fabricated_numbers():
    raw = raw_report()
    cells = raw['sheets'][0]['tables'][0]['cells']
    cells[2]['isVisible'] = False
    del cells[4]['value']
    del cells[5]['isVisible']
    output = extract_monthly_tables(html(raw))
    assert output['numeric_candidate_count'] == 0
    actual = output['tables'][0]['cells']
    assert actual[2]['raw_value'] == '۰' and actual[2]['visible'] is False
    assert not actual[4]['value_present'] and actual[4]['raw_value'] is None
    assert actual[5]['visible'] is None


def test_capture_archives_only_projected_cells_visible_text_and_code_without_backdating(tmp_path):
    api, session = provider(Response(html().replace('</body>', '<input name="__VIEWSTATE" value="SENSITIVE_SESSION_MARKER">'
        '<script>const password="SENSITIVE_SESSION_MARKER";</script></body>').encode()))
    result = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    loaded = load_codal_tables(result['folder'])
    assert result['status'] == 'captured' and result['cell_count'] == 6
    assert loaded['data']['numeric_candidate_count'] == 2
    assert datetime.fromisoformat(result['available_at']) >= datetime.fromisoformat(result['observed_at'])
    assert datetime.fromisoformat(result['available_at']).year > 2024
    assert not result['publication_time_verified'] and not result['numeric_facts_reviewed']
    assert len(session.calls) == 1
    for name in ('tables.json', 'report.txt'):
        assert 'SENSITIVE_SESSION_MARKER' not in (tmp_path / result['snapshot_id'] / name).read_text(encoding='utf-8')
    assert not list(tmp_path.rglob('*.html'))


@pytest.mark.parametrize('body,status', [('<html><body>CAPTCHA</body></html>', 'verification_required'),
                                       ('<html><body>بدون جدول</body></html>', 'error')])
def test_verification_or_missing_data_never_creates_success_archive(tmp_path, body, status):
    api, _ = provider(Response(body.encode()))
    result = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    assert result['status'] == status and not list(tmp_path.iterdir())


def test_wrong_tracing_number_rejected_before_publication(tmp_path):
    api, _ = provider(Response(html().encode()))
    with pytest.raises(ValueError, match='شماره پیگیری'):
        capture_codal_tables(URL, 999, provider=api, root=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('path', ['tables.json', 'report.txt', 'code/core/codal_tables.py'])
def test_modified_data_or_archived_code_is_detected(tmp_path, path):
    from pathlib import Path
    api, _ = provider(Response(html().encode()))
    record = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    target = Path(record['folder']) / path
    target.write_bytes(target.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='اثر انگشت'):
        load_codal_tables(record['folder'])


def test_cli_uses_explicit_tracing_private_archive_and_does_not_create_database(tmp_path, monkeypatch, capsys):
    api, _ = provider(Response(html().encode()))
    monkeypatch.setattr('data_collector.codal.CodalProvider', lambda: api)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path / 'must-not-exist.db')
    assert brs.main(['codal-tables', '--url', URL, '--expected-tracing-no', '123']) == 0
    assert len(list((tmp_path / 'private/codal-tables').glob('CODALTABLE-*/manifest.json'))) == 1
    assert not settings.DB_PATH.exists()
    output = json.loads(capsys.readouterr().out)
    assert output['accepted_for_features'] is False and output['numeric_facts_reviewed'] is False


def test_non_boolean_opt_in_is_rejected_before_network():
    api, session = provider()
    with pytest.raises(ValueError):
        api.get_document(URL, structured_tables='true')
    assert session.calls == []


@pytest.mark.parametrize('change', ['backdate', 'promotion', 'rehashed-number', 'traversal'])
def test_archive_cannot_backdate_promote_or_override_raw_values(tmp_path, change):
    import hashlib
    from pathlib import Path

    api, _ = provider(Response(html().encode()))
    record = capture_codal_tables(URL, 123, provider=api, root=tmp_path)
    folder = Path(record['folder'])
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if change == 'backdate':
        manifest['available_at'] = '2024-01-01T12:00:00+03:30'
    elif change == 'traversal':
        manifest['files_sha256']['../outside'] = 'a' * 64
    else:
        data = json.loads((folder / 'tables.json').read_text(encoding='utf-8'))
        if change == 'promotion':
            data['accepted_for_features'] = True
        else:
            data['tables'][0]['cells'][2]['numeric_candidate'] = '999'
        body = json.dumps(data, ensure_ascii=False).encode('utf-8')
        (folder / 'tables.json').write_bytes(body)
        manifest['files_sha256']['tables.json'] = hashlib.sha256(body).hexdigest()
    (folder / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError):
        load_codal_tables(folder)


def supported_month(month=9, revenue='120', rate='200', unit='میلیون ریال'):
    """جدول ساختگی فقط برای آزمون قرارداد؛ هیچ داده شرکت واقعی نیست."""
    raw = raw_report()
    current, prior = f'1403/{month:02d}/30', f'1403/{month-1:02d}/30'
    raw['periodEndToDate'] = current
    raw['tracingNo'] = month

    def c(address, row, column, value, header=False, colspan=1, rowspan=1):
        return dict(address=address, rowSequence=row, columnSequence=column, rowSpan=rowspan, colSpan=colspan,
                    value=value, dataTypeName=None if header or column == 1 else 'Float', isVisible=True,
                    cellGroupName='Header' if header else 'Body')
    raw['sheets'][0]['tables'][0]['cells'] = [
        c('A1', 1, 1, 'نام محصول', True, rowspan=2),
        c('B1', 1, 2, f'دوره یک ماهه منتهی به {current}', True, colspan=2),
        c('D1', 1, 4, f'ماه قبل {prior}', True),
        c('B2', 2, 2, f'مبلغ فروش ({unit})', True), c('C2', 2, 3, 'نرخ فروش (ریال / تن)', True),
        c('D2', 2, 4, f'مبلغ فروش ({unit})', True),
        c('A3', 3, 1, 'محصول آزمون'), c('B3', 3, 2, revenue), c('C3', 3, 3, rate), c('D3', 3, 4, '100'),
        c('A4', 4, 1, 'جمع کل'), c('B4', 4, 2, revenue), c('C4', 4, 3, ''), c('D4', 4, 4, '100'),
    ]
    return raw


def normalize_supported(raw):
    from core.codal_tables import normalize_monthly_tables
    return normalize_monthly_tables(extract_monthly_tables(html(raw)), available_at='2025-01-02T12:00:00+03:30',
        observed_at='2025-01-01T12:00:00+03:30', archived_at='2025-01-02T12:00:00+03:30', identity_verified=True)


@pytest.mark.parametrize('unit,scale', [('ریال', 1), ('هزار ریال', 1000), ('میلیون ریال', 1000000)])
def test_monthly_normalizer_scales_only_explicit_visible_units(unit, scale):
    from decimal import Decimal
    actual = normalize_supported(supported_month(unit=unit))
    assert actual['verified'], actual['reasons']
    assert Decimal(actual['totals']['current']['revenue_rial']) == 120 * scale
    assert actual['rows'][0]['cells']['current']['rate_rial_per_unit'] == '200'
    assert actual['periods'] == {'current': '1403/09/30', 'previous': '1403/08/30'}
    assert actual['accepted_for_features'] and not actual['accepted_for_signals']


@pytest.mark.parametrize('mutation', ['missing_unit', 'hidden_header', 'wrong_period', 'formula', 'wrong_total',
    'double_header', 'unknown_rate_unit', 'cumulative', 'duplicate_product', 'consolidated'])
def test_monthly_ambiguous_headers_formulas_and_failed_reconciliation_fail_closed(mutation):
    raw = supported_month()
    cells = raw['sheets'][0]['tables'][0]['cells']
    if mutation == 'missing_unit':
        cells[3]['value'] = 'مبلغ فروش'
    elif mutation == 'hidden_header':
        cells[1]['isVisible'] = False
    elif mutation == 'wrong_period':
        cells[2]['value'] = 'ماه قبل 1403/07/30'
    elif mutation == 'formula':
        cells[7]['formula'] = 'A2+B2'
    elif mutation == 'wrong_total':
        cells[-3]['value'] = '999'
    elif mutation == 'double_header':
        other = copy.deepcopy(cells[3])
        other['address'] = 'Z2'
        cells.append(other)
    elif mutation == 'unknown_rate_unit':
        cells[4]['value'] = 'نرخ فروش (ریال)'
    elif mutation == 'cumulative':
        cells[1]['value'] = 'دوره نه ماهه منتهی به 1403/09/30'
    elif mutation == 'consolidated':
        raw['isConsolidated'] = True
    else:
        cells[-4]['value'] = 'محصول آزمون'
    result = normalize_supported(raw)
    assert not result['verified'] and result['rows'] is None and not result['accepted_for_features']


def test_monthly_features_use_available_at_and_three_completed_prior_months_not_publication_claim():
    from decimal import Decimal

    from core.codal_tables import monthly_fundamental_features
    archives = []
    for month, rate in ((6, '100'), (7, '200'), (8, '300'), (9, '400')):
        data = extract_monthly_tables(html(supported_month(month=month, rate=rate)))
        archives.append(dict(data=data, manifest=dict(available_at=f'2025-01-{month:02d}T12:00:00+03:30',
            observed_at=f'2025-01-{month:02d}T11:00:00+03:30', archived_at=f'2025-01-{month:02d}T12:00:00+03:30',
            document_identity_verified=True, issuer_id='TEST-ONLY', identity_review_sha256='a' * 64)))
    early = monthly_fundamental_features(archives, decision_at='2025-01-05T12:00:00+03:30')
    assert not early['accepted_for_features'] and early['codal_rev_mom_ratio'] is None
    partial = monthly_fundamental_features(archives, decision_at='2025-01-08T12:00:00+03:30')
    assert Decimal(partial['codal_rev_mom_ratio']) == Decimal('1.2') and partial['codal_primary_rate_change'] is None
    full = monthly_fundamental_features(archives, decision_at='2025-01-09T12:00:00+03:30')
    assert Decimal(full['codal_primary_rate_change']) == 1  # 400 / mean(100,200,300) - 1
    assert full['available_at'] == '2025-01-09T12:00:00+03:30'
    assert full['sources'] == [6, 7, 8, 9]
    later = copy.deepcopy(archives[-1])
    later['manifest']['available_at'] = '2026-01-01T12:00:00+03:30'
    later['data']['report']['claimed_publish_time_jalali'] = '1391/01/01 00:00:00'
    assert monthly_fundamental_features(archives + [later], decision_at='2025-01-09T12:00:00+03:30') == full


def test_monthly_no_identity_or_backdated_availability_does_not_promote_features():
    from core.codal_tables import monthly_fundamental_features, normalize_monthly_tables
    data = extract_monthly_tables(html(supported_month()))
    manifest = dict(available_at='2025-01-01T12:00:00+03:30', observed_at='2025-01-01T13:00:00+03:30',
                    archived_at='2025-01-01T13:00:00+03:30', document_identity_verified=False)
    result = normalize_monthly_tables(data, **{k: manifest[k] for k in ('available_at', 'observed_at', 'archived_at')})
    assert not result['verified']
    assert not monthly_fundamental_features([dict(data=data, manifest=manifest)], decision_at='2025-02-01T00:00:00+03:30')['accepted_for_features']


def test_normalized_sidecar_never_rewrites_or_promotes_the_original_archive(tmp_path, monkeypatch):
    from pathlib import Path

    from ops.codal_tables import normalize_codal_archive

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    api, _ = provider(Response(html(supported_month()).encode()))
    record = capture_codal_tables(URL, 9, provider=api, root=tmp_path / 'private' / 'codal-tables')
    folder = Path(record['folder'])
    before = (folder / 'manifest.json').read_bytes(), (folder / 'tables.json').read_bytes()
    normalized = normalize_codal_archive(folder)
    assert normalized['normalized']['verified']
    assert not normalized['normalized']['accepted_for_features']
    assert before == ((folder / 'manifest.json').read_bytes(), (folder / 'tables.json').read_bytes())
    assert not load_codal_tables(folder)['data']['accepted_for_features']


def feature_months():
    return [dict(data=extract_monthly_tables(html(supported_month(month=month, rate=str(rate)))),
        manifest=dict(available_at=f'2025-01-{month:02d}T12:00:00+03:30',
            observed_at=f'2025-01-{month:02d}T11:00:00+03:30',
            archived_at=f'2025-01-{month:02d}T12:00:00+03:30',
            document_identity_verified=True, issuer_id='TEST-ONLY', identity_review_sha256='a' * 64))
        for month, rate in ((6, 100), (7, 200), (8, 300), (9, 400))]


def invalid_correction(archive):
    newer = copy.deepcopy(archive)
    newer['manifest'].update(available_at='2025-01-10T12:00:00+03:30',
        observed_at='2025-01-10T11:00:00+03:30', archived_at='2025-01-10T12:00:00+03:30')
    newer['data']['report']['tracing_no'] += 900
    newer['data']['tables'][0]['cells'][7]['formula_present'] = True
    return newer


def test_invalid_latest_current_month_blocks_older_valid_values_only_after_its_availability():
    from core.codal_tables import monthly_fundamental_features

    archives = feature_months()
    before = monthly_fundamental_features(archives, decision_at='2025-01-09T12:00:00+03:30')
    newer = invalid_correction(archives[-1])
    assert monthly_fundamental_features(archives + [newer], decision_at='2025-01-09T12:00:00+03:30') == before
    late = monthly_fundamental_features(archives + [newer], decision_at='2025-01-10T12:00:00+03:30')
    assert not late['accepted_for_features']
    assert late['codal_rev_mom_ratio'] is None and late['codal_primary_rate_change'] is None
    assert late['available_at'] == newer['manifest']['available_at'] and late['sources'] == [909]
    assert late['blocking_reasons']
    assert monthly_fundamental_features([newer] + archives[::-1], decision_at='2025-01-10T12:00:00+03:30') == late


def test_invalid_latest_prior_month_blocks_rate_without_reverting_or_destroying_current_revenue():
    from decimal import Decimal

    from core.codal_tables import monthly_fundamental_features

    archives = feature_months()
    late = monthly_fundamental_features(archives + [invalid_correction(archives[-2])],
        decision_at='2025-01-10T12:00:00+03:30')
    assert late['accepted_for_features'] and Decimal(late['codal_rev_mom_ratio']) == Decimal('1.2')
    assert late['codal_primary_rate_change'] is None
    assert late['sources'] == [6, 7, 908, 9]


def test_identity_attested_foreign_issuer_is_not_silently_ignored_when_normalization_fails():
    from core.codal_tables import monthly_fundamental_features

    archives = feature_months()
    foreign = invalid_correction(archives[-1])
    foreign['manifest']['issuer_id'] = 'OTHER-TEST-ONLY'
    with pytest.raises(ValueError, match='شرکت‌های متفاوت'):
        monthly_fundamental_features(archives + [foreign], decision_at='2025-01-10T12:00:00+03:30')


def test_equal_time_invalid_revision_is_ambiguous_instead_of_falling_back():
    from core.codal_tables import monthly_fundamental_features

    archives = feature_months()
    ambiguous = invalid_correction(archives[-1])
    ambiguous['manifest'] = copy.deepcopy(archives[-1]['manifest'])
    with pytest.raises(ValueError, match='زمان دسترسی یکسان'):
        monthly_fundamental_features(archives + [ambiguous], decision_at='2025-01-10T12:00:00+03:30')
