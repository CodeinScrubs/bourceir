"""داده متناقض نباید در مسیر اصلی فیچر و جمع بازار به عدد معتبر تبدیل شود."""

import ast

import numpy as np
import pandas as pd
import pytest

from features.daily import compute_daily_features
from features.market import compute_market_features
from features.store import build_feature_panel


def coherent_history():
    dates = pd.bdate_range('2024-01-01', periods=25).strftime('%Y-%m-%d')
    prices = pd.DataFrame({
        'date': dates, 'open_raw': 100., 'high_raw': 102., 'low_raw': 98.,
        'close_raw': 100., 'close_final_raw': 100., 'ref_price_raw': 100.,
        'volume': 1_000_000., 'value': 100_000_000., 'trade_count': 300,
    })
    clients = pd.DataFrame({
        'date': dates, 'buy_i_count': 100., 'sell_i_count': 110.,
        'buy_i_volume': 600_000., 'sell_i_volume': 550_000.,
        'buy_i_value': 60_000_000., 'sell_i_value': 55_000_000.,
        'buy_n_volume': 400_000., 'sell_n_volume': 450_000.,
        'buy_n_value': 40_000_000., 'sell_n_value': 45_000_000.,
        'buy_n_count': 20., 'sell_n_count': 20.,
    })
    return prices, clients


@pytest.mark.parametrize('field,value', [
    ('buy_i_value', 90_000_000.),  # قیمت ضمنی ۱۵۰، بیرون بازه؛ جمع ارزش نیز غلط
    ('buy_i_count', -1.), ('buy_i_count', .5), ('buy_i_count', True),
    ('buy_i_volume', 2_000_000.), ('buy_n_value', 0.),
    ('buy_i_value', float('inf')),
])
def test_inconsistent_buy_side_is_unknown_in_main_features(field, value):
    prices, clients = coherent_history()
    if isinstance(value, bool):
        clients[field] = clients[field].astype(object)
    clients.loc[24, field] = value
    before = clients.copy(deep=True)
    result = compute_daily_features(prices, clients)
    for name in ('buyer_power', 'real_flow_ratio', 'per_capita_buy_ratio_20d'):
        assert pd.isna(result.loc[24, name]), name
    pd.testing.assert_frame_equal(clients, before)
    assert result.loc[23, 'buyer_power'] == pytest.approx(1.2)


def test_value_failure_preserves_independently_coherent_volume():
    prices, clients = coherent_history()
    clients.loc[24, 'buy_i_value'] = 90_000_000.
    result = compute_daily_features(prices, clients)
    assert result.loc[24, 'legal_to_real_ratio'] == pytest.approx(.05)


def test_rolling_values_use_only_coherent_matched_days():
    prices, clients = coherent_history()
    clients.loc[20:22, 'buy_i_value'] = 90_000_000.
    result = compute_daily_features(prices, clients)
    assert np.isnan(result.loc[24, 'buyer_power_5d'])
    assert np.isnan(result.loc[24, 'real_flow_ratio_5d'])
    clients.loc[20, 'buy_i_value'] = 60_000_000.
    result = compute_daily_features(prices, clients)
    assert result.loc[24, 'buyer_power_5d'] == pytest.approx(1.2)
    assert result.loc[24, 'real_flow_ratio_5d'] == pytest.approx(.05)


def test_duplicate_client_dates_cannot_expand_or_misalign_features():
    prices, clients = coherent_history()
    duplicate = pd.concat([clients, clients.iloc[[0]]], ignore_index=True)
    with pytest.raises(ValueError, match='تاریخ|unique|one-to-one'):
        compute_daily_features(prices, duplicate)


@pytest.mark.parametrize('bad_symbols,expected', [(1, .05), (2, None)])
def test_market_flow_masks_bad_symbol_in_both_numerator_and_denominator(db, bad_symbols, expected):
    for index, symbol in enumerate(('نمادالف', 'نمادب', 'نمادج')):
        prices, clients = coherent_history()
        if index < bad_symbols:
            clients['buy_i_value'] = 90_000_000.
        symbol_id = db.upsert_symbol({'symbol_fa': symbol, 'ins_code': str(index), 'instrument_type_code': 300})
        db.upsert_daily_prices_df(symbol_id, prices)
        db.upsert_client_types_df(symbol_id, clients)
    panel = build_feature_panel(db)
    values = panel.groupby('date')['mkt_real_flow_ratio'].first()
    if expected is None:
        assert values.isna().all()
    else:
        assert values.eq(expected).all()


