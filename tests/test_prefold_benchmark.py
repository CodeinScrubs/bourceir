"""Pre-fold control cannot use Day-1 values, later activity, or future price adjustments."""

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from research.prefold_benchmark import prefold_selection, review_prefold_benchmark, run_prefold_benchmark
from research.strategy_suite import SuitePlan, run_suite
from tests.test_strategy_suite import inputs


def panel():
    prices, _, registry, days = inputs(50, ('a', 'b', 'c', 'd', 'e', 'f', 'g'))
    registry['ins_code'] = ['10', '2', '3', '4', '5', '6', '7']
    return prices, registry, days


def test_future_spike_adjusted_columns_and_present_activity_cannot_change_a_prefold_pick():
    prices, registry, days = panel()
    first = prefold_selection(prices, registry, days, days[25])
    changed = prices.copy()
    changed.loc[changed.date.ge(days[25]), 'value'] = 10**18
    changed['adj_close'] = 10**18
    changed['adjustment_factor'] = 0
    registry['is_active'] = False
    assert prefold_selection(changed, registry, days, days[25]) == first
    assert first['lookback_sessions'] == days[5:25]
    assert first['decision_date'] == days[24] and all(p['date'] < days[25] for p in first['picks'])


def test_equal_medians_tie_on_numeric_identity_and_only_five_are_selected():
    prices, registry, days = panel()
    for col in ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw'):
        prices[col] = 100
    prices['volume'], prices['value'] = 1000, 100000
    choice = prefold_selection(prices.sample(frac=1, random_state=1), registry.iloc[::-1], days, days[25])
    assert [p['symbol'] for p in choice['picks']] == ['2', '3', '4', '5', '6']
    assert sum(r['selected'] for r in choice['eligibility']) == 5


@pytest.mark.parametrize('valid_count,eligible', [(14, False), (15, True)])
def test_missing_days_do_not_pull_in_older_observations(valid_count, eligible):
    prices, registry, days = panel()
    removed = days[5:25-valid_count]
    prices = prices.loc[~(prices.symbol.eq('a') & prices.date.isin(removed))]
    choice = prefold_selection(prices, registry, days, days[25])
    row = next(r for r in choice['eligibility'] if r['symbol'] == 'a')
    assert row['valid_observations'] == valid_count and row['eligible'] is eligible
    assert row['missing_observations'] == 20-valid_count


def test_zero_and_incoherent_rows_are_counted_without_median_imputation():
    prices, registry, days = panel()
    own = prices.symbol.eq('a') & prices.date.isin(days[5:11])
    prices.loc[own, 'volume'] = 0
    row = next(r for r in prefold_selection(prices, registry, days, days[25])['eligibility'] if r['symbol'] == 'a')
    assert row['valid_observations'] == 14 and not row['eligible']
    assert row['invalid_or_zero_observations'] == 6


def test_instrument_type_and_duplicate_history_fail_closed():
    prices, registry, days = panel()
    registry.loc[0, 'instrument_type_code'] = 305
    choice = prefold_selection(prices, registry, days, days[25])
    assert next(r for r in choice['eligibility'] if r['symbol'] == 'a')['exclusion'] == 'not_ordinary_stock'
    with pytest.raises(ValueError, match='Duplicate'):
        prefold_selection(pd.concat([prices, prices.loc[prices.date.eq(days[10])]]), registry, days, days[25])
    with pytest.raises(ValueError, match='Twenty'):
        prefold_selection(prices, registry, days, days[19])


def test_registered_prefold_replay_buys_on_first_test_session_and_exits_only_after_base(tmp_path, monkeypatch, db):
    import research.labels as labels
    import research.strategy_suite as suite

    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setattr(labels, 'CALENDAR_MIN_SYMBOLS', 2)
    prices, clients, registry, days = inputs(160, ('a', 'b', 'c', 'd', 'e', 'f'))
    monkeypatch.setattr(suite, 'snapshot_inputs', lambda *_: (prices, clients, registry))
    source = run_suite(db, plan=SuitePlan(start=days[0], end_exclusive='2024-07-01', initial_capital_rial=10000,
        min_daily_value_rial=0, min_train_sessions=60, test_sessions=30), case_names=['buy_hold', 'control:liquidity:h1'])
    parent = Path(source['folder'])
    before = (parent / 'manifest.json').read_bytes()
    publications = tmp_path / 'data/private/no-publications.json'
    publications.write_text(json.dumps(dict(kind='priority_missing_session_publications', records=[])), encoding='utf-8')
    result = run_prefold_benchmark(parent, publications)
    assert not result['invariant_failures']
    folder = Path(result['folder'])
    assert review_prefold_benchmark(folder, reexecute=True)['financial_reexecution_performed']
    assert (parent / 'manifest.json').read_bytes() == before
    with zipfile.ZipFile(folder / 'replays.zip') as archive:
        for row in result['records']:
            item = json.loads(archive.read(row['member']))
            if not item['financial_path_computed']:
                continue
            buys = [t for t in item['executions'] if t['side'] == 'buy']
            sells = [t for t in item['executions'] if t['side'] == 'sell']
            assert buys and all(t['date'] == row['test_start'] for t in buys)
            assert all(t['date'] > row['base_endpoint_date'] for t in sells)
            assert not item['actual_execution_verified']
            if row['liquidation_sessions'] == 0:
                assert item['model_nav_known'] and not item['modeled_terminal_cash_known']
    original = {name: (folder / name).read_bytes() for name in
                ('protocol.json', 'results.json', 'replays.zip', 'manifest.json')}
    for mutation in ('economic_claim', 'actual_fill_claim', 'changed_picks', 'changed_lookback', 'unsealed_claim'):
        for name, body in original.items():
            (folder / name).write_bytes(body)
        if mutation in ('changed_lookback', 'unsealed_claim'):
            protocol = json.loads(original['protocol.json'])
            protocol['lookback' if mutation == 'changed_lookback' else 'holdout_opened'] = (
                19 if mutation == 'changed_lookback' else True)
            (folder / 'protocol.json').write_text(json.dumps(protocol), encoding='utf-8')
        else:
            saved = json.loads(original['results.json'])
            record = saved['records'][0]
            with zipfile.ZipFile(io.BytesIO(original['replays.zip'])) as archive:
                members = {name: archive.read(name) for name in archive.namelist()}
            item = json.loads(members[record['member']])
            if mutation == 'changed_picks':
                item['picks'][0]['symbol'] = '999999'
            else:
                item['economic_accounting_verified' if mutation == 'economic_claim' else 'actual_execution_verified'] = True
            members[record['member']] = json.dumps(item).encode()
            record['sha256'] = hashlib.sha256(members[record['member']]).hexdigest()
            with zipfile.ZipFile(folder / 'replays.zip', 'w') as archive:
                for name, body in members.items():
                    archive.writestr(name, body)
            (folder / 'results.json').write_text(json.dumps(saved), encoding='utf-8')
        manifest = json.loads(original['manifest.json'])
        for name in original:
            if name != 'manifest.json':
                manifest[name] = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        (folder / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        with pytest.raises(ValueError):
            review_prefold_benchmark(folder)
    for name, body in original.items():
        (folder / name).write_bytes(body)
    with (folder / 'selections.json').open('a') as stream:
        stream.write(' ')
    with pytest.raises(ValueError, match='manifest'):
        review_prefold_benchmark(folder)
