"""Correction/delegation formats are opt-in; old parser contracts remain unchanged."""
import pytest

from core.capital_actions import CORRECTION_VERSION, VERSION, primary_capital_registration
from tests.test_capital_actions import CUTOFF, TEXT, notice


def parse(text=TEXT, metadata=None, *, version=CORRECTION_VERSION):
    return primary_capital_registration(text, metadata or notice(), end_exclusive=CUTOFF, version=version)


@pytest.mark.parametrize('title', ['آگهی ثبت افزایش سرمایه(اصلاحیه)', 'آگهی ثبت افزایش سرمایه (اصلاحیه)'])
def test_correction_requires_prior_link_and_preserves_financial_guards(title):
    text=TEXT.replace('موضوع:', 'این اطلاعیه اصلاحیه اطلاعیه شماره ( 122 ) می باشد.\nموضوع:', 1)
    result=parse(text,notice(title=title))
    assert result['status']=='supported_primary_fact'
    assert result['is_correction'] is True and result['prior_tracing_no']==122
    assert result['capital_after_rial']==1_350_000_000
    assert not result['shareholder_entitlement_verified'] and not result['shares_credited_verified']
    assert parse(text,notice(title=title),version=VERSION)['status']=='unsupported_primary_text'


@pytest.mark.parametrize('reference', ['', 'این اطلاعیه اصلاحیه اطلاعیه شماره ( 123 ) می باشد.',
    'این اطلاعیه اصلاحیه اطلاعیه شماره ( 0 ) می باشد.',
    'این اطلاعیه اصلاحیه اطلاعیه شماره ( 122 ) می باشد.\nاین اطلاعیه اصلاحیه اطلاعیه شماره ( 121 ) می باشد.'])
def test_correction_does_not_accept_missing_self_zero_or_ambiguous_prior(reference):
    result=parse(TEXT+'\n'+reference,notice(title='آگهی ثبت افزایش سرمایه(اصلاحیه)'))
    assert result['status']=='unsupported_primary_text' and result['capital_after_rial'] is None


def delegated():
    return TEXT.replace('با عنایت به مجوز افزایش سرمایه به شماره مجوز DPM-IOP-02A-123 مورخ 1402/11/17\n','').replace(
        'افزایش سرمایۀ شرکت', 'و صورتجلسه هیئت مدیره مورخ 1402/12/05\nافزایش سرمایۀ شرکت').replace(
        'از محل سود انباشته مبلغ', 'از محل مطالبات و آورده نقدی مبلغ')


def test_explicit_board_date_can_exist_without_a_license_date_and_cash_does_not_become_paid():
    result=parse(delegated())
    assert result['status']=='supported_primary_fact'
    assert result['license_date'] is None and result['license_status']=='not_in_primary_text'
    assert result['board_resolution_date']=='2024-02-24'
    assert result['preemptive_cash_and_claims_rial']==350_000_000
    assert result['preemptive_cash_equals_increase'] is True
    assert not result['shareholder_entitlement_verified'] and not result['tradability_verified']
    assert parse(delegated(),version=VERSION)['status']=='unsupported_primary_text'


def test_delegation_licenses_can_postdate_the_authorizing_assembly_but_not_the_board_resolution():
    text=delegated().replace('بر اساس تصمیمات','با عنایت به مجوز افزایش سرمایه به شماره مجوز DPM-IOP-02A-123 مورخ 1402/12/04\nبر اساس تصمیمات')
    assert parse(text)['status']=='supported_primary_fact'
    bad=text.replace('1402/12/04','1402/12/06')
    assert parse(bad)['status']=='unsupported_primary_text'


@pytest.mark.parametrize('text', [delegated().replace('1402/12/05','1402/12/01'),
    delegated().replace('نقدی مبلغ\n350','نقدی مبلغ\n351'),
    delegated()+'\nاز محل مطالبات و آورده نقدی مبلغ 350 میلیون ریال'])
def test_delegated_dates_and_cash_amounts_remain_strict(text):
    result=parse(text)
    assert result['status']=='unsupported_primary_text' and result['capital_after_rial'] is None


def test_future_board_withholds_before_monetary_parsing():
    text=delegated().replace('1402/12/05','1404/01/01').replace('1,000','not-a-number')
    result=parse(text)
    assert result['status']=='withheld_by_cutoff' and result['capital_after_rial'] is None


def test_unknown_and_zero_cash_are_not_interchanged_and_old_output_shape_stays_fixed():
    legacy=parse(TEXT,version=VERSION)
    assert 'preemptive_cash_and_claims_rial' not in legacy and 'board_resolution_date' not in legacy
    assert parse(TEXT)['preemptive_cash_and_claims_rial'] is None
    zero=parse(delegated().replace('نقدی مبلغ\n350','نقدی مبلغ\n0'))
    assert zero['preemptive_cash_and_claims_rial']==0 and zero['preemptive_cash_equals_increase'] is False


def test_original_metadata_cannot_hide_a_declared_correction():
    result=parse(TEXT+'\nاین اطلاعیه اصلاحیه اطلاعیه شماره ( 122 ) می باشد.')
    assert result['status']=='unsupported_primary_text' and result['capital_after_rial'] is None


def test_mixed_known_funding_cannot_exceed_the_increase():
    text=delegated().replace('میلیون ریال ،','میلیون ریال ،\nاز محل سود انباشته مبلغ 1 میلیون ریال ،',1)
    result=parse(text)
    assert result['status']=='unsupported_primary_text' and result['capital_after_rial'] is None
