"""Read-only, versioned re-audit of every saved strategy replay.

The original study, its code and its receipts are never amended. No feature,
selection, parameter or financial path is recomputed or optimized here.
"""

import argparse
import hashlib
import json
import uuid
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from backtest.evidence import VERSION as AUDIT_VERSION
from backtest.evidence import audit_daily_replay
from backtest.portfolio import PortfolioResult
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.timeutils import now_tehran_iso
from research.strategy_execution import load_publications, review_execution_followup
from research.strategy_suite import _bytes, _freeze_code, _sha256, _sources_unchanged, _write, review_suite
from research.walkforward import preceding_price_observations

VERSION = 'saved-strategy-diagnostics-v1'
UNKNOWN_TERMS = ('unknown', 'invalid', 'conflicting', 'unaccounted', 'missing_', 'discontinuous')


def replay_diagnostics(item, audit):
    """Preserve all reasons, not just the last engine-stop code/date/reason."""
    statuses = Counter(row['status'] for row in item['decisions'])
    issues = [dict(date=row['date'], symbol=row.get('symbol', ''), reason=row['status'])
              for row in item['decisions'] if row['status'] == 'no_session'
              or any(term in row['status'] for term in UNKNOWN_TERMS)]
    return dict(case=item['case'], fold=item['fold'], scenario=item['scenario'],
        engine_status=item['metrics']['status'],
        marked_model_return=audit['observed_path_model_return'], modeled_cash_return=audit['modeled_cash_return'],
        unknown_model_return=audit['observed_path_model_return'] is None,
        invariant_violations=audit['invariant_violations'], blockers=audit['blockers'],
        decision_status_counts=dict(sorted(statuses.items())), data_issue_occurrences=issues,
        stale_sessions=audit['stale_sessions'], unknown_nav_sessions=audit['unknown_nav_sessions'],
        open_positions=item['metrics']['open_positions'],
        synthetic_corporate_events=item['metrics']['corporate_action_events'],
        noncash_receivable_rial=item['metrics']['noncash_receivable_rial'],
        mean_cash_fraction=(float(item['equity_frame'].cash_rial.div(item['equity_frame'].equity_rial)
            .where(item['equity_frame'].equity_rial.gt(0)).mean())
            if item['equity_frame'].equity_rial.gt(0).any() else None),
        economic_return=None, accepted_for_signals=False)


def audit_saved_study(study, *, root=None, parent_override=None, progress=None):
    study = Path(study).resolve()
    private = (settings.DATA_DIR / 'private').resolve()
    if not study.is_relative_to(private):
        raise ValueError('An explicitly selected private study is required')
    protocol = json.loads((study/'protocol.json').read_bytes())
    native = protocol['version'] == 'saved-decisions-native-execution-v1'
    if native:
        review_execution_followup(study, parent_override=parent_override)
        parent = Path(parent_override if parent_override is not None else protocol['parent']).resolve()
    else:
        if parent_override is not None:
            raise ValueError('Only a native follow-up has a parent study')
        review_suite(study)
        parent = study
    parent_protocol = json.loads((parent/'protocol.json').read_bytes())
    source_result = json.loads((study/'results.json').read_bytes())
    base = Path(root or settings.DATA_DIR/'private'/'strategy-diagnostics').resolve()
    if not base.is_relative_to(private):
        raise ValueError('Diagnostics must remain private')
    folder = base / f'AUDIT-{now_tehran_iso()[:10]}-{uuid.uuid4().hex}'
    folder.mkdir(parents=True, exist_ok=False)
    source_files = _freeze_code(folder/'code.zip')
    registration = dict(version=VERSION, auditor=AUDIT_VERSION, created_at=now_tehran_iso(),
        source_study=str(study), source_manifest_sha256=_sha256(study/'manifest.json'),
        parent_manifest_sha256=_sha256(parent/'manifest.json'), source_files=source_files,
        financial_paths_recomputed=False, original_decisions_changed=False, original_artifacts_modified=False,
        expected_replays=source_result['expected_replays'], holdout_opened=False)
    _write(folder/'protocol.json', registration)
    _write(folder/'state.json', dict(status='running', completed_replays=0))
    try:
        return _evaluate_registered(study, parent, parent_protocol, source_result, native, folder, registration, progress)
    except BaseException as error:
        _write(folder/'state.json', dict(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
            error=f'{type(error).__name__}: {error}', finished_at=now_tehran_iso()))
        raise


