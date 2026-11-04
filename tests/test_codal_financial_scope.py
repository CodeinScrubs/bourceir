"""Synthetic metadata stays in tests; reporting scope is never inferred from symbol alone."""

import copy

import pytest

from core.codal_financial_scope import FALSE_FLAGS, classify_financial_letter, financial_scope_catalog

TITLE = 'اطلاعات و صورت‌های مالی میاندوره‌ای دوره ۶ ماهه منتهی به ۱۴۰۲/۰۶/۳۱ (حسابرسی نشده)'


def letter(**changes):
    return dict(tracing_no=123, symbol_fa='آزمون', company_name='ناشر آزمون',
        letter_code='ن-10', source_estimate=False, source_published_at='2023-10-22T18:00:00+03:30',
        url='https://codal.ir/Reports/Decision.aspx?LetterSerial=test', title=TITLE) | changes


def classify(value):
    return classify_financial_letter(value, expected_symbol='آزمون', cutoff='2025-03-21')


@pytest.mark.parametrize(('suffix', 'scope', 'entity', 'candidate'), [
    ('', 'issuer_unqualified', None, True),
    (' (شرکت نمونه دیگر)', 'named_entity', 'نمونه دیگر', False),
    (' (شرکت ناشر آزمون)', 'named_entity', 'ناشر آزمون', False),
])
def test_publisher_key_is_not_reporting_entity_proof(suffix, scope, entity, candidate):
    value = letter(title=TITLE+suffix)
    before = copy.deepcopy(value)
    result = classify(value)
    assert result['status'] == 'metadata_only'
    assert result['scope_claim'] == scope and result['named_entity_claim'] == entity
    assert result['issuer_statement_candidate'] is candidate and result['cumulative_months'] == 6
    assert result['period_end_iso'] == '2023-09-22'
    assert value == before
    assert all(result[k] is False for k in FALSE_FLAGS)


def test_consolidated_named_entity_does_not_become_issuer_consolidated():
    title = TITLE.replace('میاندوره‌ای', 'میاندوره‌ای تلفیقی')
    issuer = classify(letter(title=title))
    named = classify(letter(title=title+' (شرکت نمونه دیگر)'))
    assert issuer['scope_claim'] == 'issuer_consolidated'
    assert named['scope_claim'] == 'named_entity' and named['consolidated_claim'] is True
    assert not named['issuer_statement_candidate']


def test_unqualified_title_does_not_prove_standalone_or_absent_consolidation():
    result = classify(letter())
    assert result['scope_claim'] == 'issuer_unqualified'
    assert result['consolidated_claim'] is None
    assert result['standalone_reporting_verified'] is False
    assert result['reporting_entity_verified'] is False


def test_annual_title_does_not_invent_a_twelve_month_duration():
    result = classify(letter(title='صورت‌های مالی سال مالی منتهی به ۱۴۰۱/۱۲/۲۹ (حسابرسی شده)'))
    assert result['status'] == 'metadata_only' and result['period_kind_claim'] == 'annual'
    assert result['cumulative_months'] is None and result['audit_claim'] == 'audited'


@pytest.mark.parametrize('tail', [' (ناحیه ویژه)', ' (اصلاحیه)(اصلاحیه)',
    ' (شرکت الف) (شرکت ب)', ' (شرکت )', ' (شرکت الف (ب))', ' توضیح اضافه'])
def test_unknown_scope_qualifiers_stay_unresolved_and_in_denominator(tail):
    result = financial_scope_catalog([letter(title=TITLE+tail)], expected_symbol='آزمون', cutoff='2025-03-21')
    assert result['financial_letters'] == 1 and result['scope_claim_counts'] == {'unresolved': 1}
    assert result['groups'] == [] and result['issuer_statement_candidates'] == 0
    assert result['records'][0]['reporting_key'] is None


