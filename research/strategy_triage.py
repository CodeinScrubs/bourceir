"""Account for every unknown saved replay, without inventing recoverability.

This is a diagnostic overlay. It neither changes a decision nor fills a missing
market observation, and it never ranks strategies by their known-only results.
"""

import argparse
import hashlib
import json
import uuid
import zipfile
from collections import Counter
from pathlib import Path

import pandas as pd

from config import settings
from core.daily_publications import publication_request
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from ops.validation_run import write_state
from research.strategy_diagnostics import UNKNOWN_TERMS
from research.strategy_execution import load_publications, review_execution_followup
from research.strategy_outcomes import outcome_status
from research.strategy_suite import _bytes, _freeze_code, _sha256, _sources_unchanged

VERSION = 'unknown-replay-triage-v2'
EVIDENCE = {
    'missing_held_mark': 'dated_candle_or_continuous_official_no_trade_mark',
    'no_session': 'dated_executable_candle_or_confirmed_zero_activity',
    'missing_exit_bar': 'dated_executable_candle_or_continuous_official_no_trade_mark',
    'unknown_entry_execution': 'coherent_raw_entry_ohlcv_and_reference',
    'unknown_exit_execution': 'coherent_raw_exit_ohlcv_and_reference',
    'unknown_open_position': 'official_mark_and_reference_predecessor',
    'unknown_time_exit': 'observed_last_trade_inside_session_range',
    'unknown_corporate_entitlement': 'explicit_dated_entitlement_and_tradability_ledger',
    'unaccounted_reference_change': 'explicit_dated_entitlement_and_tradability_ledger',
    'discontinuous_no_trade_valuation': 'continuous_official_marks_or_explicit_corporate_event',
    'conflicting_valuation_predecessor': 'reconciled_predecessor_and_source_vintages',
    'conflicting_no_trade_valuation': 'reconciled_candle_and_no_trade_publications',
    'conflicting_no_trade_entry': 'reconciled_candle_and_no_trade_publications',
    'invalid_decision': 'known_point_in_time_decision_inputs',
    'unknown_credited_share_mark': 'explicit_dated_entitlement_and_tradability_ledger',
}


def is_failure(status):
    return status == 'no_session' or any(term in status for term in UNKNOWN_TERMS)


def diagnose(item, audit, sessions, identities, publications):
    """Recorded stop is causal for truncation; an exchange-calendar gap is not inferred."""
    if audit['observed_path_model_return'] is not None:
        raise ValueError('Only an unknown replay belongs in the unknown ledger')
    key = item['case'], item['fold'], item['scenario']
    uid = hashlib.sha256(_bytes(key)).hexdigest()
    actual = [row['date'] for row in item['equity']]
    if not actual or actual != sessions[:len(actual)]:
        raise ValueError('Saved curve must be an exact prefix of its registered fold')
    failures = [row for row in item['decisions'] if is_failure(row['status'])]
    fatal = [row for row in failures if row['status'] in EVIDENCE]
    first = fatal[0] if fatal else None
    if first is not None and (first['date'] not in actual or not is_canonical_iso_date(first['date'])):
        raise ValueError('Failure date must belong to the recorded path')
    if first is not None and first['date'] != actual[-1]:
        raise ValueError('A recorded fatal stop cannot precede the last recorded session')
    cause = first['status'] if first else 'unresolved_valuation_or_audit_blocker'
    code = first.get('symbol') if first else None
    if code not in identities:
        code = None
    day = first['date'] if first else next((row['date'] for row in item['equity'] if
        row.get('equity_rial') is None or row.get('stale_positions', 0) or row.get('stale_fractional_exposures', 0)), None)
    evidence = EVIDENCE.get(cause, 'investigate_recorded_source_or_accounting_contract')
    native = publications.get((code, day)) if code is not None and day is not None else None
    if native is None:
        resolution = 'no_request_in_bound_publication_generation'
    elif native['status'] == 'valid_price':
        resolution = 'positive_candle_quarantined_requires_consistent_dataset_generation'
    elif native.get('flat_mark_eligible'):
        resolution = 'flat_publication_present_requires_path_continuity_review'
    elif native['status'] == 'observed_zero_activity':
        resolution = 'zero_activity_present_but_valuation_not_established'
    else:
        resolution = 'bound_request_has_no_usable_evidence'
    corporate = evidence == 'explicit_dated_entitlement_and_tradability_ledger'
    url = publication_request(code, day, 'price')[1] if code and day and not corporate else None
    return dict(replay_id=uid, configuration_id=key[0], fold=key[1], scenario=key[2],
        first_failure_date=day, first_failure_ins_code=code,
        first_failure_symbol=identities.get(code), primary_blocker=cause,
        primary_blocker_is_recorded_engine_stop=first is not None,
        secondary_blockers=sorted(set(audit['blockers'])),
        all_recorded_failure_events=failures, missing_evidence=evidence,
        required_source=('Codal_primary_documents_and_corporate_ledger' if corporate else
                         'TSETMC_dated_price_publication' if url else 'manual_contract_or_accounting_investigation'),
        required_source_url=url, recoverable=None, recoverability='not_established',
        previous_bound_request_found=native is not None,
        request_history_scope='original_EXEC_publications_only',
        previous_publication_status=native['status'] if native else None,
        resolution_status=resolution, resolution_established=False,
        curve_truncated=len(actual) < len(sessions),
        calendar_interpretation=('curve_truncated_after_recorded_engine_stop' if first and len(actual) < len(sessions)
                                 else 'no_exchange_calendar_defect_inferred'),
        exchange_calendar_completeness_verified=False,
        recorded_sessions=len(actual), expected_sessions=len(sessions),
        original_raw_amendment_allowed=False, economic_return=None, accepted_for_signals=False)


