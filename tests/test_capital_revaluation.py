"""Synthetic capital text stays in tests; registration never proves a holder credit."""
import json
from pathlib import Path

import pytest

from core.capital_actions import PREVIOUS_VERSION, VERSION, primary_capital_registration
from core.codal_notice_summary import summarize_codal_notice
from ops.capital_actions import create_capital_study, review_capital_study
from tests.test_capital_actions import CUTOFF, TEXT, notice, rehash, source  # noqa: F401


def revaluation_text(value='350'):
    return TEXT.replace('از محل سود انباشته مبلغ\n350', 'از محل مازاد تجدید ارزیابی دارایی ها مبلغ\n'+value)


def read(text, version=VERSION):
    return primary_capital_registration(text, notice(), end_exclusive=CUTOFF, version=version)


def test_revaluation_is_an_explicit_exact_funding_fact_without_holder_entitlement():
    result = read(revaluation_text())
    assert result['status'] == 'supported_primary_fact'
    assert result['revaluation_surplus_rial'] == 350_000_000
    assert result['revaluation_equals_increase'] is True
    assert result['retained_earnings_rial'] is None
    assert result['unassigned_after_known_funding_rial'] == 0
    assert all(result[k] is False for k in ('shareholder_entitlement_verified', 'shares_credited_verified',
        'tradability_verified', 'funding_completeness_verified', 'feature_eligible', 'accepted_for_signals'))
    assert 'credit_date' not in result and 'first_tradable_date' not in result


@pytest.mark.parametrize('amount,expected', [('', None), ('0', 0), ('100', 100_000_000)])
def test_blank_zero_and_partial_revaluation_are_distinct(amount, expected):
    result = read(revaluation_text(amount))
    assert result['status'] == 'supported_primary_fact'
    assert result['revaluation_surplus_rial'] == expected
    assert result['revaluation_equals_increase'] is (None if expected is None else False)
    assert result['unassigned_after_known_funding_rial'] == (None if expected is None else 350_000_000-expected)


@pytest.mark.parametrize('text', [revaluation_text('351'), revaluation_text('1,00'),
    revaluation_text()+'\nاز محل مازاد تجدید ارزیابی دارایی ها مبلغ 350 میلیون ریال',
    revaluation_text()+'\nاز محل سود انباشته مبلغ 1 میلیون ریال'])
def test_inconsistent_revaluation_does_not_publish_financial_facts(text):
    result = read(text)
    assert result['status'] == 'unsupported_primary_text'
    assert result['capital_after_rial'] is None and result['revaluation_surplus_rial'] is None


def test_previous_parser_shape_and_unknown_funding_are_preserved():
    result = read(revaluation_text(), PREVIOUS_VERSION)
    assert result['status'] == 'supported_primary_fact'
    assert result['unassigned_after_known_funding_rial'] is None
    assert 'revaluation_surplus_rial' not in result and 'revaluation_equals_increase' not in result


def test_notice_summary_keeps_current_facts_version_and_does_not_promote_review():
    body = revaluation_text().encode('utf-8')
    metadata = notice()
    manifest = dict(format='text', source_url=metadata['url'])
    summary = summarize_codal_notice(manifest, body, notice=metadata, end_exclusive=CUTOFF)
    assert summary['capital_registration']['version'] == VERSION
    assert summary['capital_registration']['revaluation_surplus_rial'] == 350_000_000
    assert summary['numeric_facts_reviewed'] is False
    assert summary['accepted_for_signals'] is False


def test_revaluation_amount_is_not_read_after_historical_cutoff(monkeypatch):
    monkeypatch.setattr('core.capital_actions._rial', lambda *_: pytest.fail('sealed amount read'))
    result = read(revaluation_text().replace('1402/12/23', '1404/01/01'))
    assert result['status'] == 'withheld_by_cutoff'
    assert result['revaluation_surplus_rial'] is None


def test_previous_version_study_replays_with_recorded_parser(source):  # noqa: F811
    folder = Path(create_capital_study(**source)['folder'])
    plan = json.loads((folder/'plan.json').read_bytes())
    result = json.loads((folder/'result.json').read_bytes())
    plan['facts_version'] = PREVIOUS_VERSION
    result['facts'] = read(TEXT, PREVIOUS_VERSION)
    for name, payload in (('plan', plan), ('result', result)):
        (folder/f'{name}.json').write_text(json.dumps(payload), encoding='utf-8')
        rehash(folder, name)
    assert review_capital_study(folder)['facts'] == result['facts']
