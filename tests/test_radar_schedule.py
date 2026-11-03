"""Market boundaries must not remain hidden behind an overnight hourly sleep."""

from datetime import datetime, timedelta

import pytest

from ops import opportunity_radar as radar


def plan(**calendar):
    return dict(calendar_exceptions=calendar, price_scan_after='13:15',
                news_interval_open_seconds=600, news_interval_closed_seconds=3600)


@pytest.mark.parametrize('when,expected', [
    ('2026-10-24T08:03:00+03:30', 1620),
    ('2026-10-24T08:29:59.750000+03:30', .25),
    ('2026-10-24T08:30:00+03:30', 600),
    ('2026-10-24T05:00:00+00:00', 600),
    ('2026-10-24T13:14:59+03:30', 1),
    ('2026-10-24T13:15:00+03:30', 600),
    ('2026-10-24T15:01:00+03:30', 3600),
    ('2026-10-23T08:29:00+03:30', 3600),
])
def test_boundaries_preserve_normal_polling_and_tehran_timezone(when, expected):
    assert radar.next_poll_delay(datetime.fromisoformat(when), plan()) == expected


def test_explicit_holiday_and_special_session_override_weekday():
    saturday = datetime.fromisoformat('2026-10-24T08:29:00+03:30')
    friday = saturday - timedelta(days=1)
    assert radar.next_poll_delay(saturday, plan(**{'2026-10-24': False})) == 3600
    assert radar.next_poll_delay(friday, plan(**{'2026-10-23': True})) == 60
    with pytest.raises(ValueError):
        radar.next_poll_delay(saturday, plan(**{'2026-10-24': None}))
    with pytest.raises(ValueError):
        radar.next_poll_delay(saturday.replace(tzinfo=None), plan())


@pytest.mark.parametrize('clock_jump', [0, 3600, -3600])
def test_wait_updates_heartbeat_and_no_sleep_or_request_catchup(tmp_path, monkeypatch, clock_jump):
    clock = datetime.fromisoformat('2026-10-24T08:29:45+03:30')
    elapsed, sleeps, writes = [0.0], [], []
    monkeypatch.setattr(radar.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(radar, 'now_tehran', lambda: clock + timedelta(
        seconds=elapsed[0] + (clock_jump if sleeps else 0)))
    monkeypatch.setattr(radar, 'now_tehran_iso', lambda: radar.now_tehran().isoformat())
    monkeypatch.setattr(radar, 'read_private_report', lambda _: {'status': 'degraded'})
    monkeypatch.setattr(radar, 'write_private_report', lambda p, value: writes.append(value))

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr(radar.time, 'sleep', sleep)
    radar._wait_for_poll(tmp_path, plan())
    assert sleeps == [15]
    assert writes[-1]['seconds_until_poll'] == 0
    assert writes[-1]['next_poll_at'] == '2026-10-24T08:30:00+03:30'
    assert writes[-1]['status'] == 'degraded'  # A healthy timer does not heal a source failure.


def test_wait_uses_completion_clock_and_bounded_heartbeats(tmp_path, monkeypatch):
    clock = datetime.fromisoformat('2026-10-24T08:31:00+03:30')
    elapsed, sleeps = [0.0], []
    monkeypatch.setattr(radar.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(radar, 'now_tehran', lambda: clock + timedelta(seconds=elapsed[0]))
    monkeypatch.setattr(radar, 'now_tehran_iso', lambda: radar.now_tehran().isoformat())
    monkeypatch.setattr(radar, 'read_private_report', lambda _: {'status': 'observed'})
    monkeypatch.setattr(radar, 'write_private_report', lambda *_: None)

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr(radar.time, 'sleep', sleep)
    radar._wait_for_poll(tmp_path, plan())
    assert sum(sleeps) == 600 and max(sleeps) <= 30
