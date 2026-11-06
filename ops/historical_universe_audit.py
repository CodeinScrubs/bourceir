"""Compare pre-holdout exchange identity sightings with frozen and current research coverage."""

import argparse
import hashlib
import json
import re
import sqlite3
import uuid
from collections import Counter, defaultdict
from contextlib import closing
from pathlib import Path

from config import settings
from core.timeutils import now_tehran_iso
from ops.query_report import export_csv
from ops.report_files import private_report_path, write_private_report
from ops.research_review import read_development_parquet, sha256
from ops.rights_daily_universe import _read_plan, _records
from ops.rights_discovery import _canonical, _code_fingerprints

VERSION = 'historical-universe-audit-v1'
ORDINARY = {300, 303}
CODE = re.compile(r'[1-9][0-9]{0,29}')
ISIN = re.compile(r'IR[A-Z0-9]{10}')
SOURCE_FILES = ('ops/historical_universe_audit.py', 'ops/rights_daily_universe.py', 'core/rights_daily_universe.py',
                'ops/research_review.py', 'config/settings.py', 'config/default.yaml')


def _open_read_only(path, *, immutable=False):
    path = Path(path).resolve(strict=True)
    suffix = '&immutable=1' if immutable else ''
    return sqlite3.connect(path.as_uri() + '?mode=ro' + suffix, uri=True)


def _registry(conn):
    rows = conn.execute('SELECT id, ins_code, tse_code, symbol_fa, instrument_type_code FROM symbols ORDER BY id')
    exact, by_code, by_isin, by_name, by_id = {}, defaultdict(set), defaultdict(set), {}, {}
    for sid, code, isin, name, instrument_type in rows:
        if not isinstance(code, str) or not CODE.fullmatch(code) or not isinstance(isin, str) or not ISIN.fullmatch(isin):
            continue
        identity = code, isin
        if identity in exact or name in by_name:
            raise ValueError('Registry identities and exact symbol names must be unique')
        exact[identity] = dict(symbol=name, type_code=instrument_type,
                               ordinary=instrument_type in ORDINARY and not name[-1:].isdigit())
        by_code[code].add(isin)
        by_isin[isin].add(code)
        by_name[name] = identity
        by_id[sid] = identity
    return exact, by_code, by_isin, by_name, by_id


def _local_prices(conn, registry, start, end):
    by_id = registry[4]
    result = defaultdict(set)
    query = '''SELECT date, symbol_id FROM daily_prices
               WHERE date >= ? AND date < ? AND source = 'tsetmc' AND price_unit = 'Rial'
               AND volume > 0 AND trade_count > 0 ORDER BY date, symbol_id'''
    for day, sid in conn.execute(query, (start, end)):
        identity = by_id.get(sid)
        if identity is not None and registry[0][identity]['ordinary']:
            result[day].add(identity)
    return result


def _frozen_prices(frame, registry, start):
    result = defaultdict(set)
    by_name = registry[3]
    for name, day, volume in frame[['symbol', 'date', 'volume']].itertuples(index=False, name=None):
        if day < start or not volume > 0:
            continue
        identity = by_name.get(name)
        if identity is None or not registry[0][identity]['ordinary']:
            raise ValueError('Frozen price symbol lacks an exact identity in its registry snapshot')
        if identity in result[day]:
            raise ValueError('Duplicate symbol-day in the frozen price file')
        result[day].add(identity)
    return result


def _scope(identity, registry, positive):
    code, isin = identity
    exact, by_code, by_isin, _, _ = registry
    item = exact.get(identity)
    if item is not None:
        if not item['ordinary']:
            return 'known_other_scope'
        return 'ordinary_price_present' if identity in positive else 'ordinary_price_unobserved'
    if code in by_code:
        return 'code_with_different_or_missing_isin'
    if isin in by_isin:
        return 'isin_with_different_code'
    return 'unknown_identity'


