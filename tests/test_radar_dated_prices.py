"""Source-bound whole-day adapter replay; no live cache or account state required."""

import hashlib
import json
from datetime import datetime

import pandas as pd
import pytest

from core.dated_history import compose_history
from core.opportunity_radar import attention_events
from data_collector.daily_history import VERSION as HISTORY_VERSION
from data_collector.daily_history import history_request
from data_collector.daily_publications import DailyPublicationArchive
from ops import opportunity_radar as radar
from ops.radar_prices import collect_composition, composition_facts, composition_summary, replay_composition
from ops.report_files import read_private_report
from tests.test_dated_history import AT, REGISTERED, history, publication
from tests.test_publication_cache import DAY, SCOPE, make_archive, price
from tests.test_publication_cache import cache_root as cache_root


def scope():
    return [dict(SCOPE[0], symbol_fa='آزمون')]


def cycle(cache_root, *, conflicting=False):
    make_archive(cache_root)
    folder = cache_root.parent/'radar-cycle'
    folder.mkdir()
    histories = {SCOPE[0]['ins_code']: history(current=conflicting)}
    if conflicting:
        histories[SCOPE[0]['ins_code']].loc[0, 'close_final_raw'] = 97
    views, provenance = collect_composition(folder, histories, scope(), session_date=DAY,
        observed_at=AT, registered_at=REGISTERED)
    inventory = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir()}
    return folder, histories, views, provenance, inventory


def test_adapter_freezes_public_bytes_and_replays_without_original_cache(cache_root, monkeypatch):
    folder, histories, views, provenance, inventory = cycle(cache_root)
    monkeypatch.setattr('requests.sessions.Session.request', lambda *a, **kw: pytest.fail('No HTTP'))
    monkeypatch.setattr('ops.radar_prices.capture_cache', lambda *a, **kw: pytest.fail('No live cache on replay'))
    # The original cache may become corrupted later; the cycle remains reproducible.
    origin = next((cache_root/DAY).glob('DAYPUB-*/observation-00000.json'))
    origin.write_bytes(b'corrupted origin must not be read by replay')
    rebuilt, proof = replay_composition(folder, histories, scope(), session_date=DAY,
        registered_at=REGISTERED, allowed_files=inventory)
    code = SCOPE[0]['ins_code']
    pd.testing.assert_frame_equal(rebuilt[code], views[code])
    assert proof == provenance
    facts = composition_facts(rebuilt[code], proof[code], ['2024-01-02', DAY], session_date=DAY)
    assert facts['status'] == 'observed' and facts['reasons']['breakout'] is None
    assert facts['source_initialization_baseline'] is True
    assert facts['price_publication_provenance']['references'][0]['original_received_at'] != AT
    assert composition_summary(proof)['statuses'] == {'appended_whole_dated_candle': 1}
    assert all(b'must not be copied' not in (folder/name).read_bytes() for name in inventory)


def test_conflicting_whole_day_makes_all_dependent_flags_unknown(cache_root):
    folder, histories, _, provenance, inventory = cycle(cache_root, conflicting=True)
    rebuilt, proof = replay_composition(folder, histories, scope(), session_date=DAY,
        registered_at=REGISTERED, allowed_files=inventory)
    code = SCOPE[0]['ins_code']
    facts = composition_facts(rebuilt[code], proof[code], [DAY], session_date=DAY)
    assert provenance[code]['status'] == facts['status'] == 'conflicting_publications'
    assert all(value is None for value in facts['reasons'].values())
    assert facts['close_final_rial'] is None and facts['probability_of_surge'] is None
    assert histories[code].loc[0, 'close_final_raw'] == 97


@pytest.mark.parametrize('target', ['dated-cache-source.json', 'dated-composition.json', 'derived-prices.parquet', 'native'])
def test_changed_composition_archive_member_is_rejected_before_replay(cache_root, target):
    folder, histories, _, _, inventory = cycle(cache_root)
    if target == 'native':
        target = next(name for name in inventory if name.endswith('observation-00000.json'))
    path = folder/target
    path.write_bytes(path.read_bytes()+b' ')
    with pytest.raises(ValueError, match='digest differs'):
        replay_composition(folder, histories, scope(), session_date=DAY,
            registered_at=REGISTERED, allowed_files=inventory)


def test_unbound_publication_and_changed_registration_cannot_enter_replay(cache_root):
    folder, histories, _, _, inventory = cycle(cache_root)
    file = next(name for name in inventory if name.endswith('observation-00000.json'))
    del inventory[file]
    with pytest.raises(ValueError, match='bound'):
        replay_composition(folder, histories, scope(), session_date=DAY,
            registered_at=REGISTERED, allowed_files=inventory)


def test_no_candle_or_short_history_does_not_invent_rolling_pattern():
    code = SCOPE[0]['ins_code']
    view, proof = compose_history(None, [publication()], ins_code=code,
        instrument_type_code=300, session_date=DAY, observed_at=AT, registered_at=REGISTERED)
    facts = composition_facts(view, proof, [DAY], session_date=DAY)
    assert facts['status'] == 'observed'
    assert facts['reasons']['breakout'] is facts['reasons']['value_surge'] is None


