"""A prior-day baseline must reconcile that day's original publications after midnight."""

import hashlib
import json
from datetime import datetime

import pandas as pd
import pytest

from config import settings
from data_collector.daily_history import VERSION as HISTORY_VERSION
from data_collector.daily_history import history_request
from ops import opportunity_radar as radar
from ops.radar_prices import (
    collect_session_composition,
    composition_facts,
    replay_composition,
    replay_session_selection,
)
from ops.report_files import read_private_report
from tests.test_daily_publications import DAY, observation, price, task
from tests.test_dated_history import AT, REGISTERED, history
from tests.test_opportunity_radar import PublicNews, PublicProvider, identity
from tests.test_publication_cache import SCOPE as SOURCE_SCOPE
from tests.test_publication_cache import cache_root as cache_root
from tests.test_publication_cache import make_archive

NEXT = '2024-01-04'
SCOPE = [dict(SOURCE_SCOPE[0], symbol_fa='آزمون')]
OBSERVED = NEXT+'T08:00:00+03:30'
REGISTERED_NEXT = NEXT+'T07:00:00+03:30'


def run(cache_root, *, conflicting=True, bootstrap=True, requested=NEXT):
    make_archive(cache_root)
    folder = cache_root.parent/'session-cycle'
    folder.mkdir()
    frame = history(current=True)
    if conflicting:
        frame.loc[0, 'close_final_raw'] = 97
    original = {SCOPE[0]['ins_code']: frame}
    views, proof, selection = collect_session_composition(folder, original, SCOPE,
        session_date=requested, observed_at=OBSERVED, registered_at=REGISTERED_NEXT,
        bootstrap=bootstrap, minimum_calendar_symbols=1)
    inventory = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    return folder, original, views, proof, selection, inventory


def test_new_day_baseline_preserves_the_existing_conflict_and_original_source_times(cache_root):
    folder, original, views, proof, selection, _ = run(cache_root)
    code = SCOPE[0]['ins_code']
    assert selection['requested_session'] == NEXT and selection['selected_session'] == DAY
    assert selection['target_changed'] and selection['native_requests'] == 0
    assert read_private_report(folder/'requested-dated-composition.json')['session_date'] == NEXT
    assert read_private_report(folder/'dated-composition.json')['session_date'] == DAY
    facts = composition_facts(views[code], proof[code], [DAY], session_date=DAY)
    assert facts['status'] == 'conflicting_publications'
    assert facts['date'] == DAY and facts['price_publication_provenance']['session_date'] == DAY
    assert all(value is None for value in facts['reasons'].values())
    assert facts['close_final_rial'] is None and not facts['accepted_for_signals']
    assert proof[code]['references'][0]['original_received_at'] < REGISTERED_NEXT
    assert original[code].iloc[0].close_final_raw == 97


def test_selection_and_both_views_replay_offline_after_original_cache_becomes_unavailable(cache_root, monkeypatch):
    folder, original, views, proof, selection, inventory = run(cache_root)
    monkeypatch.setattr('ops.radar_prices.capture_cache', lambda *a, **k: pytest.fail('No live cache'))
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **k: pytest.fail('No HTTP'))
    replayed = replay_session_selection(folder, original, SCOPE, session_date=NEXT,
        registered_at=REGISTERED_NEXT, allowed_files=inventory, bootstrap=True, minimum_calendar_symbols=1)
    assert replayed == selection
    actual, provenance = replay_composition(folder, original, SCOPE, session_date=DAY,
        registered_at=REGISTERED_NEXT, allowed_files=inventory)
    assert provenance == proof
    pd.testing.assert_frame_equal(actual[SCOPE[0]['ins_code']], views[SCOPE[0]['ins_code']])


@pytest.mark.parametrize('file', ['composition-session-selection.json', 'requested-dated-cache-source.json',
    'requested-dated-composition.json', 'requested-derived-prices.parquet', 'dated-cache-source.json'])
def test_missing_or_modified_session_selection_evidence_is_not_replayed(cache_root, file):
    folder, original, _, _, _, inventory = run(cache_root)
    (folder/file).write_bytes((folder/file).read_bytes()+b' ')
    if file == 'dated-cache-source.json':
        with pytest.raises(ValueError):
            replay_composition(folder, original, SCOPE, session_date=DAY,
                registered_at=REGISTERED_NEXT, allowed_files=inventory)
    else:
        with pytest.raises(ValueError):
            replay_session_selection(folder, original, SCOPE, session_date=NEXT,
                registered_at=REGISTERED_NEXT, allowed_files=inventory, bootstrap=True, minimum_calendar_symbols=1)