def test_dashboard_does_not_display_a_contradictory_buyer_power():
    from tests.test_research_integrity import dashboard_symbol_daily

    prices, clients = coherent_history()
    clients.loc[24, 'buy_i_value'] = 90_000_000.
    result = dashboard_symbol_daily(prices, clients)
    assert pd.isna(result.loc[24, 'buyer_power'])
    assert pd.isna(result.loc[24, 'real_flow'])
    assert result.loc[24, 'client_buy_value_quality'] == 'inconsistent_totals_or_implied_price'
    assert result.loc[24, 'buy_i_value'] == 90_000_000.  # خام حذف یا اصلاح ساختگی نشده است


def test_market_home_coverage_requires_both_coherent_sides():
    from config import settings
    from core import market_rules

    prices, clients = coherent_history()
    panel = prices.head(2).merge(clients, on='date')
    panel.loc[0, 'buy_i_value'] = 90_000_000.

    class ReadOnlyDB:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def get_universe_symbols(self):
            return [{'id': 1}, {'id': 2}]

    tree = ast.parse((settings.BASE_DIR / 'dashboard/app.py').read_text(encoding='utf-8'))
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'market_pulse')
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    namespace = {'ro_db': ReadOnlyDB, 'market_rules': market_rules,
                 'query_df': lambda sql, params: pd.DataFrame({'d': ['2024-01-01']}) if 'MAX(date)' in sql else panel}
    exec(compile(module, 'dashboard/app.py', 'exec'), namespace)
    result = namespace['market_pulse']()
    assert result['real_flow'] == 5_000_000. and result['flow_coverage'] == 1
    panel.loc[1, 'sell_i_value'] = np.nan
    result = namespace['market_pulse']()
    assert result['real_flow'] is None and result['flow_coverage'] == 0


@pytest.mark.parametrize('bad_value,bad_net', [(np.nan, 5.), (100., np.inf), (-100., 5.), (100., 150.)])
def test_market_coverage_does_not_count_an_invalid_pair(bad_value, bad_net):
    panel = pd.DataFrame({
        'date': ['d']*3, 'symbol': ['a', 'b', 'c'], 'ret_1d': 0.,
        'locked_up': 0., 'locked_down': 0., 'real_net_value': [5., bad_net, np.nan],
        'value': [100., bad_value, 100.],
    })
    assert np.isnan(compute_market_features(panel).loc[0, 'mkt_real_flow_ratio'])


@pytest.mark.parametrize('changes', [
    {'real_buy_count': True}, {'real_buy_count': .5}, {'real_buy_volume': 0.},
    {'volume': 100.}, {'volume': 1000., 'legal_buy_volume': 500.},
    {'volume': False}, {'volume': 1000., 'legal_sell_volume': float('inf')},
])
def test_live_buyer_power_rejects_invalid_counts_and_known_inconsistent_totals(changes):
    from data_collector.monitor import buyer_power

    snapshot = dict(real_buy_volume=600., real_buy_count=10., real_sell_volume=400., real_sell_count=10.)
    assert buyer_power(snapshot) == 1.5
    assert buyer_power(dict(snapshot, volume=1000., legal_buy_volume=400., legal_sell_volume=600.)) == 1.5
    assert buyer_power(dict(snapshot, **changes)) is None


def test_client_quality_is_a_required_feature_fingerprint_dependency(monkeypatch):
    from pathlib import Path

    from config import settings
    from features.provenance import feature_logic_fingerprint

    original = feature_logic_fingerprint()
    read = Path.read_bytes
    target = settings.BASE_DIR / 'core/client_quality.py'
    monkeypatch.setattr(Path, 'read_bytes', lambda path: read(path)+b'\n# changed' if path == target else read(path))
    assert feature_logic_fingerprint() != original


def test_sell_failure_does_not_erase_a_coherent_buy_side():
    prices, clients = coherent_history()
    clients.loc[24, 'sell_i_value'] = 90_000_000.
    result = compute_daily_features(prices, clients)
    assert np.isnan(result.loc[24, 'buyer_power'])
    assert result.loc[24, 'per_capita_buy_ratio_20d'] == 1.


def test_consistency_masks_and_rolling_windows_do_not_use_future_revisions():
    prices, clients = coherent_history()
    clients.loc[20:22, 'buy_i_value'] = 90_000_000.
    full = compute_daily_features(prices, clients)
    for stop in (19, 21, 23, 25):
        partial = compute_daily_features(prices.iloc[:stop], clients.iloc[:stop])
        pd.testing.assert_frame_equal(partial, full.iloc[:stop].reset_index(drop=True))


def test_nullable_counts_and_missing_columns_remain_unknown_without_crashing():
    prices, clients = coherent_history()
    clients['buy_i_count'] = clients['buy_i_count'].astype('Int64')
    clients.loc[24, 'buy_i_count'] = pd.NA
    result = compute_daily_features(prices, clients)
    assert np.isnan(result.loc[24, 'buyer_power'])
    minimal = clients[['date']]
    result = compute_daily_features(prices, minimal)
    assert result['buyer_power'].isna().all()