def test_zero_observation_after_missing_history_stays_unavailable():
    zero = publication(native=price() | dict(qTotTran5J=0, qTotCap=0, zTotTran=0))
    view, proof = compose_history(None, [zero], ins_code=SCOPE[0]['ins_code'],
        instrument_type_code=300, session_date=DAY, observed_at=AT, registered_at=REGISTERED)
    facts = composition_facts(view, proof, [DAY], session_date=DAY)
    assert facts['status'] == 'unavailable'
    assert proof['status'] == 'zero_activity_no_candle'


@pytest.mark.parametrize('baseline,kind', [(True, 'baseline_observation'), (False, 'new_attention')])
def test_cached_initialization_is_never_backdated_or_scored_as_new_prediction(baseline, kind):
    row = dict(ins_code='1', symbol='آزمون', date=DAY,
               reasons=dict(breakout=True, value_surge=None, locked_up=False),
               source_initialization_baseline=baseline)
    state, events = attention_events({}, [row], [], registered_at=REGISTERED, observed_at=AT, price_scan=True)
    assert len(events) == 1 and events[0]['kind'] == kind
    assert events[0]['first_seen_at'] == AT and events[0]['baseline'] is baseline
    assert state['episodes']['1:breakout']['first_seen_at'] == AT


def test_old_cached_observation_after_gap_does_not_create_a_fresh_resume_alert():
    row = dict(ins_code='1', symbol='آزمون', date=DAY,
               reasons=dict(breakout=True, value_surge=None, locked_up=False))
    state, events = attention_events({}, [row], [], registered_at=REGISTERED, observed_at=AT, price_scan=True)
    assert events[0]['baseline'] is False
    unknown = dict(row, reasons=dict(breakout=None, value_surge=None, locked_up=None))
    state, events = attention_events(state, [unknown], [], registered_at=REGISTERED,
                                    observed_at='2024-01-03T15:01:00+03:30', price_scan=True)
    assert not events
    old = dict(row, source_initialization_baseline=True)
    state, events = attention_events(state, [old], [], registered_at=REGISTERED,
                                    observed_at='2024-01-03T15:02:00+03:30', price_scan=True)
    assert events[0]['kind'] == 'attention_observed_after_gap' and events[0]['baseline'] is True
    assert events[0]['first_seen_at'] == AT
    assert state['episodes']['1:breakout']['baseline'] is False


def test_real_recorder_interface_admits_one_cached_day_and_replays_offline(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from config import settings
    from tests.test_daily_publications import observation, task
    from tests.test_opportunity_radar import PublicNews, PublicProvider, identity

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr(settings, 'DB_PATH', tmp_path/'account.db')
    monkeypatch.setattr('data_collector.daily_publications.now_tehran_iso', lambda: '2024-01-03T14:00:00+03:30')
    monkeypatch.setattr(radar, 'now_tehran_iso', lambda: AT)
    monkeypatch.setattr(radar, 'now_tehran', lambda: datetime.fromisoformat(AT))
    cache_folder = settings.DATA_DIR/'private/daily-publications'
    archive = DailyPublicationArchive(cache_folder, [task(code='1', symbol=identity()['symbol_fa'])])
    native = price(code='1') | dict(priceFirst=104, priceMax=104, priceMin=104,
                                   pDrCotVal=104, pClosing=104, qTotCap=10400)
    archive.capture(SimpleNamespace(get_daily_publication_observation=lambda *a:
                                   observation(code='1', native=native)))
    archive.finish(dict(storage_not_required=True))

    class PreviousDayProvider(PublicProvider):
        supports_history_observations = True

        def get_symbol_history_observation(self, code, last_n):
            self.calls.append(code)
            body = json.dumps(dict(closingPriceDaily=[price(code=code, day='2024-01-02')])).encode()
            return dict(version=HISTORY_VERSION, source='tsetmc', price_unit='Rial', ins_code=code,
                url=history_request(code, last_n)[1], last_n=last_n, requested_at=AT,
                received_at=AT, normalization_as_of=AT, http_status=200,
                response_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest(), content=body)

        def get_daily_publication_observation(self, *args):
            pytest.fail('A cache miss must not start new dated requests')

    root = settings.DATA_DIR/'private/radar'
    frozen = radar.activate(root, seed=[identity()])
    provider = PreviousDayProvider()
    result, receipt = radar.collect_cycle(root, frozen, provider=provider,
        codal=PublicNews(), price_scan=True, bootstrap=True)
    assert len(provider.calls) == 25 and len(result['rows']) == 25
    first = next(row for row in result['rows'] if row['ins_code'] == '1')
    assert first['status'] == 'observed' and first['date'] == DAY
    assert first['source_initialization_baseline'] is True
    assert first['reasons']['locked_up'] is True
    assert sum(row['status'] == 'stale_session' for row in result['rows']) == 24
    event = next(row for row in receipt['events'] if row.get('reason') == 'locked_up')
    assert event['kind'] == 'baseline_observation' and event['first_seen_at'] == AT
    folder = root/result['artifacts_folder']
    assert read_private_report(folder/'dated-cache-source.json')['native_requests'] == 0
    raw_history = pd.read_parquet(folder/'prices.parquet')
    assert DAY not in set(raw_history.date)
    derived = pd.read_parquet(folder/'derived-prices.parquet')
    assert sum(derived.date.eq(DAY)) == 1
    monkeypatch.setattr('ops.radar_prices.capture_cache', lambda *a, **kw: pytest.fail('No live cache during replay'))
    verified = radar.verify(root)
    assert verified['replayed_instruments'] == verified['replayed_native_histories'] == 25
    assert not settings.DB_PATH.exists()