def test_current_date_cache_is_considered_before_any_bootstrap_fallback(cache_root):
    make_archive(cache_root)
    folder = cache_root.parent/'current-cache'; folder.mkdir()
    views, proof, selection = collect_session_composition(folder, {SCOPE[0]['ins_code']: history()}, SCOPE,
        session_date=DAY, observed_at=AT, registered_at=REGISTERED,
        bootstrap=True, minimum_calendar_symbols=1)
    assert selection['requested_session_present'] and selection['selected_session'] == DAY
    assert not selection['target_changed'] and not (folder/'requested-dated-cache-source.json').exists()
    assert views[SCOPE[0]['ins_code']].date.max() == DAY
    assert proof[SCOPE[0]['ins_code']]['status'] == 'appended_whole_dated_candle'


def test_nonbootstrap_missing_today_remains_missing_today(cache_root):
    _, _, views, proof, selection, _ = run(cache_root, bootstrap=False)
    code = SCOPE[0]['ins_code']
    assert selection['selected_session'] == NEXT and not selection['target_changed']
    assert composition_facts(views[code], proof[code], [DAY, NEXT], session_date=NEXT)['status'] == 'stale_session'


def test_displayed_date_cannot_use_a_different_days_provenance(cache_root):
    _, _, views, proof, _, _ = run(cache_root, conflicting=False)
    code = SCOPE[0]['ins_code']
    with pytest.raises(ValueError, match='Displayed session'):
        composition_facts(views[code], proof[code], [DAY, NEXT], session_date=NEXT)


def test_no_calendar_quorum_does_not_guess_a_baseline(cache_root):
    folder = cache_root.parent/'no-quorum'; folder.mkdir()
    views, proof, selection = collect_session_composition(folder, {SCOPE[0]['ins_code']: history()}, SCOPE,
        session_date=DAY, observed_at=AT, registered_at=REGISTERED, bootstrap=True, minimum_calendar_symbols=20)
    assert selection['selected_session'] == DAY and not selection['inferred_calendar']
    assert composition_facts(views[SCOPE[0]['ins_code']], proof[SCOPE[0]['ins_code']],
        ['2024-01-02', DAY], session_date=DAY)['status'] == 'stale_session'


def test_end_to_end_new_run_keeps_conflict_on_the_displayed_previous_day(cache_root, monkeypatch):
    monkeypatch.setattr(radar, 'now_tehran_iso', lambda: OBSERVED)
    monkeypatch.setattr(radar, 'now_tehran', lambda: datetime.fromisoformat(OBSERVED))
    make_archive(cache_root, tasks=[task(code='1', symbol=identity()['symbol_fa'])],
        response=lambda *a: observation(code='1', native=price(code='1') | dict(pClosing=97)))

    class NativeProvider(PublicProvider):
        supports_history_observations = True
        def get_symbol_history_observation(self, code, last_n):
            body = json.dumps(dict(closingPriceDaily=[price(code=code, day=DAY)])).encode()
            return dict(version=HISTORY_VERSION, source='tsetmc', price_unit='Rial', ins_code=code,
                url=history_request(code, last_n)[1], last_n=last_n, requested_at=OBSERVED,
                received_at=OBSERVED, normalization_as_of=OBSERVED, http_status=200,
                response_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest(), content=body)

    root = settings.DATA_DIR/'private/radar'
    frozen = radar.activate(root, seed=[identity()])
    report, _ = radar.collect_cycle(root, frozen, provider=NativeProvider(), codal=PublicNews(),
        price_scan=True, bootstrap=True)
    first = next(row for row in report['rows'] if row['ins_code'] == '1')
    assert report['requested_price_session'] == NEXT and report['price_session_date'] == DAY
    assert report['composition_session_selection']['target_changed']
    assert first['status'] == 'conflicting_publications'
    assert first['price_publication_provenance']['session_date'] == first['date'] == DAY
    assert sum(row['status'] == 'observed' for row in report['rows']) == 24
    checked = radar.verify(root)
    assert checked['replayed_native_histories'] == checked['replayed_instruments'] == 25
    assert not settings.DB_PATH.exists()
