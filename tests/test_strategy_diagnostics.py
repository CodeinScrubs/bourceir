"""Full denominators and overlapping reasons must survive a read-only audit."""

import json
import shutil
from pathlib import Path

import pandas as pd
import pytest

from backtest.evidence import audit_daily_replay
from backtest.portfolio import replay_portfolio
from config import settings
from research.strategy_diagnostics import audit_saved_study, replay_diagnostics
from research.strategy_execution import review_execution_followup, run_execution_followup
from tests.test_portfolio import config, history, picks
from tests.test_strategy_execution import parent as parent


def test_all_unknown_reasons_and_all_blocker_classes_are_preserved():
    prices, days = history(8, ('a',))
    selected = picks(days[:1], ('a',))
    result = replay_portfolio(prices, selected, days, config(max_positions=1, horizon=20))
    audit = audit_daily_replay(result, prices, selected, days)
    audit['blockers'] += ['unknown_nav', 'synthetic_corporate_entitlements']
    rows = [dict(date=days[-1], symbol='a', status=status) for status in (
        'missing_held_mark', 'unknown_entry_execution', 'no_session', 'discontinuous_no_trade_valuation', 'exit_delayed')]
    item = dict(case='fixed', fold=0, scenario='normal', decisions=rows, metrics=result.metrics,
                equity_frame=result.equity)
    diagnostic = replay_diagnostics(item, audit)
    assert len(diagnostic['data_issue_occurrences']) == 4
    assert diagnostic['decision_status_counts']['exit_delayed'] == 1
    assert 'terminal_assets_not_cash' in diagnostic['blockers']
    assert 'synthetic_corporate_entitlements' in diagnostic['blockers']
    assert diagnostic['economic_return'] is None


def test_complete_audit_is_separate_and_relocation_preserves_parent_binding(parent):
    folder, observations, _, _, _ = parent
    execution = run_execution_followup(folder, observations)
    copy = folder.with_name('relocated')
    shutil.copytree(folder, copy)
    assert review_execution_followup(execution['folder'], parent_override=copy)['replays'] == execution['expected_replays']
    before = (Path(execution['folder'])/'manifest.json').read_bytes()
    result = audit_saved_study(execution['folder'], parent_override=copy)
    assert result['replays'] == execution['expected_replays'] and result['invariant_failures'] == 0
    assert result['known_status_changed_from_prior'] == 0
    table = pd.read_parquet(Path(result['folder'])/'diagnostics.parquet')
    assert len(table) == result['replays']
    assert (Path(execution['folder'])/'manifest.json').read_bytes() == before
    assert json.loads((Path(result['folder'])/'state.json').read_bytes())['status'] == 'complete'
    with pytest.raises(ValueError):
        review_execution_followup(execution['folder'], parent_override=settings.DATA_DIR/'private'/'missing')
