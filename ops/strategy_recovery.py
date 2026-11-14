"""Bounded, immutable public-source recovery for an explicitly bound replay triage."""

import argparse
import base64
import hashlib
import json
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

from config import settings
from core.daily_publications import parse_publication, publication_request
from core.no_trade_valuation import parse_no_trade_publication
from core.timeutils import now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from ops.validation_run import write_state
from research.strategy_execution import MAX_INPUT_BYTES, load_publications, review_execution_followup
from research.strategy_suite import _freeze_code, _sha256, _sources_unchanged

VERSION = 'bounded-strategy-recovery-v1'


@dataclass(frozen=True)
class RecoveryBudget:
    max_requests: int = 256
    max_seconds: float = 600.

    def __post_init__(self):
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 1024:
            raise ValueError('A request budget between 1 and 1024 is required')
        if type(self.max_seconds) not in (int, float) or not 0 < self.max_seconds <= 900:
            raise ValueError('A finite time budget up to 900 seconds is required')


def check_manifest(folder):
    manifest = json.loads((folder/'manifest.json').read_bytes())
    if (set(manifest) != {p.name for p in folder.iterdir() if p.is_file() and p.name != 'manifest.json'}
            or any(Path(n).name != n or _sha256(folder/n) != h for n,h in manifest.items())):
        raise ValueError('Evidence manifest mismatch')


def private_path(path):
    path = Path(path).resolve()
    if not path.is_relative_to((settings.DATA_DIR/'private').resolve()):
        raise ValueError('Explicit private evidence paths are required')
    return path


def recovery_requests(study, triage):
    study, triage = private_path(study), private_path(triage)
    review_execution_followup(study)
    check_manifest(triage)
    protocol = json.loads((triage/'protocol.json').read_bytes())
    needs = json.loads((triage/'requirements.json').read_bytes())
    if (protocol['source_manifest_sha256'] != _sha256(study/'manifest.json')
            or needs['source_manifest_sha256'] != protocol['source_manifest_sha256']
            or json.loads((triage/'state.json').read_bytes())['status'] != 'complete'):
        raise ValueError('Triage does not bind to the complete source study')
    parent = Path(json.loads((study/'protocol.json').read_bytes())['parent'])
    registry = pd.read_parquet(parent/'registry.parquet')
    identities = dict(zip(registry.ins_code, registry.symbol_fa, strict=True))
    calendar = set(json.loads((parent/'calendar.json').read_bytes()))
    requests = {}
    for row in needs['requests']:
        code, day = row['ins_code'], row['date']
        if (identities.get(code) != row['symbol'] or day not in calendar
                or day >= settings.RESEARCH_CONFIG['holdout_start']
                or publication_request(code, day, 'price')[1] != row['required_source_url']):
            raise ValueError('A recovery request is outside its native identity/date/source contract')
        key = code, day
        own = requests.setdefault(key, dict(ins_code=code, date=day, symbol=row['symbol'], affected_replays=0))
        own['affected_replays'] += row['affected_replays']
    return sorted(requests.values(), key=lambda r: (-r['affected_replays'], r['date'], r['ins_code']))


def collect_recovery(study, triage, *, root=None, budget=None, provider=None, progress=None):
    study, triage = private_path(study), private_path(triage)
    requests = recovery_requests(study, triage)
    budget = budget or RecoveryBudget()
    if not isinstance(budget, RecoveryBudget):
        raise ValueError('An explicit bounded recovery policy is required')
    base = private_path(root or settings.DATA_DIR/'private/strategy-recovery')
    folder = base/f'RECOVERY-{now_tehran_iso()[:10]}-{uuid.uuid4().hex}'
    folder.mkdir(parents=True, exist_ok=False)
    sources = _freeze_code(folder/'code.zip')
    protocol = dict(version=VERSION, created_at=now_tehran_iso(), budget=asdict(budget), targets=requests,
        source_study_manifest_sha256=_sha256(study/'manifest.json'),
        source_triage_manifest_sha256=_sha256(triage/'manifest.json'), source_files=sources,
        original_raw_modified=False, feature_or_selection_retraining=False, holdout_opened=False,
        first_publication_time_verified=False, stop_on_source_failure=True)
    write_state(folder/'protocol.json', protocol)
    records, stopped, error = [], None, None
    started = time.monotonic()
    own_provider = provider is None
    provider = provider or TsetmcProvider(realtime=True)
    try:
        for request in requests:
            if len(records) >= budget.max_requests or time.monotonic()-started >= budget.max_seconds:
                stopped = 'bounded_collection_budget_exhausted'
                break
            record = dict(request=request, attempted_at=now_tehran_iso(), observation=None,
                          status='source_unavailable')
            observation = provider.get_daily_publication_observation(request['ins_code'], request['date'], 'price')
            if observation is None:
                record['native_body_exposed_by_provider'] = False
                record['transport'] = {k:v for k,v in (provider.last_transport_evidence or {}).items()
                    if k in {'status','reason','url','requested_at','received_at','http_status',
                             'request_sent','request_admitted','not_before'}}
                stopped = 'source_unavailable_or_deferred'
            else:
                record['observation'] = {k:v for k,v in observation.items() if k != 'content'}
                record['observation']['content_base64'] = base64.b64encode(observation['content']).decode('ascii')
                try:
                    if observation.get('http_status') != 200:
                        raise ValueError('A complete HTTP 200 native response is required')
                    typed = parse_publication(observation, request['ins_code'], request['date'], 'price')
                    flat = parse_no_trade_publication(observation, request['ins_code'], request['date'])
                    record.update(status=typed['status'], flat_mark_eligible=flat['flat_mark_eligible'],
                                  positive_candle_quarantined=typed['status'] == 'valid_price')
                except ValueError as exc:
                    record.update(status='unverified_native_response', error=str(exc))
                    stopped = 'unverified_native_response'
            records.append(record)
            write_state(folder/'publications.json', dict(kind='priority_missing_session_publications', records=records))
            write_state(folder/'state.json', dict(status='collecting' if stopped is None else 'stopped',
                attempted=len(records), expected=len(requests), stopped_reason=stopped))
            if progress and (stopped or len(records) % 20 == 0):
                progress(dict(attempted=len(records), expected=len(requests), stopped_reason=stopped))
            if stopped:
                break
    except BaseException as exc:
        error = f'{type(exc).__name__}: {exc}'
        stopped = 'collector_exception'
        raise
    finally:
        if own_provider:
            provider.close()
        write_state(folder/'publications.json', dict(kind='priority_missing_session_publications', records=records))
        unchanged = _sources_unchanged(sources)
        summary = dict(version=VERSION, folder=str(folder), expected=len(requests), attempted=len(records),
            unattempted=len(requests)-len(records), stopped_reason=stopped, error=error,
            complete_received=len(records) == len(requests) and stopped is None and unchanged,
            statuses=dict(Counter(r['status'] for r in records)),
            flat_marks=sum(r.get('flat_mark_eligible', False) for r in records),
            source_unchanged=unchanged, elapsed_seconds=round(time.monotonic()-started, 2),
            actual_unknowns_resolved=0, financial_replay_performed=False, economic_profit_verified=False)
        write_state(folder/'summary.json', summary)
        write_state(folder/'state.json', dict(status='complete_collection' if summary['complete_received'] else
            'failed' if error or not unchanged else 'stopped', **summary))
        write_state(folder/'manifest.json', {p.name:_sha256(p) for p in folder.iterdir() if p.is_file()})
    return summary


