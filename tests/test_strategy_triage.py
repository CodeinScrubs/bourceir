"""One diagnosis per unknown replay; observed truncation is not a missing exchange day."""

import copy
import json
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from research.strategy_diagnostics import audit_saved_study
from research.strategy_execution import run_execution_followup
from research.strategy_suite import _sha256
from research.strategy_triage import diagnose, reconcile, run_triage
from tests.test_strategy_execution import parent as parent


def case():
    days = ['2024-01-01', '2024-01-02', '2024-01-03']
    item = dict(case='example', fold=0, scenario='normal',
        equity=[dict(date=days[0], equity_rial=100.), dict(date=days[1], equity_rial=None)],
        decisions=[dict(date=days[0], symbol='', status='allocation_unknown_stale_marks'),
                   dict(date=days[1], symbol='1', status='missing_held_mark')])
    audit = dict(observed_path_model_return=None,
                 blockers=['engine_unknown_or_invalid', 'incomplete_calendar', 'unknown_nav', 'terminal_assets_not_cash'])
    return item, audit, days


def test_every_unknown_gets_a_recorded_primary_stop_and_preserves_secondary_reasons():
    item, audit, days = case()
    row = diagnose(item, audit, days, {'1': 'a'}, {})
    assert row['primary_blocker'] == 'missing_held_mark'
    assert row['first_failure_date'] == days[1] and row['first_failure_symbol'] == 'a'
    assert row['calendar_interpretation'] == 'curve_truncated_after_recorded_engine_stop'
    assert not row['exchange_calendar_completeness_verified']
    assert row['secondary_blockers'] == sorted(audit['blockers'])
    assert len(row['all_recorded_failure_events']) == 2
    assert row['recoverable'] is None and not row['resolution_established']
    assert not row['accepted_for_signals'] and row['economic_return'] is None


@pytest.mark.parametrize('status,flat,expected', [
    ('valid_price', False, 'positive_candle_quarantined_requires_consistent_dataset_generation'),
    ('observed_zero_activity', True, 'flat_publication_present_requires_path_continuity_review'),
    ('observed_zero_activity', False, 'zero_activity_present_but_valuation_not_established'),
    ('unverified_native_response', False, 'bound_request_has_no_usable_evidence')])
def test_previous_request_is_not_mistaken_for_a_successful_resolution(status, flat, expected):
    item, audit, days = case()
    row = diagnose(item, audit, days, {'1': 'a'}, {('1', days[1]): dict(status=status, flat_mark_eligible=flat)})
    assert row['previous_bound_request_found']
    assert row['resolution_status'] == expected
    assert row['recoverable'] is None and not row['resolution_established']


@pytest.mark.parametrize('defect', ['known', 'off_calendar', 'future_failure', 'fatal_before_terminal'])
def test_inconsistent_sources_cannot_receive_a_confident_diagnosis(defect):
    item, audit, days = case()
    if defect == 'known':
        audit['observed_path_model_return'] = 0.
    elif defect == 'off_calendar':
        item['equity'][0]['date'] = days[1]
    elif defect == 'future_failure':
        item['decisions'][-1]['date'] = days[-1]
    else:
        item['equity'].append(dict(date=days[2], equity_rial=None))
    with pytest.raises(ValueError):
        diagnose(item, audit, days, {'1': 'a'}, {})


def test_global_accounting_gap_does_not_turn_into_a_fake_price_request():
    item, audit, days = case()
    item['decisions'][-1]['status'] = 'unknown_corporate_entitlement'
    row = diagnose(item, audit, days, {'1': 'a'}, {})
    assert row['required_source'] == 'Codal_primary_documents_and_corporate_ledger'
    assert row['required_source_url'] is None
    result = reconcile([row], {('example', 0, 'normal')})
    assert result['requestable_requirements'] == []


def test_requirements_deduplicate_sources_without_losing_replay_denominators():
    item, audit, days = case()
    a = diagnose(item, audit, days, {'1': 'a'}, {})
    item['scenario'] = 'stress'
    b = diagnose(item, audit, days, {'1': 'a'}, {})
    keys = {('example', 0, 'normal'), ('example', 0, 'stress')}
    summary = reconcile([a, b], keys)
    assert summary['unknown_replays'] == 2 and summary['requirement_keys'] == 1
    assert summary['requestable_requirements'][0]['affected_replays'] == 2
    assert summary['required_unique_instrument_sessions'] == 1
    assert summary['recoverability_undetermined'] == 2
    assert summary['unknowns_resolved_by_this_diagnostic'] == 0
    for corrupted in ([a], [a, a], [a, b, copy.deepcopy(a)]):
        with pytest.raises(ValueError):
            reconcile(corrupted, keys)


def test_unknown_mark_without_an_instrument_stays_explicitly_undetermined():
    item, audit, days = case()
    item['decisions'] = []
    row = diagnose(item, audit, days, {'1': 'a'}, {})
    assert not row['primary_blocker_is_recorded_engine_stop']
    assert row['first_failure_date'] == days[1] and row['first_failure_ins_code'] is None
    assert row['required_source_url'] is None and row['recoverable'] is None


def test_real_adapter_writes_only_a_new_overlay_and_checks_audit_to_replay_binding(parent):
    folder, observations, _, _, _ = parent
    observations.write_text(json.dumps(dict(kind='priority_missing_session_publications', records=[])), encoding='utf-8')
    execution = Path(run_execution_followup(folder, observations)['folder'])
    audit = Path(audit_saved_study(execution)['folder'])
    before = (execution/'manifest.json').read_bytes()
    result = run_triage(execution, audit)
    table = pd.read_parquet(Path(result['folder'])/'unknown-replays.parquet')
    assert result['unknown_replays'] == len(table) > 0
    assert table.first_failure_ins_code.notna().all() and table.first_failure_date.notna().all()
    assert (execution/'manifest.json').read_bytes() == before
    with zipfile.ZipFile(audit/'audits.zip') as source:
        contents = {n: source.read(n) for n in source.namelist()}
    name = next(iter(contents))
    row = json.loads(contents[name])
    row['source_replay_sha256'] = '0' * 64
    contents[name] = json.dumps(row).encode()
    with zipfile.ZipFile(audit/'audits.zip', 'w') as target:
        for member, body in contents.items():
            target.writestr(member, body)
    manifest = json.loads((audit/'manifest.json').read_bytes())
    manifest['audits.zip'] = _sha256(audit/'audits.zip')
    (audit/'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='bind'):
        run_triage(execution, audit)