def reconcile(rows, expected_keys):
    actual = [(row['configuration_id'], row['fold'], row['scenario']) for row in rows]
    if len(actual) != len(set(actual)) or set(actual) != set(expected_keys):
        raise ValueError('Unknown ledger is duplicated, missing, or has unexpected replay keys')
    requirements = {}
    for row in rows:
        if not row['required_source_url']:
            continue
        key = row['first_failure_ins_code'], row['first_failure_date'], row['missing_evidence']
        item = requirements.setdefault(key, dict(ins_code=key[0], date=key[1], missing_evidence=key[2],
            symbol=row['first_failure_symbol'], required_source_url=row['required_source_url'],
            previous_bound_request_found=row['previous_bound_request_found'], affected_replays=0))
        item['affected_replays'] += 1
    return dict(unknown_replays=len(rows), unique_diagnostic_records=len(actual),
        primary_blocker_counts=dict(sorted(Counter(row['primary_blocker'] for row in rows).items())),
        resolution_counts=dict(sorted(Counter(row['resolution_status'] for row in rows).items())),
        identified_first_failure_dates=sum(row['first_failure_date'] is not None for row in rows),
        identified_instruments=sum(row['first_failure_ins_code'] is not None for row in rows),
        confirmed_recoverable=0, recoverability_undetermined=len(rows),
        requirement_keys=len(requirements),
        required_unique_instrument_sessions=len({(k[0], k[1]) for k in requirements}),
        requestable_requirements=list(requirements.values()),
        unknowns_resolved_by_this_diagnostic=0, independent_calendar_defect_claimed=False,
        economic_profit_verified=False, model_search_gate_passed=False)


