"""Public rate-limit receipts impose honest, reproducible request deadlines."""

from copy import deepcopy
from datetime import timedelta

import pytest

from core.news_backoff import DEFAULT_SECONDS, MAX_SECONDS, checked_backoff, deferred, make_backoff
from core.opportunity_radar import digest, stamp

AT = '2026-10-25T14:27:02+03:30'


def receipt(**overrides):
    return dict(requested_page=41,url='https://search.codal.ir/api/search/v2/q?&PageNumber=41',
                requested_at='2026-10-25T14:27:01+03:30',received_at=AT,http_status=429,
                rate_limit_retry_after_seconds=None,rate_limit_retry_after_unsupported=False) | overrides


@pytest.mark.parametrize('value',[None,{},dict(http_status=200),dict(http_status=True),dict(http_status=429.0)])
def test_non_rate_limit_pages_do_not_create_state(value):
    assert make_backoff(value) is None


def test_fallback_is_explicit_and_bound_to_observed_receipt_not_a_later_restart():
    state=make_backoff(receipt())
    assert state['delay_seconds']==DEFAULT_SECONDS and state['delay_source']=='fallback'
    assert state['observed_at']==AT and state['not_before']=='2026-10-25T15:27:02+03:30'
    assert not state['manual_review_required'] and checked_backoff(state)==state
    assert state['receipt_sha256']==digest(state['observation'])
    assert deferred(state,at='2026-10-25T15:27:01+03:30')
    assert not deferred(state,at='2026-10-25T15:27:02+03:30')
    assert not deferred(state,at='2026-10-26T15:27:02+03:30')


@pytest.mark.parametrize('seconds',[0,1,120,172800,MAX_SECONDS])
def test_server_delay_is_honored_exactly_including_zero_and_long_waits(seconds):
    state=make_backoff(receipt(rate_limit_retry_after_seconds=seconds))
    assert state['delay_seconds']==seconds and state['delay_source']=='server'
    assert stamp(state['not_before'])-stamp(AT)==timedelta(seconds=seconds)
    assert not deferred(state,at=state['not_before'])
    if seconds:
        assert deferred(state,at=(stamp(state['not_before'])-timedelta(microseconds=1)).isoformat())


def test_unsupported_delay_requires_review_without_an_arbitrary_automatic_retry():
    state=make_backoff(receipt(rate_limit_retry_after_unsupported=True))
    assert state['delay_source']=='unsupported' and state['delay_seconds'] is None
    assert state['not_before'] is None and state['manual_review_required'] is True
    assert deferred(state,at=AT) and deferred(state,at='2099-10-25T14:27:02+03:30')


def test_only_whitelisted_public_receipt_fields_are_retained_and_copied():
    page=receipt(headers={'Set-Cookie':'secret'},body_base64='not-retained',letters=[dict(token='secret')])
    state=make_backoff(page)
    assert state['observation']==receipt()
    page['requested_page']=1
    assert state['observation']['requested_page']==41
    assert not {'headers','body_base64','letters'} & state['observation'].keys()


@pytest.mark.parametrize('changes',[
    dict(requested_page=True),dict(requested_page=0),dict(requested_page=101),
    dict(url='https://example.org/'),dict(requested_at='invalid'),dict(received_at='2026-10-25T14:27:02'),
    dict(requested_at='2026-10-26T14:27:01+03:30'),dict(rate_limit_retry_after_seconds=True),
    dict(rate_limit_retry_after_seconds=1.0),dict(rate_limit_retry_after_seconds=-1),
    dict(rate_limit_retry_after_seconds=MAX_SECONDS+1),dict(rate_limit_retry_after_unsupported=1),
    dict(rate_limit_retry_after_unsupported=True,rate_limit_retry_after_seconds=120),
],ids=['page-bool','page-zero','page-large','url','bad-clock','naive','reverse-clock','delay-bool',
        'delay-float','delay-negative','delay-large','unsupported-int','conflicting-delay'])
def test_invalid_rate_limit_receipt_cannot_become_a_usable_state(changes):
    with pytest.raises(ValueError):
        make_backoff(receipt(**changes))


@pytest.mark.parametrize('value',[0,-1,True,1.0,MAX_SECONDS+1])
def test_invalid_fallback_delay_is_rejected(value):
    with pytest.raises(ValueError):
        make_backoff(receipt(),default_seconds=value)


@pytest.mark.parametrize('field,value',[
    ('version','other'),('source','other'),('observed_at','2026-10-25T14:27:03+03:30'),
    ('not_before',AT),('manual_review_required',True),('manual_review_required',0),
    ('delay_seconds',1),('delay_seconds',True),('delay_seconds',3600.0),('delay_source','server'),
    ('default_seconds',7200),('receipt_sha256','0'*64),
])
def test_derived_state_mutation_fails_closed(field,value):
    state=make_backoff(receipt())
    state[field]=value
    with pytest.raises(ValueError):
        checked_backoff(state)
    with pytest.raises(ValueError):
        deferred(state,at=AT)


def test_changed_receipt_or_extra_state_fields_fail_verification():
    original=make_backoff(receipt())
    for state in [original|dict(extra='unknown'),original|dict(observation=receipt(rate_limit_retry_after_seconds=120))]:
        with pytest.raises(ValueError):
            checked_backoff(state)
    copied=deepcopy(original)
    copied['observation']['headers']={'secret':'not-valid'}
    with pytest.raises(ValueError):
        checked_backoff(copied)
    assert checked_backoff(original)==original


def test_none_state_and_aware_utc_clock_are_supported_without_time_inference():
    assert checked_backoff(None) is None and not deferred(None,at=AT)
    state=make_backoff(receipt())
    assert deferred(state,at='2026-10-25T10:57:02+00:00')
    for value in ('invalid','2026-10-25T14:27:02'):
        with pytest.raises(ValueError):
            deferred(None,at=value)


def test_clock_rollback_and_unrepresentable_deadline_fail_closed():
    for state in (make_backoff(receipt()),make_backoff(receipt(rate_limit_retry_after_unsupported=True))):
        with pytest.raises(ValueError,match='rollback'):
            deferred(state,at='2026-10-25T14:27:01+03:30')
    with pytest.raises(ValueError,match='calendar'):
        make_backoff(receipt(requested_at='9999-12-31T23:59:00+03:30',
                             received_at='9999-12-31T23:59:01+03:30'))