def _evaluate_registered(study, parent, parent_protocol, source_result, native, folder, registration, progress):
    source_files = registration['source_files']
    prices = pd.read_parquet(parent/'prices.parquet')
    registry = pd.read_parquet(parent/'registry.parquet')
    calendar = json.loads((parent/'calendar.json').read_bytes())
    identities = dict(zip(registry.symbol_fa, registry.ins_code, strict=True))
    marks = []
    if native:
        _, marks, _ = load_publications((study/'publications.json').read_bytes(), registry, calendar,
                                        parent_protocol['plan']['end_exclusive'])
        prices = prices.assign(symbol=prices.symbol.map(identities))
    diagnostic_rows, issues, reason_replays = [], {}, defaultdict(set)
    source_index = {r['member']: r for r in source_result['records']}
    by_fold = defaultdict(list)
    for record in source_result['records']:
        by_fold[record['fold']].append(record)
    with zipfile.ZipFile(study/'replays.zip') as saved, zipfile.ZipFile(
            folder/'audits.zip', 'x', compression=zipfile.ZIP_DEFLATED) as audits:
        for index, fold in enumerate(parent_protocol['folds']):
            days = [d for d in calendar if fold['test_start'] <= d < fold['test_end_exclusive']]
            raw = pd.concat([preceding_price_observations(prices, fold['test_start']), prices.loc[
                prices.date.ge(fold['test_start']) & prices.date.lt(fold['test_end_exclusive'])]], ignore_index=True)
            prepared = prepare_portfolio_prices(raw)
            proofs = [o for o in marks if o['requested_date'] in set(days)]
            for record in by_fold[index]:
                body = saved.read(record['member'])
                if hashlib.sha256(body).hexdigest() != record['sha256']:
                    raise ValueError('Saved replay changed after source validation')
                item = json.loads(body)
                curve = pd.DataFrame(item['equity'])
                result = PortfolioResult(curve, pd.DataFrame(item['executions']), pd.DataFrame(item['decisions']),
                                         item['metrics'], item['assumptions'])
                selected = pd.DataFrame(item['picks'], columns=['date', 'symbol', 'rank'])
                audit = audit_daily_replay(result, prepared, selected, days,
                    no_trade_valuation_observations=proofs if native else None)
                item['equity_frame'] = curve
                row = replay_diagnostics(item, audit)
                for reason in set(row['blockers']):
                    reason_replays[reason].add(record['member'])
                for issue in row['data_issue_occurrences']:
                    key = issue['symbol'], issue['date'], issue['reason']
                    own = issues.setdefault(key, dict(**issue, affected_replays=set(), occurrences=0))
                    own['affected_replays'].add(record['member'])
                    own['occurrences'] += 1
                audits.writestr(record['member'], _bytes(dict(source_replay_sha256=record['sha256'], audit=audit,
                                                             diagnostics=row)))
                # Nested lists/maps are explicit JSON columns in the flat table.
                diagnostic_rows.append({key: _bytes(value).decode() if isinstance(value, (list, dict)) else value
                                        for key, value in row.items()})
                if len(diagnostic_rows) % 100 == 0:
                    state = dict(status='running', completed_replays=len(diagnostic_rows))
                    _write(folder/'state.json', state)
                    if progress:
                        progress(state)
    table = pd.DataFrame(diagnostic_rows)
    if len(table) != source_result['expected_replays'] or len(source_index) != len(table):
        raise ValueError('Full saved-replay denominator was not audited')
    table.to_parquet(folder/'diagnostics.parquet', index=False)
    requests = [dict(symbol=v['symbol'], date=v['date'], reason=v['reason'], occurrences=v['occurrences'],
                     affected_replays=len(v['affected_replays'])) for _, v in sorted(issues.items())]
    _write(folder/'data-issues.json', requests)
    previous = source_result.get('residual_requests', [])
    summary = dict(version=VERSION, replays=len(table),
        invariant_failures=int(table.invariant_violations.ne('[]').sum()),
        known_model_returns=int((~table.unknown_model_return).sum()),
        unknown_model_returns=int(table.unknown_model_return.sum()),
        reason_affected_replays={k: len(v) for k, v in sorted(reason_replays.items())},
        reasons_are_overlapping_not_mutually_exclusive=True,
        data_issue_code_date_reasons=len(requests),
        data_issue_unique_symbol_dates=len({(r['symbol'], r['date']) for r in requests}),
        previous_terminal_stop_requests=len(previous),
        previous_terminal_unique_symbol_dates=len({(r['ins_code'], r['date']) for r in previous}),
        previous_terminal_requests_are_not_all_blockers=True,
        order_policy=dict(entry_time_in_force='one next-session attempt; remainder expires (DAY proxy)',
            exit_time_in_force='retry due/target disposal until filled or evidence becomes unknown',
            cash='sales credited after purchases; reusable next session; not a legal settlement ledger',
            actual_requested_filled_remaining_order_tape_available=False),
        economic_profit_verified=False, accepted_for_signals=False)
    # Compare by key, not incidental archive/loop order.
    prior_known = {(r['case'], r['fold'], r['scenario']): r['audit']['observed_path_model_return'] is not None
                   for r in source_result['records']}
    summary['known_status_changed_from_prior'] = sum(prior_known[r.case, r.fold, r.scenario] != (not r.unknown_model_return)
                                                    for r in table.itertuples())
    if not _sources_unchanged(source_files):
        raise RuntimeError('Audit source changed during the run')
    if (_sha256(study/'manifest.json') != registration['source_manifest_sha256']
            or _sha256(parent/'manifest.json') != registration['parent_manifest_sha256']):
        raise RuntimeError('Saved study identity changed during the audit')
    _write(folder/'summary.json', summary)
    _write(folder/'state.json', dict(status='complete' if not summary['invariant_failures'] else 'failed',
                                   completed_replays=len(table)))
    _write(folder/'manifest.json', {p.name: _sha256(p) for p in folder.iterdir() if p.is_file()})
    return dict(folder=str(folder), **summary)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study', required=True)
    parser.add_argument('--parent')
    parser.add_argument('--output-root')
    args = parser.parse_args()
    result = audit_saved_study(args.study, root=args.output_root, parent_override=args.parent,
        progress=lambda state: print(json.dumps(state), flush=True))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result['invariant_failures']))


if __name__ == '__main__':
    raise SystemExit(main())