def compare(source_days, current, frozen, local_prices, frozen_prices, *, progress=None):
    daily, candidates, sources, by_year = [], {}, Counter(), defaultdict(Counter)
    body_counts = Counter()
    for day, record in source_days:
        assessment = record['assessment'] if record else None
        status = assessment['status'] if assessment else 'not_attempted'
        rows = (record.get('identity_projection') or {}).get('closingPriceDailyHistoryWithInstDetails', []) if record else []
        current_prices, old_prices = local_prices.get(day, set()), frozen_prices.get(day, set())
        matched_current, matched_frozen, seen = Counter(), Counter(), set()
        for row in rows:
            identity = row['insCode'], row['instrumentID']
            seen.add(identity)
            current_status = _scope(identity, current, current_prices)
            frozen_status = _scope(identity, frozen, old_prices)
            matched_current[current_status] += 1
            matched_frozen[frozen_status] += 1
            # Focus the recovery ledger on registry conflicts, missing data and additions.
            if current_status == frozen_status == 'ordinary_price_present' or 'known_other_scope' in (current_status, frozen_status):
                continue
            candidate = candidates.setdefault(identity, dict(ins_code=identity[0], isin=identity[1], names=set(),
                first_date=day, last_date=day, sightings=0, current=Counter(), frozen=Counter(), dates=[]))
            candidate['names'].add(row['lVal18AFC'])
            candidate['last_date'] = day
            candidate['sightings'] += 1
            candidate['current'][current_status] += 1
            candidate['frozen'][frozen_status] += 1
            if len(candidate['dates']) < 3:
                candidate['dates'].append(day)
        observation = (record.get('observation') or {}) if record else {}
        digest = observation.get('body_sha256') if rows else None
        if digest:
            body_counts[digest] += 1
        daily.append(dict(date=day, status=status, source_rows=len(rows) if assessment and
            status in ('observed', 'observed_empty_unverified') else None, body_sha256=digest,
            projection_sha256=record.get('projection_sha256') if record else None,
            native_date_verified=assessment.get('native_date_verified') if assessment else None,
            local_positive_prices=len(current_prices), frozen_positive_prices=len(old_prices),
            local_price_unseen_in_nonempty_source=len(current_prices - seen) if rows else None,
            frozen_price_unseen_in_nonempty_source=len(old_prices - seen) if rows else None,
            current=dict(matched_current), frozen=dict(matched_frozen)))
        year = by_year[day[:4]]
        year.update(days=1, nonempty_days=bool(rows), source_rows=len(rows), local_positive_prices=len(current_prices),
                    frozen_positive_prices=len(old_prices), source_without_rows_on_local_positive_day=bool(current_prices) and not rows)
        year.update({f'current_{key}': value for key, value in matched_current.items()})
        year.update({f'frozen_{key}': value for key, value in matched_frozen.items()})
        sources[status] += 1
        if progress and len(daily) % 500 == 0:
            progress(len(daily))
    for row in daily:
        row['repeated_nonempty_body'] = body_counts[row['body_sha256']] > 1
    for item in candidates.values():
        item['names'] = sorted(item['names'])
        item['ordinary_board_pattern'] = bool(re.fullmatch(r'IRO[13][A-Z0-9]{4}0001', item['isin'] or ''))
    ordered = sorted(candidates.values(), key=lambda item: (-item['sightings'], item['ins_code'], item['isin'] or ''))
    return daily, ordered, dict(sources), {year: dict(counts) for year, counts in sorted(by_year.items())}


