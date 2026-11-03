"""A source change during collection must not publish a mixed-code radar cycle."""

import pytest

from config import settings
from database.db import DatabaseManager
from database.opportunity_radar import RadarRepository
from ops import opportunity_radar as radar
from ops.report_files import read_private_report
from tests.test_news_service_unavailable import ServiceNews
from tests.test_opportunity_radar import PublicNews, PublicProvider, identity, protocol, report
from tests.test_opportunity_radar import radar_root as radar_root


def test_precommit_guard_rolls_back_cycle_episodes_and_events(db):
    repo = RadarRepository(db)
    repo.activate(protocol())

    def fail_guard():
        raise ValueError('source changed')

    with pytest.raises(ValueError, match='source changed'):
        repo.append('test-radar', report(), before_commit=fail_guard)
    for table in ('radar_cycles', 'radar_episodes', 'radar_notice_versions', 'radar_attention_events'):
        assert db.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0
    assert repo.latest('test-radar') is None


def test_code_change_during_news_collection_cannot_commit_cycle(radar_root, monkeypatch):
    registered = radar.activate(radar_root, seed=[identity()])

    class ChangingNews(PublicNews):
        def get_news_window(self, **kwargs):
            result = super().get_news_window(**kwargs)
            monkeypatch.setattr(radar, 'code_identity', lambda: {'changed_during_collection': 'different'})
            return result

    with pytest.raises(ValueError, match='Source changed during radar collection'):
        radar.collect_cycle(radar_root, registered, provider=PublicProvider(), codal=ChangingNews())
    with DatabaseManager(radar_root/'radar.db', read_only=True) as db:
        assert RadarRepository(db).latest(registered['id']) is None
        for table in ('radar_cycles', 'radar_episodes', 'radar_notice_versions', 'radar_attention_events'):
            assert db.conn.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0
    assert not settings.DB_PATH.exists()


def test_code_change_cannot_erase_an_observed_service_cooldown(radar_root, monkeypatch):
    registered = radar.activate(radar_root, seed=[identity()])
    identity_before = radar.code_identity

    class ChangingServiceNews(ServiceNews):
        def get_news_window(self, **kwargs):
            result = super().get_news_window(**kwargs)
            monkeypatch.setattr(radar, 'code_identity', lambda: {'changed_during_collection': 'different'})
            return result

    with pytest.raises(ValueError, match='Source changed during radar collection'):
        radar.collect_cycle(radar_root, registered, provider=PublicProvider(), codal=ChangingServiceNews())
    state = read_private_report(radar_root/'news-backoff.json')['state']
    assert state['observation']['http_status'] == 503 and state['delay_seconds'] == 7200
    monkeypatch.setattr(radar, 'code_identity', identity_before)
    source = ServiceNews()
    result, _ = radar.collect_cycle(radar_root, registered, provider=PublicProvider(), codal=source)
    assert source.calls == 0 and result['news_coverage']['rate_limit_deferred']
    assert result['news_coverage']['rate_limit_state'] == state
