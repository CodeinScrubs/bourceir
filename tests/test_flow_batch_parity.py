"""Batching must preserve the published formulas, unknowns and symbol boundaries."""

import hashlib
import importlib.machinery
import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from core import flow_filters
from tests.test_flow_filters import history


@pytest.fixture(scope='module')
def published():
    path = Path(__file__).parent / 'fixtures/flow-v3-0752/flow_filters.py.txt'
    loader = importlib.machinery.SourceFileLoader('published_flow_filters_0752', str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        '9c07ae70e5cf109a34b79b55e90f9069e428ca7cf04aafc1049eb8921b132c6a')
    return module


def inputs(case):
    prices, clients = [], []
    for symbol, sessions, scale in (('ب', 93, 1.), ('آ', 78, 9.), ('ج', 9, .2)):
        p, c = history(sessions)
        p['symbol'], c['symbol'] = symbol, symbol
        for column in ('open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value'):
            p[column] *= scale
        c[['buy_i_value', 'sell_i_value']] *= scale
        if case == 'variable':
            close = 1000 * scale * np.cumprod(1 + .002 * np.sin(np.arange(sessions) * .53 + scale))
            ref = np.r_[1000 * scale, close[:-1]]
            p['close_raw'], p['close_final_raw'], p['ref_price_raw'], p['open_raw'] = close, close, ref, ref
            p['high_raw'], p['low_raw'] = np.maximum(close, ref) + 1, np.minimum(close, ref) - 1
            p['value'] = close * p.volume
            c['buy_i_value'], c['sell_i_value'] = close * c.buy_i_volume, close * c.sell_i_volume
        elif case == 'missing':
            c = c.drop(index=c.index[::7])
            p.loc[11:13, 'volume'] = np.nan
        elif case == 'corporate':
            p.loc[35:, ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'value']] /= 2
            c.loc[35:, ['buy_i_value', 'sell_i_value']] /= 2
        elif case == 'gaps':
            p = p.drop(index=p.index[::9])
            p.loc[p.index[-1], 'ref_price_raw'] = np.nan
        elif case == 'invalid':
            c['buy_i_count'] = c.buy_i_count.astype(object)
            c.loc[c.index[-1], 'buy_i_count'] = True
            p.loc[p.index[-2], 'high_raw'] = np.inf
            c.loc[c.index[-3], 'sell_i_value'] = -1.
        prices.append(p)
        clients.append(c)
    # Deliberately duplicated index labels and mixed input order cannot align symbols.
    return (pd.concat(prices).sample(frac=1, random_state=17),
            pd.concat(clients).sample(frac=1, random_state=23))


@pytest.mark.parametrize('case', ['regular', 'variable', 'missing', 'corporate', 'gaps', 'invalid'])
@pytest.mark.parametrize('version', [3, 4])
def test_batch_is_identical_to_the_published_per_symbol_calculation(published, case, version):
    prices, clients = inputs(case)
    original_prices, original_clients = prices.copy(deep=True), clients.copy(deep=True)
    name = 'flow_metrics' if version == 3 else 'flow_metrics_v4'
    expected = getattr(published, name)(prices, clients)
    actual = getattr(flow_filters, name)(prices, clients)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)
    pd.testing.assert_frame_equal(prices, original_prices)
    pd.testing.assert_frame_equal(clients, original_clients)


def test_short_symbol_cannot_borrow_rolling_warmup_or_client_data():
    prices, clients = inputs('regular')
    clients = clients.loc[clients.symbol != 'ج']
    result = flow_filters.flow_metrics(prices, clients)
    short = result.loc[result.symbol == 'ج']
    assert len(short) == 9
    assert short[['volume_ratio_20d', 'volume_ratio_30d', 'buyer_power_value']].isna().all().all()
    assert short.breakout_60d.isna().all()


def test_participant_quality_is_checked_once_per_side_for_the_whole_batch(monkeypatch):
    prices, clients = inputs('regular')
    original = flow_filters._coherent_side
    calls = []

    def check(p, c, side):
        calls.append((side, len(p), len(c)))
        return original(p, c, side)

    monkeypatch.setattr(flow_filters, '_coherent_side', check)
    flow_filters.flow_metrics(prices, clients)
    assert calls == [('buy', len(prices), len(prices)), ('sell', len(prices), len(prices))]


def test_v4_explicit_calendar_gaps_match_the_published_unknown_policy(published):
    prices, clients = inputs('gaps')
    calendar = pd.bdate_range('2024-01-01', periods=93).strftime('%Y-%m-%d').tolist()
    expected = published.flow_metrics_v4(prices, clients, calendar=calendar)
    actual = flow_filters.flow_metrics_v4(prices, clients, calendar=calendar)
    pd.testing.assert_frame_equal(actual, expected, check_exact=True)


def test_v4_validates_lock_inputs_once_for_the_whole_batch(monkeypatch):
    prices, clients = inputs('regular')
    original = flow_filters.market_rules.lock_flags
    calls = []

    def check(frame, *args, **kwargs):
        calls.append(len(frame))
        return original(frame, *args, **kwargs)

    monkeypatch.setattr(flow_filters.market_rules, 'lock_flags', check)
    flow_filters.flow_metrics_v4(prices, clients)
    assert calls == [len(prices)]
