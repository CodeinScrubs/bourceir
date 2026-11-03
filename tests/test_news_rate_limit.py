"""HTTP429 is a collection pause, not empty news or a resettable clock."""

import json

import pytest

from core.news_backoff import VERSION as BACKOFF_VERSION
from core.news_backoff import make_backoff
from core.news_continuity import NewsWindow
from ops import opportunity_radar as radar
from ops.report_files import read_private_report, write_private_report
from tests.test_news_continuity import SINCE, START, page
from tests.test_opportunity_radar import PublicProvider, identity
from tests.test_opportunity_radar import radar_root as radar_root


def limited(number=1,seconds=None,*,unsupported=False):
    return dict(requested_page=number,url=f'https://search.codal.ir/api/search/v2/q?&PageNumber={number}',
        requested_at=START,received_at=START,http_status=429,status='unavailable',
        rate_limit_retry_after_seconds=seconds,rate_limit_retry_after_unsupported=unsupported,
        failure_stage='http',failure_code='http_rate_limited',letters=[],raw_count=None,invalid_rows=0)


@pytest.mark.parametrize('seconds',[None,0,7200,172800])
def test_failed_page_retains_rate_limit_and_unknown_news(seconds):
    scan=NewsWindow(since=SINCE,requested_at=START)
    scan.add(limited(seconds=seconds))
    result=scan.result(stop_reason='source_error')
    assert result['rate_limit_state']==make_backoff(limited(seconds=seconds))
    assert result['failed_page']==1 and not result['window_scan_satisfied']
    assert result['checkpoint_through'] is None and not result['letters']


def test_rate_limit_during_head_recheck_is_retained_without_checkpoint():
    scan=NewsWindow(since=SINCE,requested_at=START)
    scan.add(page(1,'09:00'))
    result=scan.result(head_recheck=limited(seconds=7200),stop_reason='boundary')
    assert result['stop_reason']=='head_recheck_unavailable'
    assert result['rate_limit_state']['delay_seconds']==7200
    assert result['checkpoint_through'] is None


def test_deferred_window_replays_and_cannot_contain_requests():
    state=make_backoff(limited(seconds=7200))
    scan=NewsWindow(since=SINCE,requested_at=START,initial_rate_limit_state=state)
    with pytest.raises(ValueError,match='deferred'):
        scan.add(page(1,'11:00'))
    with pytest.raises(ValueError,match='Deferred'):
        scan.result(stop_reason='boundary')
    result=scan.result(stop_reason='rate_limit_cooldown')
    assert result['rate_limit_deferred'] and result['pages_fetched']==0
    assert result['rate_limit_state']==state and result['checkpoint_through'] is None
    assert result['errors']==['rate_limit_cooldown']


class RateNews:
    def __init__(self):
        self.calls=0
    def get_news_window(self,*,since,pages,time_budget_seconds,progress):
        self.calls+=1
        scan=NewsWindow(since=since,requested_at=radar.now_tehran_iso())
        at=radar.now_tehran_iso()
        failed=dict(limited(seconds=7200),requested_at=at,received_at=at)
        scan.add(failed)
        return scan.result(stop_reason='source_error')


def test_collector_persists_cooldown_across_upgrade_and_replay_without_more_requests(radar_root):
    initial=radar.activate(radar_root,seed=[identity()])
    news=RateNews()
    first,_=radar.collect_cycle(radar_root,initial,provider=PublicProvider(),codal=news)
    state=first['news_coverage']['rate_limit_state']
    assert news.calls==1 and state['delay_source']=='server'
    assert read_private_report(radar_root/'news-backoff.json')==dict(version=BACKOFF_VERSION,state=state)
    upgraded=radar.activate(radar_root,new_run=True)
    second,_=radar.collect_cycle(radar_root,upgraded,provider=PublicProvider(),codal=news)
    assert news.calls==1 and second['news_coverage']['rate_limit_deferred']
    assert second['news_coverage']['rate_limit_state']==state
    assert second['news_coverage']['window_start']==first['news_coverage']['window_start']
    assert radar.verify(radar_root)['replayed_news_windows']==1
    archive=radar_root/second['artifacts_folder']
    assert read_private_report(archive/'news-source.json')['pages']==[]


def test_expired_cooldown_allows_one_new_attempt_and_new_rate_limit_is_not_ignored(radar_root,monkeypatch):
    monkeypatch.setattr(radar,'now_tehran_iso',lambda:START)
    frozen=radar.activate(radar_root,seed=[identity()])
    state=make_backoff(limited(seconds=0))
    write_private_report(radar_root/'news-backoff.json',dict(version=BACKOFF_VERSION,state=state))
    news=RateNews()
    result,_=radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=news)
    assert news.calls==1 and not result['news_coverage']['rate_limit_deferred']
    assert result['news_coverage']['rate_limit_state']['delay_seconds']==7200


def test_corrupted_cooldown_fails_closed_before_request(radar_root):
    frozen=radar.activate(radar_root,seed=[identity()])
    state=make_backoff(limited(seconds=7200))
    state['not_before']=START
    write_private_report(radar_root/'news-backoff.json',dict(version=BACKOFF_VERSION,state=state))
    news=RateNews()
    with pytest.raises(ValueError,match='differs'):
        radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=news)
    assert news.calls==0 and radar.status(radar_root)['latest'] is None


def test_archived_rate_limit_cannot_be_changed_even_if_runtime_state_changes(radar_root):
    frozen=radar.activate(radar_root,seed=[identity()])
    result,_=radar.collect_cycle(radar_root,frozen,provider=PublicProvider(),codal=RateNews())
    # Operational state may subsequently expire/reset. Replay still uses its own bound receipt.
    write_private_report(radar_root/'news-backoff.json',dict(version=BACKOFF_VERSION,state=None))
    assert radar.verify(radar_root)['replayed_news_windows']==1
    source=radar_root/result['artifacts_folder']/'news-backoff-source.json'
    source.write_text(json.dumps(dict(version=BACKOFF_VERSION,state=make_backoff(limited(seconds=1)))))
    with pytest.raises(ValueError,match='artifact'):
        radar.verify(radar_root)
