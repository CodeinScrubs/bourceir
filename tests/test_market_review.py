"""تاریخ هر سهم، دامنه واقعی و مخرج نامعلوم در گزارش توصیفی بازار."""

import copy
import hashlib
import json

import pytest

from config import settings
from core.market_review import build_market_review, market_scope, registry_coverage
from data_collector.tsetmc import TsetmcProvider
from ops.market_review import collect_market_review, load_market_review, reanalyze_market_review, render_html
from tests.conftest import FOOLAD_INS, FakeSession

DATE = '2026-10-17'
STAMP = DATE + 'T12:29:59+03:30'
RECEIVED = DATE + 'T13:15:00+03:30'
NOW = DATE + 'T13:16:00+03:30'


def item(code='1', symbol='کیان', kind=300):
    return dict(metadata=dict(ins_code=code, symbol_fa=symbol, instrument_type_code=kind,
                              sector_code='27', shares_outstanding=1000), snapshot={})


def closing(code='1', **changes):
    candle = dict(date=DATE, open_raw=100, low_raw=98, high_raw=103, close_raw=102,
                  close_final_raw=101, ref_price_raw=100, volume=100, value=10_100, trade_count=2)
    candle.update(changes)
    return dict(ins_code=code, received_at=RECEIVED, provider_timestamp=STAMP, candle=candle)


def review(items=None, prices=None, headlines=None):
    return build_market_review(dict(instruments=items if items is not None else [item()]),
                               prices if prices is not None else {'1': closing()}, headlines or [],
                               {'27': 'فلزات'}, session_date=DATE, observed_at=NOW)


@pytest.mark.parametrize('payload', [[], {'marketwatch': {}}, {'marketwatch': [None]}])
def test_invalid_market_payload_is_unknown_not_an_exception(payload):
    provider = TsetmcProvider(session=FakeSession({'GetMarketWatch': payload}))
    assert provider.get_market_snapshots() is None
    assert provider.get_market_observation() is None


@pytest.mark.parametrize('payload', [None, [], 'unavailable', 500, True])
def test_provider_json_root_contract_protects_other_callers(payload):
    provider = TsetmcProvider(session=FakeSession({'GetClosingPriceInfo': payload}))
    assert provider._get_json('GetClosingPriceInfo', rate_limit=False) is None


@pytest.mark.parametrize('payload', [[], {'clientTypeAllDto': {}}, {'clientTypeAllDto': [None]}])
def test_invalid_client_payload_does_not_crash_market_collection(payload):
    provider = TsetmcProvider(session=FakeSession({'GetClientTypeAll': payload}))
    assert provider.get_client_types_today() == {}


def test_duplicate_client_identity_is_not_last_row_wins():
    provider = TsetmcProvider(session=FakeSession({'GetClientTypeAll': {'clientTypeAllDto': [
        {'insCode': '1', 'buy_I_Volume': 100}, {'insCode': '1', 'buy_I_Volume': 900}, {'insCode': '2'}]}}))
    assert set(provider.get_client_types_today()) == {'2'}


@pytest.mark.parametrize('bad_time', ['wrong', 999999, None, float('inf')])
def test_invalid_time_and_book_levels_remain_unknown(market_watch_rows, bad_time):
    row = dict(market_watch_rows[0], hEven=bad_time, blDs=[None, {'n': '1'}, {'n': True}, {'n': 6}])
    snapshot = TsetmcProvider.snapshot_from_market_watch(row, None, RECEIVED)
    assert snapshot['last_trade_time'] is None and snapshot['provider_timestamp'] is None
    assert snapshot['buy_queue_volume'] is None


def test_conflicting_book_level_is_not_last_row_wins(market_watch_rows):
    row = copy.deepcopy(market_watch_rows[0])
    row['blDs'].append(dict(row['blDs'][0], qmd=900_000_000))
    snapshot = TsetmcProvider.snapshot_from_market_watch(row, None, RECEIVED)
    assert snapshot['bid_vol_1'] is None and snapshot['ask_price_1'] is None
    assert snapshot['buy_queue_volume'] is None and snapshot['sell_queue_volume'] is None


@pytest.mark.parametrize('payload', [[], {'closingPriceInfo': []}, {'closingPriceInfo': {'finalLastDate': 'wrong'}}])
def test_market_date_invalid_payload_is_unknown(payload):
    provider = TsetmcProvider(session=FakeSession({'GetClosingPriceInfo': payload}))
    assert provider.get_market_date('1') is None


@pytest.mark.parametrize('payload', [[], {'marketwatch': {}}, {'marketwatch': [None]}])
def test_session_candle_invalid_payload_is_unknown(payload):
    provider = TsetmcProvider(session=FakeSession({'GetMarketWatch': payload}))
    assert provider.get_session_candles() is None


def test_market_filter_does_not_truncate_fractional_type(market_watch_rows):
    row = dict(market_watch_rows[0], yVal=300.7)
    provider = TsetmcProvider(session=FakeSession({'GetMarketWatch': {'marketwatch': [row]}}))
    assert market_scope(provider.get_market_observation())[0] == []


