from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import pytest

from config import settings
from core.timeutils import TEHRAN_TZ
from ops import prospective_sessions as ledger
from ops.report_files import read_private_report, write_private_report


@pytest.fixture
def trial(monkeypatch, tmp_path):
    data = tmp_path/'data'
    monkeypatch.setattr(settings, 'DATA_DIR', data)
    monkeypatch.setattr(settings, 'DB_PATH', data/'brs.db')
    moment = [datetime(2026,11,7,20,tzinfo=TEHRAN_TZ)]
    monkeypatch.setattr(ledger, 'now_tehran', lambda: moment[0])
    root = data/'private/registered'
    plan = dict(trading_weekdays=[5,6,0,1,2],calendar_overrides={'2026-11-09':False},
                capture_after='13:00:00',capture_before='23:30:00',genome=[0,-3,2,2,0,0,5,0])
    return root, plan, moment


def test_same_day_registration_never_counts_as_forward_data(trial):
    root, plan, _ = trial
    spec = ledger.register(root, plan)
    with pytest.raises(ValueError,match='later scheduled'):
        ledger.begin(root)
    with pytest.raises(FileExistsError):
        ledger.register(root,plan | {'genome':[]})
    assert ledger.load(root) == (spec, [])


def test_receipts_preserve_order_and_detect_payload_tampering(trial):
    root, plan, moment = trial
    reg = ledger.register(root,plan)
    moment[0] += timedelta(days=1)
    _, _, start = ledger.begin(root)
    day = ledger.append(root,start,{'content':'observed'},expected_previous=reg['registration_sha256'])
    assert ledger.load(root)[1] == [day]
    with pytest.raises(ValueError,match='already been'):
        ledger.begin(root)
    path = root/'sessions/2026-11-08.json'
    record = read_private_report(path)
    record['payload']['content'] = 'different'
    write_private_report(path,record)
    with pytest.raises(ValueError,match='chain'):
        ledger.load(root)


def test_retries_outside_frozen_window_or_with_stale_parent_fail(trial):
    root, plan, moment = trial
    spec = ledger.register(root,plan)
    moment[0] += timedelta(days=1)
    _, _, started = ledger.begin(root)
    with pytest.raises(ValueError,match='changed'):
        ledger.append(root,started,{},expected_previous='wrong')
    moment[0] = moment[0].replace(hour=23,minute=31)
    with pytest.raises(ValueError,match='window'):
        ledger.append(root,started,{},expected_previous=spec['registration_sha256'])
    assert not ledger.load(root)[1]


def test_calendar_distinguishes_missed_days_from_explicit_holidays(trial):
    root, plan, _ = trial
    reg = ledger.register(root,plan)
    assert ledger.missing_days(reg,[],through=date(2026,11,12)) == ['2026-11-08','2026-11-10','2026-11-11']


def test_concurrent_session_cannot_overwrite_first_writer(trial):
    root, plan, moment = trial
    spec = ledger.register(root,plan)
    moment[0] += timedelta(days=1)

    def record(i):
        try:
            return ledger.append(root,moment[0],{'sequence':i},expected_previous=spec['registration_sha256'])
        except (FileExistsError, ValueError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(record, (1,2)))
    successful = [x for x in results if x is not None]
    assert len(successful) == 1
    assert ledger.load(root)[1] == successful
