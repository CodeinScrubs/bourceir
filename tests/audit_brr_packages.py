"""Reproduce selected supplied-package defects; never imports their providers.

Run explicitly with --root pointing at the privately extracted review packages.
The only executed external modules here were statically reviewed as pure helpers.
All fabricated inputs are confined to this test module and memory.
"""
import argparse
import importlib.util
import json
import sys
import types
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module


def run(root):
    overlay = root / 'BRR_Research_Integration_Overlay_v0.2/BRR_Integration_Overlay_v0.2/brr_integration'
    package('reviewed_overlay', overlay)
    contracts = load('reviewed_overlay.contracts', overlay / 'contracts.py')
    panel = load('reviewed_overlay.panel', overlay / 'panel.py')
    promotion = load('reviewed_overlay.promotion', overlay / 'promotion.py')
    date = '2024-01-02'
    base = pd.DataFrame([dict(date=date, symbol='نمونه', decision_at=date+'T09:00:00+03:30')])
    observation = dict(date=date, symbol='نمونه', decision_at=date+'T11:00:00+03:30',
        available_at=date+'T10:00:00+03:30', source='test', quality_status='OK', module_version='test', metric=7.)
    merged, _ = panel.merge_extension_panel(base, {'test': pd.DataFrame([observation])})
    assert bool(merged.loc[0, 'test__pit_eligible']) and merged.loc[0, 'test__metric'] == 7
    results = [dict(id='overlay_module_overrides_base_decision', reproduced=True,
        base_decision=base.loc[0, 'decision_at'], later_module_time=observation['decision_at'],
        later_availability=observation['available_at'], imported_future_metric=7)]
    naive = {**observation, 'available_at': date+'T05:00:00', 'decision_at': date+'T09:00:00Z'}
    result, _ = contracts.validate_module_frame(pd.DataFrame([naive]), module='test')
    assert bool(result.loc[0, 'pit_eligible'])
    results.append(dict(id='overlay_naive_time_assumed_utc', reproduced=True))
    bad = {**observation, 'metric': float('inf'), 'label_positive_h5': 1.0}
    result, audit = contracts.validate_module_frame(pd.DataFrame([bad]), module='test')
    assert bool(result.loc[0, 'pit_eligible']) and result.loc[0, 'metric'] == float('inf')
    assert 'label_positive_h5' in audit.feature_columns
    results.append(dict(id='overlay_nonfinite_and_label_feature_accepted', reproduced=True))
    evidence = {k: True for k in ['point_in_time_verified','costs_included','execution_modelled',
        'independent_holdout','protocol_preregistered','no_holdout_reuse']}
    evidence.update(point_in_time_violations=.9, data_coverage=float('inf'), oos_sessions=100.9,
        independent_oos_trades=200.9, positive_regimes=2.9, net_excess_return_pct=float('inf'))
    promoted = promotion.assess_promotion_evidence(evidence)
    assert promoted.eligible_for_paper_review and not promoted.live_trading_allowed
    results.append(dict(id='promotion_nonfinite_fractional_evidence_accepted', reproduced=True,
        permits_paper_review=True, permits_live_trading=False))
    cai = root / 'BRR_CrossAsset_Intelligence_v0.1/BRR_CrossAsset_Intelligence_v0.1/brr_cai/core.py'
    cross = load('reviewed_cross', cai)
    quotes = pd.DataFrame([
        dict(timestamp=date+'T08:00:00Z', fetched_at=date+'T08:01:00Z', symbol='FX', price=100., unit='IRR', source='test'),
        dict(timestamp=date+'T09:00:00Z', fetched_at=date+'T11:00:00Z', symbol='FX', price=200., unit='IRR', source='test')])
    result = cross.calculate_window_changes(quotes, date+'T08:30:00Z', date+'T09:30:00Z')
    assert result.loc[0, 'status'] == 'OK' and result.loc[0, 'return_pct'] == 100.
    results.append(dict(id='crossasset_after_cutoff_fetch_accepted', reproduced=True, status='OK', reported_return_pct=100.))
    quotes.loc[1, ['fetched_at', 'unit']] = [date+'T09:01:00Z', 'Toman']
    result = cross.calculate_window_changes(quotes, date+'T08:30:00Z', date+'T09:30:00Z')
    assert result.loc[0, 'status'] == 'OK'
    results.append(dict(id='crossasset_unit_switch_accepted', reproduced=True, status='OK'))
    quality = load('reviewed_quality', root / 'BRR_Khegolpa_Case_Study_Enhancement_v0.1/brr_khegolpa_quality_rules.py')
    rows = pd.DataFrame([{'<DTYYYYMMDD>': '20240102', '<FIRST>':100, '<HIGH>':100, '<LOW>':100,
        '<CLOSE>':100, '<VALUE>':1000, '<VOL>':10, '<OPEN>':100, '<LAST>':100},
        {'<DTYYYYMMDD>': '20240103', '<FIRST>':50, '<HIGH>':50, '<LOW>':50,
        '<CLOSE>':50, '<VALUE>':500, '<VOL>':10, '<OPEN>':50, '<LAST>':50}])
    _, traded = quality.normalize_tsetmc_csv(rows)
    assert traded.loc[1, 'return_pct'] == -50.
    results.append(dict(id='case_return_uses_shifted_close_instead_of_native_reference', reproduced=True,
        reported_return_pct=-50., native_reference_return_pct=0.))
    try:
        quality.normalize_tsetmc_csv(rows.assign(**{'<VOL>':0}))
    except IndexError:
        results.append(dict(id='case_zero_traded_rows_index_error', reproduced=True))
    else:
        raise AssertionError('Expected empty-series index error')
    queue = root / 'BRR_Queue_Alert_Monitor_v0.1/BRR_Queue_Alert_Monitor_v0.1/src/brr_queue_alerts'
    package('reviewed_queue', queue)
    models = load('reviewed_queue.models', queue / 'models.py')
    engine = load('reviewed_queue.engine', queue / 'engine.py')
    obs = models.SymbolObservation(symbol='نمونه', ins_code='test', observed_at=datetime.fromisoformat(date+'T09:30:00+03:30'),
        quote_time=93000, last_price=100, previous_close=100, limit_down=90, limit_up=110,
        volume=10_000_000, trade_count=100, sell_queue_at_limit=None, best_ask_price=100,
        best_ask_volume=10, best_bid_price=99, best_bid_volume=10, bid_depth_5=10, ask_depth_5=10)
    monitor = engine.AlertEngine()
    rule = engine.RuleConfig(symbol='نمونه')
    assert monitor.process(rule, obs) == []
    alerts = monitor.process(rule, replace(obs, observed_at=obs.observed_at+timedelta(seconds=1), volume=3_000_000))
    assert any(a.event_type == 'VOLUME_BURST' for a in alerts)
    results.append(dict(id='queue_counter_reset_causes_false_volume_burst', reproduced=True))
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.root), ensure_ascii=False, indent=2, allow_nan=False))