def run(archive, frozen_run, registry_backup, registry_sha, output, *, progress=None):
    archive = private_report_path(Path(archive) / 'plan.json').parent
    frozen_run, registry_backup = Path(frozen_run).resolve(strict=True), Path(registry_backup).resolve(strict=True)
    if (not frozen_run.is_relative_to((settings.DATA_DIR / 'research').resolve())
            or not registry_backup.is_relative_to((settings.DATA_DIR / 'backups').resolve())
            or not re.fullmatch(r'[a-f0-9]{64}', registry_sha)):
        raise ValueError('Use a local frozen research run and its checksummed registry backup')
    plan = _read_plan(archive)
    start, end = plan['start_date'], plan['end_date_exclusive']
    if start < settings.RESEARCH_CONFIG['dev_start']:
        raise ValueError('Choose a development-window archive')
    source_plan_sha = hashlib.sha256(_canonical(plan)).hexdigest()
    with (frozen_run / 'manifest.json').open('rb') as stream:
        manifest = stream.read(2**20 + 1)
    if len(manifest) > 2**20:
        raise ValueError('Frozen manifest exceeds the audit input limit')
    identity = json.loads(manifest)
    if identity['exclusive_end_date'] < end:
        raise ValueError('The frozen data cutoff precedes the audit interval')
    output = private_report_path(Path(output) / 'audit-plan.json').parent
    output.mkdir(parents=True, exist_ok=False)
    audit_plan = dict(version=VERSION, registered_at=now_tehran_iso(), archive=archive.name, archive_plan_sha256=source_plan_sha,
        start=start, end_exclusive=end, frozen_run=frozen_run.name, frozen_registry=registry_backup.name,
        frozen_registry_sha256=registry_sha, frozen_manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        frozen_prices_sha256=identity['raw_prices_sha256'], code_sha256=_code_fingerprints(SOURCE_FILES),
        holdout_opened=False, reads_returns=False, accepted_for_signals=False)
    write_private_report(output / 'audit-plan.json', audit_plan)
    if sha256(registry_backup) != registry_sha:
        raise ValueError('Frozen registry backup differs from the registered checksum')
    with closing(_open_read_only(registry_backup, immutable=True)) as conn:
        frozen = _registry(conn)
    frame = read_development_parquet(frozen_run / 'prices.parquet', identity['raw_prices_sha256'], identity['exclusive_end_date'])
    frame = frame[frame['date'].lt(end)]
    frozen_prices = _frozen_prices(frame, frozen, start)
    del frame
    with closing(_open_read_only(settings.DB_PATH)) as conn:
        conn.execute('BEGIN')
        current = _registry(conn)
        current_prices = _local_prices(conn, current, start, end)
    current_registry_sha = hashlib.sha256(_canonical([(key, row) for key, row in sorted(current[0].items())])).hexdigest()
    current_prices_digest = hashlib.sha256()
    for day, identities in sorted(current_prices.items()):
        current_prices_digest.update(_canonical([day, sorted(identities)]) + b'\n')

    def source_days():
        for day in plan['dates']:
            attempts = _records(archive, day, source_plan_sha, plan['registered_at'])
            yield day, attempts[-1] if attempts else None

    daily, candidates, source_counts, yearly = compare(source_days(), current, frozen, current_prices, frozen_prices, progress=progress)
    if _read_plan(archive) != plan or _code_fingerprints(SOURCE_FILES) != audit_plan['code_sha256']:
        raise ValueError('Archive plan or audit code changed while reading the evidence')
    export_csv(output / 'days.csv', daily, tuple(daily[0]))
    export_csv(output / 'candidates.csv', candidates, ('ins_code', 'isin', 'names', 'first_date', 'last_date', 'sightings',
        'dates', 'ordinary_board_pattern', 'current', 'frozen'))
    totals = {kind: dict(sum((Counter(row[kind]) for row in daily), Counter())) for kind in ('current', 'frozen')}
    result = dict(version=VERSION, audit_plan_sha256=sha256(output / 'audit-plan.json'), completed_at=now_tehran_iso(),
        dates=len(daily), native_dated_nonempty_days=sum(r['native_date_verified'] is True for r in daily),
        repeated_nonempty_body_days=sum(r['repeated_nonempty_body'] for r in daily), source_status=source_counts,
        frozen_registry_count=len(frozen[0]), current_registry_count=len(current[0]), coverage_totals=totals,
        current_registry_sha256=current_registry_sha, current_positive_identity_dates_sha256=current_prices_digest.hexdigest(),
        recovery_candidate_identities=len(candidates), yearly=yearly,
        outputs={name: sha256(output / name) for name in ('days.csv', 'candidates.csv')},
        historical_membership_verified=False, profitability_proven=False, accepted_for_signals=False, holdout_opened=False)
    write_private_report(output / 'result.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--frozen-run', required=True, type=Path)
    parser.add_argument('--frozen-registry', required=True, type=Path)
    parser.add_argument('--registry-sha256', required=True)
    args = parser.parse_args()
    output = settings.DATA_DIR / 'private' / 'historical-universe-audit' / ('AUDIT-' + uuid.uuid4().hex)
    result = run(args.archive, args.frozen_run, args.frozen_registry, args.registry_sha256, output,
                 progress=lambda done: print(f'{done} days checked', flush=True))
    print(f"{result['dates']} days — {output}", flush=True)


if __name__ == '__main__':
    main()