def merge_recovery(study, recovery, output):
    """Never silently replace an existing native version, even if a fresh one looks better."""
    study, recovery, output = private_path(study), private_path(recovery), private_path(output)
    review_execution_followup(study)
    check_manifest(recovery)
    protocol = json.loads((recovery/'protocol.json').read_bytes())
    if protocol['source_study_manifest_sha256'] != _sha256(study/'manifest.json'):
        raise ValueError('Recovery belongs to another source study')
    if output.exists():
        raise ValueError('Merged generations cannot overwrite an existing file')
    base_bytes = (study/'publications.json').read_bytes()
    fresh_bytes = (recovery/'publications.json').read_bytes()
    if max(len(base_bytes),len(fresh_bytes)) > MAX_INPUT_BYTES:
        raise ValueError('Publication input exceeds the size bound')
    parent = Path(json.loads((study/'protocol.json').read_bytes())['parent'])
    args = (pd.read_parquet(parent/'registry.parquet'), json.loads((parent/'calendar.json').read_bytes()),
            json.loads((parent/'protocol.json').read_bytes())['plan']['end_exclusive'])
    load_publications(base_bytes, *args)
    _, _, fresh_coverage = load_publications(fresh_bytes, *args)
    fresh_statuses = {(r['ins_code'],r['date']):r['status'] for r in fresh_coverage}
    key = lambda r: (r['request']['ins_code'],r['request']['date'])
    records = {key(r):r for r in json.loads(base_bytes)['records']}
    overlaps = []
    for row in json.loads(fresh_bytes)['records']:
        k = key(row)
        if k in records:
            original = records[k]
            replacement = (original.get('observation') is None and row.get('observation') is not None
                and fresh_statuses[k] in {'valid_price','observed_zero_activity'})
            overlaps.append(dict(ins_code=k[0], date=k[1],
                same_native_body=(original.get('observation') or {}).get('body_sha256') ==
                    (row.get('observation') or {}).get('body_sha256'),
                policy='replace_absent_observation_only' if replacement else 'keep_original_native_version'))
            if replacement:
                records[k] = row
        else:
            records[k] = row
    payload = dict(kind='priority_missing_session_publications', records=list(records.values()), overlaps=overlaps,
        parent_publications_sha256=hashlib.sha256(base_bytes).hexdigest(),
        recovery_publications_sha256=hashlib.sha256(fresh_bytes).hexdigest(),
        recovery_manifest_sha256=_sha256(recovery/'manifest.json'),
        original_raw_modified=False, feature_or_selection_retraining=False)
    contents = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    _, _, coverage = load_publications(contents, *args)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(contents)
    return dict(path=str(output), unique_publications=len(records), overlaps=overlaps,
        publication_statuses=dict(Counter(r['status'] for r in coverage)),
        raw_amendments=False, original_artifacts_modified=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--study',required=True)
    parser.add_argument('--triage',required=True)
    parser.add_argument('--output-root')
    parser.add_argument('--max-requests',type=int,default=256)
    parser.add_argument('--max-seconds',type=float,default=600.)
    parser.add_argument('--merge-output')
    args = parser.parse_args()
    result = collect_recovery(args.study,args.triage,root=args.output_root,
        budget=RecoveryBudget(args.max_requests,args.max_seconds),
        progress=lambda row:print(json.dumps(row),flush=True))
    if args.merge_output:
        result['merge'] = merge_recovery(args.study,result['folder'],args.merge_output)
    print(json.dumps(result,ensure_ascii=True,indent=2))
    return 0 if result['complete_received'] else 2


if __name__=='__main__':
    raise SystemExit(main())