@pytest.mark.parametrize('final_date, expected', [(20261017, DATE), (20261014, None), (0, None)])
def test_closing_date_requires_two_matching_source_fields(final_date, expected):
    payload = {'closingPriceInfo': {'dEven': 20261017, 'finalLastDate': final_date, 'hEven': 122959, 'insCode': '0'}}
    provider = TsetmcProvider(session=FakeSession({'GetClosingPriceInfo': payload}))
    result = provider.get_closing_observation(FOOLAD_INS)
    assert result['candle']['date'] == expected
    assert result['identity_attestation'] == 'request_url_only'


def test_response_identity_conflict_blocks_candle():
    received = closing()
    received['identity_attestation'] = 'conflicting_response'
    result = review(prices={'1': received})
    assert result['instruments'][0]['status'] == 'identity_conflict'
    assert result['summary']['value_rial'] is None


def test_scope_excludes_funds_base_block_and_all_ambiguous_rows():
    scope, excluded = market_scope(dict(instruments=[item(), item('2', 'دارا', 305), item('3', 'پایه', 309),
        item('4', 'کیان2'), item('5', 'تکرار'), item('6', 'تکرار'), item('7', 'دیگر'), item('7', 'دوم')]))
    assert [row['metadata']['ins_code'] for row in scope] == ['1']
    assert excluded == {'outside_or_unknown_scope': 3, 'ambiguous_identity_rows': 4}


def test_missing_dates_and_stale_rows_do_not_become_today_from_capture_time():
    items = [item(), item('2', 'دوم'), item('3', 'سوم'), item('4', 'چهارم')]
    stale = closing('2', date='2026-10-14')
    stale['provider_timestamp'] = '2026-10-14T12:29:59+03:30'
    undated = closing('3', date=None)
    result = review(items, {'1': closing(), '2': stale, '3': undated})
    assert result['summary']['scope_count'] == 4
    assert result['summary']['traded_count'] == 1
    assert result['summary']['value_rial'] == 10_100
    assert result['summary']['status_counts'] == {'traded': 1, 'stale_session': 1,
                                                  'unknown_or_conflicting_date': 1, 'unavailable': 1}
    assert result['status'] == 'partial'


def test_impossible_daily_value_or_ohlc_is_not_aggregated():
    for values in ({'value': 100_000}, {'open_raw': 110}, {'close_raw': 0}, {'volume': 1.5}):
        result = review(prices={'1': closing(**values)})
        assert result['summary']['traded_count'] == 0
        assert result['summary']['value_rial'] is None
        assert result['instruments'][0]['status'] == 'invalid_candle'


def test_unknown_reference_and_official_close_are_not_zero_return():
    result = review(prices={'1': closing(ref_price_raw=0)})
    assert result['summary']['value_rial'] == 10_100
    assert result['summary']['positive_fraction'] is None
    assert result['summary']['return_unknown_count'] == 1
    assert result['instruments'][0]['closing_return'] is None
    assert review(prices={'1': closing(close_final_raw=None)})['summary']['return_known_count'] == 0


def test_official_close_outside_trading_candle_is_allowed():
    result = review(prices={'1': closing(close_final_raw=104)})
    assert result['summary']['traded_count'] == 1
    assert result['instruments'][0]['closing_return'] == pytest.approx(.04)


def test_zero_activity_does_not_count_as_a_positive_carried_price():
    result = review(prices={'1': closing(volume=0, trade_count=0)})
    assert result['summary']['status_counts'] == {'not_traded': 1}
    assert result['summary']['positive'] == 0 and result['summary']['value_rial'] is None


def test_future_or_naive_source_clock_is_not_current_data():
    for stamp in ('2026-10-17T13:15:01+03:30', '2026-10-17T12:29:59'):
        received = closing()
        received['provider_timestamp'] = stamp
        assert review(prices={'1': received})['summary']['traded_count'] == 0


def test_single_price_proxy_is_not_a_verified_queue_or_real_money_flow():
    result = review(prices={'1': closing(open_raw=102, high_raw=102, low_raw=102, close_raw=102,
                                         close_final_raw=102, value=10_200)})
    assert result['summary']['one_price_up_proxy_count'] == 1
    assert result['summary']['verified_queue_count'] is None
    assert result['summary']['verified_retail_net_value_rial'] is None
    assert result['accepted_for_signals'] is False


def test_headline_indices_require_same_session_context():
    headline = dict(flow=1, provider_timestamp=STAMP, received_at=RECEIVED,
                    index_value=990, index_change=-10, equal_weight_value=202, equal_weight_change=2)
    result = review(headlines=[headline])
    assert result['official_overviews'][0]['index_return'] == pytest.approx(-.01)
    assert result['official_overviews'][0]['equal_weight_return'] == pytest.approx(.01)
    headline['provider_timestamp'] = '2026-10-14T12:30:00+03:30'
    assert review(headlines=[headline])['official_overviews'][0]['index_value'] is None