@pytest.mark.parametrize('title', [
    'گزارش ناشناخته', TITLE.replace('۶ ماهه','۰ ماهه'), TITLE.replace('۶ ماهه','۱۳ ماهه'),
    TITLE.replace('۱۴۰۲/۰۶/۳۱','۱۴۰۲/۱۳/۳۱'), TITLE.replace('۱۴۰۲/۰۶/۳۱','۱۴۰۳/۰۶/۳۱')])
def test_unsupported_period_and_title_do_not_make_scope_or_values(title):
    result = classify(letter(title=title))
    assert result['status'] == 'unresolved' and result['reasons']
    assert result['scope_claim'] is None and result['financial_values_read'] is False


@pytest.mark.parametrize('changes', [dict(symbol_fa='نماد دیگر'), dict(letter_code='ن-30'),
    dict(source_estimate=True), dict(source_estimate=None), dict(tracing_no=True),
    dict(tracing_no=0), dict(company_name=''), dict(title=None),
    dict(source_published_at='2023-10-22T18:00:00'), dict(url='https://evil.example/report')])
def test_invalid_native_identity_metadata_is_rejected(changes):
    with pytest.raises(ValueError):
        classify(letter(**changes))


@pytest.mark.parametrize('published', ['2025-03-21T00:00:00+03:30', '2025-03-20T23:30:00-02:00'])
def test_sealed_publication_is_rejected_before_title_interpretation(published):
    class Guarded(dict):
        def get(self, key, *args):
            assert key != 'title', 'held title must not be interpreted'
            return super().get(key, *args)
    with pytest.raises(ValueError, match='sealed'):
        classify(Guarded(letter(source_published_at=published)))


def test_catalog_does_not_merge_audit_consolidation_duration_or_named_entities():
    titles = [TITLE, TITLE.replace('حسابرسی نشده','حسابرسی شده'),
        TITLE.replace('میاندوره‌ای','میاندوره‌ای تلفیقی'), TITLE+' (شرکت دیگر)',
        TITLE.replace('۶ ماهه','۳ ماهه'),
        'صورت‌های مالی سال مالی منتهی به ۱۴۰۲/۰۶/۳۱ (حسابرسی نشده)']
    records = [letter(tracing_no=i+1,title=t) for i,t in enumerate(titles)]
    result = financial_scope_catalog(records, expected_symbol='آزمون', cutoff='2025-03-21')
    assert len(result['groups']) == len(records)
    assert result['issuer_statement_candidates'] == 5
    assert result['financial_values_read'] is False and result['values_or_growth_computed'] is False


def test_corrections_are_retained_without_invented_native_links_or_asof_selection():
    records = [letter(), letter(tracing_no=456,title=TITLE+'(اصلاحیه)',
        source_published_at='2023-10-23T18:00:00+03:30')]
    result = financial_scope_catalog(records, expected_symbol='آزمون', cutoff='2025-03-21')
    group, = result['groups']
    assert group['tracing_numbers'] == [123,456]
    assert group['original_title_claims'] == group['correction_title_claims'] == 1
    assert not group['native_revision_links_verified'] and not group['publication_claim_ambiguous']
    records[1]['source_published_at'] = records[0]['source_published_at']
    result = financial_scope_catalog(records, expected_symbol='آزمون', cutoff='2025-03-21')
    assert result['groups'][0]['publication_claim_ambiguous'] is True


def test_duplicate_traces_and_conflicting_publisher_names_require_reconciliation():
    for records in ([letter(),letter()], [letter(),letter(tracing_no=456,company_name='ناشر متفاوت')]):
        with pytest.raises(ValueError):
            financial_scope_catalog(records, expected_symbol='آزمون', cutoff='2025-03-21')


@pytest.mark.parametrize(('letters','symbol','cutoff'), [([], '', '2025-03-21'),
    ([], 'آزمون', 'wrong'), ([False], 'آزمون', '2025-03-21'), ({}, 'آزمون', '2025-03-21')])
def test_empty_and_malformed_catalogs_still_validate_the_contract(letters,symbol,cutoff):
    with pytest.raises(ValueError):
        financial_scope_catalog(letters, expected_symbol=symbol, cutoff=cutoff)