def run_triage(study, audit_folder, *, root=None):
    private = (settings.DATA_DIR/'private').resolve()
    study, audit_folder = Path(study).resolve(), Path(audit_folder).resolve()
    if not study.is_relative_to(private) or not audit_folder.is_relative_to(private):
        raise ValueError('Explicit private saved study and audit are required')
    review_execution_followup(study)
    manifest = json.loads((audit_folder/'manifest.json').read_bytes())
    if (set(manifest) != {p.name for p in audit_folder.iterdir() if p.is_file() and p.name != 'manifest.json'}
            or any(Path(n).name != n or _sha256(audit_folder/n) != digest for n, digest in manifest.items())):
        raise ValueError('Audit evidence manifest mismatch')
    audit_protocol = json.loads((audit_folder/'protocol.json').read_bytes())
    if (audit_protocol['source_manifest_sha256'] != _sha256(study/'manifest.json')
            or json.loads((audit_folder/'state.json').read_bytes())['status'] != 'complete'):
        raise ValueError('Audit is incomplete or belongs to another study')
    protocol = json.loads((study/'protocol.json').read_bytes())
    parent = Path(protocol['parent']).resolve()
    parent_protocol = json.loads((parent/'protocol.json').read_bytes())
    calendar = json.loads((parent/'calendar.json').read_bytes())
    registry = pd.read_parquet(parent/'registry.parquet')
    identities = dict(zip(registry.ins_code, registry.symbol_fa, strict=True))
    _, _, observed = load_publications((study/'publications.json').read_bytes(), registry, calendar,
                                       parent_protocol['plan']['end_exclusive'])
    publications = {(row['ins_code'], row['date']): row for row in observed}
    result = json.loads((study/'results.json').read_bytes())
    rows, expected, benchmarks, outcomes = [], set(), [], []
    with zipfile.ZipFile(study/'replays.zip') as saved, zipfile.ZipFile(audit_folder/'audits.zip') as audits:
        if set(audits.namelist()) != {record['member'] for record in result['records']}:
            raise ValueError('Audit replay denominator mismatch')
        for record in result['records']:
            body = saved.read(record['member'])
            checked = json.loads(audits.read(record['member']))
            if (hashlib.sha256(body).hexdigest() != record['sha256']
                    or checked['source_replay_sha256'] != record['sha256']):
                raise ValueError('Audit does not bind to the saved financial replay')
            item = json.loads(body)
            key = item['case'], item['fold'], item['scenario']
            if key != (record['case'], record['fold'], record['scenario']):
                raise ValueError('Saved replay key differs from its registered key')
            own = checked['audit']
            if own['invariant_violations']:
                raise ValueError('An invariant failure needs investigation before data triage')
            fold = parent_protocol['folds'][item['fold']]
            days = [day for day in calendar if fold['test_start'] <= day < fold['test_end_exclusive']]
            status = outcome_status(item, own, days)
            outcomes.append(dict(configuration_id=key[0], fold=key[1], scenario=key[2], **status))
            if item['case'] in {'cash', 'buy_hold'} or item['case'].startswith('control:liquidity:'):
                benchmarks.append(dict(case=item['case'], fold=item['fold'], scenario=item['scenario'],
                    known=own['observed_path_model_return'] is not None,
                    model_return=own['observed_path_model_return'], economic_return=None))
            if own['observed_path_model_return'] is not None:
                continue
            expected.add(key)
            rows.append(dict(**diagnose(item, own, days, identities, publications), **{
                k: v for k, v in status.items() if k not in {'economic_return', 'accepted_for_signals'}}))
    summary = reconcile(rows, expected)
    if len(result['records']) != result['expected_replays'] or len(rows) != sum(
            r['audit']['observed_path_model_return'] is None for r in result['records']):
        raise ValueError('Old/new known status or full replay denominator changed')
    base = Path(root or private/'strategy-triage').resolve()
    if not base.is_relative_to(private):
        raise ValueError('Triage output must remain private')
    folder = base/f'TRIAGE-{now_tehran_iso()[:10]}-{uuid.uuid4().hex}'
    folder.mkdir(parents=True, exist_ok=False)
    files = _freeze_code(folder/'code.zip')
    registration = dict(version=VERSION, created_at=now_tehran_iso(),
        source_study=str(study), source_audit=str(audit_folder),
        source_manifest_sha256=_sha256(study/'manifest.json'),
        audit_manifest_sha256=_sha256(audit_folder/'manifest.json'), source_files=files,
        raw_prices_modified=False, financial_paths_recomputed=False, holdout_opened=False)
    write_state(folder/'protocol.json', registration)
    frame = pd.DataFrame([{key: _bytes(value).decode() if isinstance(value, (list, dict)) else value
                           for key, value in row.items()} for row in rows])
    frame.to_parquet(folder/'unknown-replays.parquet', index=False)
    pd.DataFrame(outcomes).to_parquet(folder/'outcomes.parquet', index=False)
    write_state(folder/'outcome-summary.json', dict(replays=len(outcomes),
        model_nav_known=sum(r['model_nav_known'] for r in outcomes),
        modeled_terminal_cash_known=sum(r['modeled_terminal_cash_known'] for r in outcomes),
        economic_accounting_verified=0, actual_execution_verified=0,
        stopped_before_planned_endpoint=sum(not r['planned_endpoint_reached'] for r in outcomes),
        recorded_non_cash_assets_before_endpoint=sum(r['recorded_assets_not_cash'] and
            not r['planned_endpoint_reached'] for r in outcomes),
        recorded_non_cash_assets_at_endpoint=sum(r['recorded_assets_not_cash'] and
            r['planned_endpoint_reached'] for r in outcomes)))
    write_state(folder/'summary.json', {k: v for k, v in summary.items() if k != 'requestable_requirements'})
    write_state(folder/'requirements.json', dict(kind='unknown_replay_requirements',
        requests=summary['requestable_requirements'], source_manifest_sha256=registration['source_manifest_sha256']))
    write_state(folder/'benchmark-coverage.json', dict(rows=benchmarks,
        static_top5='not_registered_in_original_suite',
        indices='separate_reference_series_not_execution_matched_portfolios',
        full_universe_and_top5_are_not_equal_concentration=True,
        strategy_outperformance_verified=False))
    if not _sources_unchanged(files):
        raise RuntimeError('Triage source changed during output generation')
    write_state(folder/'state.json', dict(status='complete', unknowns_diagnosed=len(rows), unknowns_resolved=0))
    write_state(folder/'manifest.json', {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()})
    return dict(folder=str(folder), **{k: v for k, v in summary.items() if k != 'requestable_requirements'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--audit', required=True)
    parser.add_argument('--output-root')
    args = parser.parse_args()
    print(json.dumps(run_triage(args.study, args.audit, root=args.output_root), ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()
