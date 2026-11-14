"""Frozen initial selection, NAV without forced sale, and finite liquidation periods."""

import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from research.strategy_benchmarks import CASES, EXTENSIONS, benchmark_picks, review_benchmarks, run_benchmarks
from research.strategy_suite import SuitePlan, run_suite
from tests.test_strategy_suite import inputs


def test_static_top5_uses_original_initial_rank_and_preserves_native_identities():
    initial = pd.DataFrame([dict(date='2024-01-01',symbol=str(i),rank=6-i) for i in range(7)])
    rotation = pd.DataFrame([dict(date='2024-01-02',symbol='0',rank=0)])
    tapes = benchmark_picks(initial,rotation,{str(i):str(100+i) for i in range(7)})
    assert list(tapes['initial_liquidity_top5'].symbol) == ['106','105','104','103','102']
    assert tapes['initial_liquidity_top5'].date.eq('2024-01-01').all()
    assert len(tapes['initial_eligible_equal_weight']) == 7
    assert list(initial.symbol) == [str(i) for i in range(7)]


@pytest.fixture
def benchmark_source(tmp_path,monkeypatch,db):
    import research.labels as labels
    import research.strategy_suite as suite

    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    monkeypatch.setattr(labels,'CALENDAR_MIN_SYMBOLS',2)
    prices,clients,registry,days = inputs(160,('a','b','c'))
    monkeypatch.setattr(suite,'snapshot_inputs',lambda *_:(prices,clients,registry))
    result = run_suite(db,plan=SuitePlan(start=days[0],end_exclusive='2024-07-01',
        initial_capital_rial=10000,min_daily_value_rial=0,min_train_sessions=60,test_sessions=30),
        case_names=['buy_hold','control:liquidity:h1'])
    folder = Path(result['folder'])
    observations = tmp_path/'data/private/empty-publications.json'
    observations.write_text(json.dumps(dict(kind='priority_missing_session_publications',records=[])),encoding='utf-8')
    return folder,observations


def test_benchmark_registration_keeps_every_objective_and_does_not_convert_nav_to_cash(benchmark_source):
    parent,publications = benchmark_source
    before = (parent/'manifest.json').read_bytes()
    result = run_benchmarks(parent,publications)
    assert len(result['records']) == result['expected_replays']
    assert not result['invariant_failures']
    assert (parent/'manifest.json').read_bytes() == before
    folder = Path(result['folder'])
    assert review_benchmarks(folder,reexecute=True)['financial_reexecution_performed']
    with zipfile.ZipFile(folder/'replays.zip') as archive:
        for record in result['records']:
            row = json.loads(archive.read(record['member']))
            assert record['case'] in CASES and record['liquidation_sessions'] in EXTENSIONS
            assert not row['economic_accounting_verified'] and not row['actual_execution_verified']
            if not row['financial_path_computed']:
                assert row['unresolved_reason'] == 'insufficient_declared_calendar_for_liquidation_window'
                assert row['model_nav_return'] is None and not row['modeled_terminal_cash_known']
                continue
            assert row['audit']['invariant_violations'] == []
            if record['case'] != 'liquidity_rotation_h1':
                sales = [v for v in row['executions'] if v['side']=='sell']
                assert all(v['date'] > row['base_endpoint_date'] for v in sales)
                if record['liquidation_sessions']==0:
                    assert row['model_nav_known'] and not row['modeled_terminal_cash_known']
                    assert row['recorded_open_positions'] > 0 and not sales
            cashflows = row['per_instrument_trading_cashflows']
            assert all(not v['is_total_return_attribution'] for v in cashflows)


def test_missing_required_frozen_tape_fails_before_creating_benchmark(benchmark_source):
    parent,publications = benchmark_source
    # Tampering is rejected by the source manifest, before any new registration.
    with (parent/'results.json').open('a',encoding='utf-8') as stream:
        stream.write(' ')
    with pytest.raises(ValueError):
        run_benchmarks(parent,publications)