def test_industry_and_cap_are_descriptive_and_html_escapes_source_text():
    result = review([item(symbol='<script>bad</script>')])
    assert result['sectors'][0]['name'] == 'فلزات'
    assert result['largest_observed'][0]['market_cap_observed_rial'] == 101_000
    assert not result['historical_universe_verified']
    body = render_html(result)
    assert '<script>bad</script>' not in body and '&lt;script&gt;' in body
    assert 'بازده شاخص رسمی صنعت نیست' in body
    assert '<td dir="ltr" style="unicode-bidi:isolate">+1.00٪</td>' in body


class ReviewProvider:
    name = 'test-only'

    def __init__(self, root, count=1, unavailable=False):
        self.root, self.count, self.unavailable, self.calls = root, count, unavailable, []

    def get_market_observation(self):
        assert list(self.root.rglob('protocol.json'))
        return dict(instruments=[item(str(i), 'نماد' + chr(1570+i)) for i in range(1, self.count + 1)])

    def get_closing_observation(self, code):
        selection = next(self.root.rglob('selection.json'))
        assert code in json.loads(selection.read_bytes())['ins_codes']
        self.calls.append(code)
        return None if self.unavailable else closing(code)

    def get_market_overview(self, flow):
        return None

    def get_sector_names(self):
        return {'27': 'فلزات'}


def test_collection_registers_scope_before_calls_and_preserves_hashes(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    root = tmp_path / 'private' / 'reviews'
    provider = ReviewProvider(root)
    result = collect_market_review(provider, output_root=root)
    folder = root / result['report']['run_id']
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    assert provider.calls == ['1']
    assert (folder / 'report.html').is_file()
    for name, expected in manifest['files'].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == expected
    assert not (tmp_path / 'brs.db').exists()


def test_collection_stops_on_five_failures_and_retains_all_denominators(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    root = tmp_path / 'private' / 'reviews'
    provider = ReviewProvider(root, count=12, unavailable=True)
    result = collect_market_review(provider, output_root=root)
    assert len(provider.calls) == 5
    assert result['report']['collection_unattempted'] == 7
    assert result['report']['summary']['scope_count'] == 12
    assert result['report']['summary']['value_rial'] is None


def test_rejected_output_is_detected_before_network(tmp_path):
    provider = ReviewProvider(tmp_path)
    with pytest.raises(ValueError, match='private'):
        collect_market_review(provider, output_root=tmp_path / 'public')
    assert provider.calls == []


def test_core_does_not_mutate_source_observations():
    raw = {'1': closing()}
    before = copy.deepcopy(raw)
    review(prices=raw)
    assert raw == before


def test_missing_registry_symbols_are_not_deleted_or_assumed_halted():
    registry = [item()['metadata'], item('2', 'غایب')['metadata']]
    registry[0]['is_active'] = 0
    result = registry_coverage(dict(instruments=[item(), item('3', 'تازه')]), registry)
    assert result['known_registry_count'] == 2
    assert [row['symbol_fa'] for row in result['registry_not_observed']] == ['غایب']
    assert [row['symbol_fa'] for row in result['observed_not_in_registry']] == ['تازه']
    assert not result['absence_reason_verified']
    assert [row['symbol_fa'] for row in result['registry_inactive_but_observed']] == ['کیان']


def test_archive_reanalysis_preserves_source_time_and_does_not_refetch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    root = tmp_path / 'private' / 'reviews'
    provider = ReviewProvider(root)
    collected = collect_market_review(provider, output_root=root)
    replay = reanalyze_market_review(collected['folder'], output_root=root, registry=[item()['metadata']])
    assert len(provider.calls) == 1
    assert replay['report']['summary'] == collected['report']['summary']
    assert replay['report']['observed_at'] == collected['report']['observed_at']
    assert replay['report']['network_used'] is False
    assert replay['report']['normalization_reexecuted'] is False
    assert load_market_review(replay['folder'])['run_id'] == replay['report']['run_id']


def test_changed_market_evidence_is_rejected_before_reanalysis(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    root = tmp_path / 'private' / 'reviews'
    result = collect_market_review(ReviewProvider(root), output_root=root)
    from pathlib import Path

    (Path(result['folder']) / 'closing-1.json').write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='هش'):
        load_market_review(result['folder'])
    with pytest.raises(ValueError, match='هش'):
        reanalyze_market_review(result['folder'], output_root=root)


def test_cli_will_not_overwrite_an_evidence_archive(tmp_path, monkeypatch):
    import brs

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    args = brs.build_parser().parse_args(['market-review', '--json', str(tmp_path / 'private/market-reviews/old/manifest.json')])
    with pytest.raises(ValueError, match='آرشیو'):
        args.func(args)


def test_provider_uses_typed_all_instrument_endpoint(market_watch_rows):
    session = FakeSession({'GetMarketWatch': {'marketwatch': market_watch_rows}})
    observation = TsetmcProvider(session=session).get_market_observation()
    assert 'paperTypes%5B8%5D=9' in session.calls[0]
    assert [row['metadata']['symbol_fa'] for row in market_scope(observation)[0]] == ['خزامیا', 'فولاد']
