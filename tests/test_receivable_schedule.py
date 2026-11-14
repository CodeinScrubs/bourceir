"""Synthetic payment notices: deadlines and holder classes are not cash receipts."""

from copy import deepcopy

import pytest

from research.receivable_schedule import classify_payment_plan


def fact(**changes):
    record = dict(symbol='نماد', fiscal_year_end='2023-12-31', tracing_no=123,
        source_url='https://codal.ir/Reports/DownloadFile.aspx?id=test', source_sha256='a' * 64,
        source_published_at='2024-08-01T12:00:00+03:30', holder_groups=['individual_sejam'],
        date_kind='deadline', start_date=None, end_date='2024-12-20')
    record.update(changes)
    return record


def classify(record):
    return classify_payment_plan(record, symbol='نماد', fiscal_year_end='2023-12-31',
        terminal='2024-11-10', holder_group='individual_sejam')


def test_future_deadline_cannot_prove_payment_had_not_already_happened():
    result = classify(fact())
    assert result['timing_relation'] == 'deadline_after_terminal_payment_timing_unknown'
    assert result['actual_payment_date'] is None and result['spendable_cash_rial'] is None
    assert result['actual_payment_verified'] is False


def test_plan_before_terminal_is_still_not_a_payment_receipt():
    result = classify(fact(date_kind='fixed', start_date='2024-10-01', end_date='2024-10-01'))
    assert result['timing_relation'] == 'planned_before_or_at_terminal'
    assert result['spendable_cash_rial'] is None


def test_future_fixed_date_and_crossing_window_are_different_from_deadline():
    fixed = classify(fact(date_kind='fixed', start_date='2024-12-20'))
    crossing = classify(fact(date_kind='window', start_date='2024-10-20'))
    assert fixed['timing_relation'] == 'planned_after_terminal'
    assert crossing['timing_relation'] == 'planned_window_straddles_terminal'


def test_latest_nonsejam_or_legal_notice_does_not_apply_to_sejam_individual():
    for groups in (['individual_nonsejam'], ['legal_sejam', 'legal_nonsejam']):
        result = classify(fact(holder_groups=groups))
        assert result['timing_relation'] == 'different_holder_group'
        assert result['applicable_to_holder'] is False


def test_later_publication_never_becomes_available_in_a_past_decision():
    result = classify(fact(source_published_at='2024-11-11T00:00:00+03:30'))
    assert result['source_published_by_terminal'] is False and result['feature_eligible'] is False


def test_undated_conditional_payment_and_input_immutability():
    record = fact(date_kind='conditional', end_date=None)
    saved = deepcopy(record)
    result = classify(record)
    assert record == saved
    assert result['timing_relation'] == 'conditional_payment_date_unknown'
    assert result['actual_payment_date'] is None


@pytest.mark.parametrize('changes', [dict(symbol='دیگر'), dict(fiscal_year_end='2022-12-31'),
    dict(tracing_no=True), dict(source_sha256='not-a-hash'),
    dict(source_url='https://evil.test/DownloadFile.aspx'),
    dict(source_published_at='2024-08-01T12:00:00'), dict(holder_groups=[]), dict(holder_groups=[{}]),
    dict(holder_groups=['individual_sejam', 'individual_sejam']), dict(holder_groups=['unknown']),
    dict(date_kind='fixed', start_date='2024-10-01'),
    dict(date_kind='window', start_date='2025-01-01'),
    dict(date_kind='deadline', start_date='2024-01-01'),
    dict(date_kind='conditional'), dict(date_kind='receipt'), dict(end_date='2024-13-01')])
def test_ambiguous_or_mismatched_facts_are_rejected(changes):
    with pytest.raises(ValueError):
        classify(fact(**changes))


def test_unknown_holder_request_is_rejected_without_a_cash_fallback():
    with pytest.raises(ValueError):
        classify_payment_plan(fact(), symbol='نماد', fiscal_year_end='2023-12-31',
            terminal='2024-11-10', holder_group=[])
