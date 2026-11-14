"""Local, append-only registration and same-day paper evidence."""
from __future__ import annotations

from datetime import date, time, timedelta
from pathlib import Path

from config import settings
from core.forward_observations import clock, digest
from core.timeutils import is_canonical_iso_date, now_tehran
from ops.report_files import private_report_path, read_private_report, write_private_report

VERSION = 'prospective-session-ledger-v1'


def _paths(directory):
    path = private_report_path(Path(directory)/'strategy-spec.json', forbidden_paths=(settings.DB_PATH,))
    return path, path.parent/'sessions'


def _calendar(plan):
    weekdays = plan['trading_weekdays']
    dates = plan['calendar_overrides']
    after, before = (time.fromisoformat(plan[k]) for k in ('capture_after','capture_before'))
    if (not isinstance(weekdays, list) or not weekdays or len(weekdays) != len(set(weekdays))
            or any(type(n) is not int or not 0 <= n < 7 for n in weekdays)
            or not isinstance(dates, dict) or any(not is_canonical_iso_date(d) or type(v) is not bool
                                                for d,v in dates.items())
            or after.tzinfo is not None or before.tzinfo is not None or after >= before):
        raise ValueError('Invalid frozen market calendar')
    return weekdays, dates, after, before


def register(directory, plan):
    """Seal the plan at the local system time. A prior release cannot be overwritten."""
    _calendar(plan)
    path, _ = _paths(directory)
    body = dict(version=VERSION, registered_at=now_tehran().isoformat(), plan=plan, externally_timestamped=False)
    body['registration_sha256'] = digest(body)
    write_private_report(path,body,forbidden_paths=(settings.DB_PATH,),exclusive=True)
    return body


def _scheduled(day, plan):
    weekdays, dates, _, _ = _calendar(plan)
    return dates.get(day.isoformat(), day.weekday() in weekdays)


def _window(at, registration):
    plan = registration['plan']
    _, _, after, before = _calendar(plan)
    return (at.date() > clock(registration['registered_at']).date() and _scheduled(at.date(),plan)
            and after <= at.time() < before)


def load(directory):
    """Validate the registration and all retained event hashes and timestamps."""
    path, sessions = _paths(directory)
    registered = read_private_report(path)
    expected = digest({k:v for k,v in registered.items() if k != 'registration_sha256'})
    if registered.get('version') != VERSION or registered.get('registration_sha256') != expected:
        raise ValueError('Invalid registration hash')
    at = clock(registered['registered_at'])
    now = now_tehran()
    if at > now:
        raise ValueError('Registration time is in the future')
    _calendar(registered['plan'])
    previous = expected
    events = []
    for file in sorted(sessions.glob('*.json')):
        record = read_private_report(file)
        if set(record) != {'date','capture_started_at','recorded_at','previous_sha256','payload','sha256'}:
            raise ValueError('Invalid session record shape')
        start, end = clock(record['capture_started_at']), clock(record['recorded_at'])
        if (record['date'] != file.stem or end.date().isoformat() != file.stem or start.date() != end.date()
                or not at < start <= end <= now or not _window(start, registered) or not _window(end, registered)
                or record['previous_sha256'] != previous
                or record['sha256'] != digest({k:v for k,v in record.items() if k != 'sha256'})):
            raise ValueError('Session receipt, chain, date or capture window is inconsistent')
        events.append(record)
        previous = record['sha256']
    return registered, events


def begin(directory):
    registered, events = load(directory)
    started = now_tehran()
    if not _window(started, registered):
        raise ValueError('Capture must occur after registration on a later scheduled session, inside the frozen window')
    if events and events[-1]['date'] >= started.date().isoformat():
        raise ValueError('The session has already been registered')
    return registered, events, started


def append(directory, started, payload, *, expected_previous):
    """Publish one record. A parallel worker or a late capture cannot replace it."""
    registered, events, now = begin(directory)
    if started.date() != now.date() or started > now or not _window(started, registered):
        raise ValueError('Capture crossed its registered time boundary')
    previous = events[-1]['sha256'] if events else registered['registration_sha256']
    if previous != expected_previous:
        raise ValueError('The journal changed while this observation was collected')
    day = now.date().isoformat()
    record = dict(date=day,capture_started_at=started.isoformat(),recorded_at=now.isoformat(),
                  previous_sha256=previous,payload=payload)
    record['sha256'] = digest(record)
    _, sessions = _paths(directory)
    write_private_report(sessions/f'{day}.json',record,forbidden_paths=(settings.DB_PATH,),exclusive=True)
    return record


def missing_days(registration, events, *, through: date):
    """Configured session gaps are unresolved, even when a weekday may have been a holiday."""
    observed = {record['date'] for record in events}
    day = clock(registration['registered_at']).date() + timedelta(days=1)
    missing = []
    while day <= through:
        if day.isoformat() not in observed and _scheduled(day, registration['plan']):
            missing.append(day.isoformat())
        day += timedelta(days=1)
    return missing
