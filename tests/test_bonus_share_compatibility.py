"""خروجی ثبت‌شده موتور پیش از تغییر، مستقل از اجرای موتور جدید است."""

import json
from pathlib import Path

import pandas as pd
import pytest

from backtest.cash_dividends import CashDividend, DividendPayment, replay_cash_dividend_portfolio
from backtest.portfolio import PortfolioConfig, replay_portfolio
from research.dip import DipRule
from research.run import _json_safe

FROZEN = json.loads((Path(__file__).parent/'fixtures/portfolio-v4-compat.json').read_bytes())


@pytest.mark.parametrize('case', FROZEN['cases'], ids=lambda c: c['name'])
def test_default_synthetic_dip_and_cash_paths_equal_the_frozen_prechange_results(case):
    assert FROZEN['source_commit'] == '40b46c04413ea35c775bfbb6a9971a8ae7545c95'
    assert FROZEN['source_portfolio_sha256'] == 'f65b57e843f3310001cc2f6c8abc6dc2434a536daceecf4e180377cb553c6e3b'
    prices, selections = pd.DataFrame(case['prices']), pd.DataFrame(case['selections'])
    config = PortfolioConfig(**case['config'])
    if case['cash_events']:
        result = replay_cash_dividend_portfolio(prices, selections, case['calendar'], config,
            events=[CashDividend(**e) for e in case['cash_events']],
            payments=[DividendPayment(**p) for p in case['cash_payments']])
    else:
        result = replay_portfolio(prices, selections, case['calendar'], config,
            dip_rule=None if case['dip_rule'] is None else DipRule(**case['dip_rule']))
    actual = dict(equity=result.equity.to_dict('records'), executions=result.executions.to_dict('records'),
        decisions=result.decisions.to_dict('records'), metrics=result.metrics, assumptions=result.assumptions)
    assert _json_safe(actual) == case['expected']
