"""Pagination, failures and clock boundaries must not silently drop a news interval."""

import pytest

from core.news_continuity import ORDER_DIAGNOSTIC_LIMIT, NewsWindow, next_window_start
from core.opportunity_radar import digest

START = '2026-10-23T12:00:00+03:30'
SINCE = '2026-10-23T10:00:00+03:30'


def page(number, *hours, **extra):
    return dict(requested_page=number, status='observed', received_at=START,
        letters=[dict(tracing_no=int(hour.replace(':', '')), title='Test',
            published_at=f'2026-10-23T{hour}:00+03:30') for hour in hours],
        raw_count=len(hours), invalid_rows=0, **extra)


def window():
    return NewsWindow(since=SINCE, requested_at=START)


def test_boundary_requires_older_not_equal_and_stable_head():
    scan = window(); first = page(1, '11:59', '10:00'); scan.add(first)
    assert not scan.boundary_reached
    scan.add(page(2, '10:00', '09:59'))
    result = scan.result(head_recheck=first, stop_reason='boundary')
    assert result['window_scan_satisfied'] and result['checkpoint_through'] == START
    assert len(result['letters']) == 3
    assert not result['complete_coverage_verified']


@pytest.mark.parametrize('reason', ['page_limit', 'time_budget', 'request_failed'])
def test_unfinished_window_does_not_advance_checkpoint(reason):
    scan = window(); first = page(1, '11:59'); scan.add(first)
    result = scan.result(head_recheck=first, stop_reason=reason)
    assert result['checkpoint_through'] is None and not result['window_scan_satisfied']
    assert result['page_limit_reached'] == (reason == 'page_limit')
    assert next_window_start(dict(news_coverage=result), registered_at=START) == SINCE


@pytest.mark.parametrize('bad', ['missing', 'changed', 'failed'])
def test_no_success_when_head_changes_or_cannot_be_verified(bad):
    scan = window(); scan.add(page(1, '09:00'))
    head = {'missing':None, 'changed':page(1, '11:59'), 'failed':dict(status='unavailable')}[bad]
    assert not scan.result(head_recheck=head, stop_reason='boundary')['window_scan_satisfied']


def test_partial_invalid_and_out_of_order_publications_block_checkpoint_but_retain_letters():
    scan = window(); first = page(1, '11:00', '09:00'); first['invalid_rows'] = 1
    first['status'] = 'partial'; scan.add(first)
    result = scan.result(head_recheck=first, stop_reason='boundary')
    assert len(result['letters']) == 2 and result['invalid_rows'] == 1
    assert not result['window_scan_satisfied']
    scan = window(); first = page(1, '11:00', '11:30', '09:00'); scan.add(first)
    assert 'publication_order_changed' in scan.result(head_recheck=first, stop_reason='boundary')['errors']


@pytest.mark.parametrize('time', [None, 'bad', '2026-10-23T13:00:00+03:30', '2026-10-23T09:00:00'])
def test_unknown_future_or_naive_date_never_closes_window(time):
    scan = window(); first = page(1, '09:00'); first['letters'][0]['published_at'] = time
    scan.add(first)
    assert not scan.result(head_recheck=first, stop_reason='boundary')['window_scan_satisfied']


def test_native_empty_end_requires_stable_recheck():
    scan = window(); first = page(1); scan.add(first)
    result = scan.result(head_recheck=first, stop_reason='end')
    assert result['reached_end'] and result['window_scan_satisfied']
    assert result['oldest_published_at'] is None


def test_failed_page_and_same_id_revisions_are_not_collapsed():
    scan = window(); first = page(1, '11:00'); scan.add(first)
    second = page(2, '10:59'); second['letters'][0]['tracing_no'] = first['letters'][0]['tracing_no']
    scan.add(second); scan.add(dict(requested_page=3,status='unavailable'))
    result = scan.result(stop_reason='request_failed')
    assert result['failed_page'] == 3 and len(result['letters']) == 2
    assert result['checkpoint_through'] is None


def test_success_overlap_and_initial_boundary_are_explicit():
    previous = dict(news_coverage=dict(window_scan_satisfied=True,checkpoint_through=START,window_start=SINCE))
    assert next_window_start(previous, registered_at=START) == SINCE
    assert next_window_start(None, registered_at=START) == '2026-10-22T12:00:00+03:30'
    with pytest.raises(ValueError):
        NewsWindow(since=START, requested_at=SINCE)
    with pytest.raises(ValueError):
        window().add(page(2))


def test_order_diagnostics_bind_exact_original_rows_and_cross_page_positions():
    scan=window(); first=page(1,'11:00','11:01'); scan.add(first)
    second=page(2,'11:02'); scan.add(second)
    result=scan.result(stop_reason='page_limit')
    assert result['publication_order_violation_count']==2
    a,b=result['publication_order_violations']
    assert a['previous_valid']['page']==a['current']['page']==1
    assert a['previous_valid']['row']==1 and a['current']['row']==2
    assert a['current']['letter_sha256']==digest(first['letters'][1])
    assert b['previous_valid']['page']==1 and b['current']['page']==2
    assert b['previous_valid']['row']==2 and b['current']['row']==1
    assert b['current']['letter_sha256']==digest(second['letters'][0])
    assert not result['publication_order_diagnostics_truncated']
    assert not result['window_scan_satisfied'] and result['checkpoint_through'] is None
    assert result['letters']==[*first['letters'],*second['letters']]


def test_order_diagnostic_sample_is_bounded_while_all_notices_and_failure_count_remain():
    scan=window()
    first=page(1,*[f'10:{minute:02}' for minute in range(ORDER_DIAGNOSTIC_LIMIT+5)])
    scan.add(first)
    result=scan.result(stop_reason='page_limit')
    assert result['publication_order_violation_count']==ORDER_DIAGNOSTIC_LIMIT+4
    assert len(result['publication_order_violations'])==ORDER_DIAGNOSTIC_LIMIT
    assert result['publication_order_diagnostics_truncated']
    assert len(result['letters'])==ORDER_DIAGNOSTIC_LIMIT+5
    assert result['checkpoint_through'] is None


def test_invalid_timestamp_does_not_become_a_valid_predecessor_in_order_diagnostics():
    scan=window(); first=page(1,'11:00','11:01','11:02')
    first['letters'][1]['published_at']='bad'
    scan.add(first)
    result=scan.result(stop_reason='source_error')
    violation=result['publication_order_violations'][0]
    assert violation['previous_valid']['row']==1 and violation['current']['row']==3
    assert set(result['errors'])=={'invalid_publication_time','publication_order_changed'}
    assert result['checkpoint_through'] is None
