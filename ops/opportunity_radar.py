"""Local broad-coverage public-market recorder and factual attention worker."""

import hashlib
import json
import os
import sys
import time
import uuid
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from threading import local

import pandas as pd

from config import settings
from core.codal_news import VERSION as NEWS_NATIVE_VERSION
from core.dated_history import VERSION as COMPOSITION_VERSION
from core.market_review import market_scope, number
from core.news_backoff import VERSION as BACKOFF_VERSION
from core.news_backoff import checked_backoff, deferred
from core.news_continuity import LEGACY_VERSION as LEGACY_NEWS_VERSION
from core.news_continuity import VERSION as NEWS_VERSION
from core.news_continuity import NewsWindow
from core.opportunity_radar import REASONS, VERSION, notice_records, observe_history, scope_rows, stamp
from core.timeutils import is_trading_session, now_tehran, now_tehran_iso
from data_collector.daily_history import VERSION as HISTORY_VERSION
from data_collector.daily_history import archive_observation, normalize_observation, restore_observation
from data_collector.daily_publications import CODE_FILES as PUBLICATION_CODE_FILES
from data_collector.publication_cache import POLICY as CACHE_POLICY
from data_collector.publication_cache import VERSION as CACHE_VERSION
from database.db import DatabaseManager
from database.opportunity_radar import RadarRepository, read_snapshot
from ops.radar_continuity import PRICE_VERSION as PRICE_RETRY_VERSION
from ops.radar_continuity import VERSION as CONTINUITY_VERSION
from ops.radar_continuity import attempt_intent, news_boundary, predecessor_cycle_limit, price_budget
from ops.radar_prices import (
    SESSION_SELECTION_VERSION,
    collect_composition,
    collect_session_composition,
    composition_facts,
    composition_summary,
    prices_from_views,
    replay_composition,
    replay_session_selection,
)
from ops.radar_publications import POLICY as PRODUCER_POLICY
from ops.radar_publications import produce, replay_producer
from ops.report_files import private_report_path, read_private_report, write_private_report

SOURCE_FILES = ('core/opportunity_radar.py', 'database/opportunity_radar.py', 'ops/opportunity_radar.py',
    'data_collector/tsetmc.py', 'data_collector/codal.py', 'data_collector/validator.py',
    'data_collector/base.py', 'database/db.py', 'database/migrator.py', 'ops/report_files.py',
    'requirements.lock.txt',
    'core/market_rules.py', 'core/market_review.py', 'core/text.py', 'core/timeutils.py', 'core/news_continuity.py',
    'config/default.yaml', 'config/settings.py', 'database/migrations/016_opportunity_radar.sql',
    'data_collector/daily_history.py','core/news_backoff.py','ops/radar_continuity.py')
SOURCE_FILES = tuple(dict.fromkeys((*SOURCE_FILES, *PUBLICATION_CODE_FILES,
    'core/dated_history.py', 'data_collector/publication_cache.py', 'ops/radar_prices.py',
    'ops/radar_publications.py', 'core/codal_news.py', 'core/codal_search.py', 'core/codal_documents.py')))
WATCH_COVERAGE_VERSION = 'native-watch-presence-v1'


def root_path(root=None):
    path = Path(root or settings.DATA_DIR/'private'/'opportunity-radar').resolve()
    private_report_path(path/'heartbeat.json', forbidden_paths=(settings.DB_PATH,))
    target=(path/'radar.db').resolve()
    if target == settings.DB_PATH.resolve() or (target.exists() and settings.DB_PATH.exists()
            and target.samefile(settings.DB_PATH)):
        raise ValueError('Radar requires a separate DB')
    return path


def code_identity():
    return {name: hashlib.sha256((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n')).hexdigest()
            for name in SOURCE_FILES}


def watch_coverage(observation, retained_scope):
    """Native identity presence is separate from strict current-type admission.

    Registry types are never joined into the received rows. Missing native bodies
    leave presence unknown; a parsed native payload is not original HTTP bytes.
    """
    admitted, exclusions = market_scope(observation)
    known = {row['ins_code'] for row in retained_scope}
    eligible = {row['metadata']['ins_code'] for row in admitted}
    not_admitted = sorted(known-eligible)
    result = dict(version=WATCH_COVERAGE_VERSION, public_watch_available=bool(admitted),
        source_scope_count=len(admitted), retained_scope_count=len(known), exclusions=exclusions,
        known_missing_from_watch=not_admitted,
        known_missing_from_watch_semantics='not admitted to verified scope; not native-payload absence',
        known_not_admitted_codes=not_admitted, historical_membership_verified=False,
        native_presence_available=False, native_row_count=None, native_invalid_identity_rows=None,
        native_malformed_rows=None, native_duplicate_identity_rows=None,
        known_native_present_codes=None, known_native_absent_codes=None,
        known_native_present_unclassified_codes=None, known_native_present_outside_scope_codes=None)
    payload = observation.get('payload') if isinstance(observation,dict) else None
    rows = payload.get('marketwatch') if isinstance(payload,dict) else None
    if not isinstance(rows,list):
        return result
    result.update(native_row_count=len(rows), native_malformed_rows=sum(not isinstance(row,dict) for row in rows))
    if result['native_malformed_rows']:
        return result
    native, kinds = [], {}
    for row in rows:
        value = row.get('insCode')
        code = str(value) if type(value) in (str,int) else ''
        if not code.isascii() or not code.isdigit() or code.startswith('0') or len(code)>30:
            continue
        native.append(code)
        kinds.setdefault(code,set()).add(number(row.get('yVal'),positive=True,integer=True))
    counts = Counter(native)
    present = known & set(native)
    unknown = {code for code in present if None in kinds[code] or len(kinds[code])!=1}
    outside = {code for code in present-unknown if not kinds[code] & {300,303}}
    result.update(native_presence_available=True, native_invalid_identity_rows=len(rows)-len(native),
        native_duplicate_identity_rows=sum(count-1 for count in counts.values()),
        known_native_present_codes=sorted(present), known_native_absent_codes=sorted(known-present),
        known_native_present_unclassified_codes=sorted(unknown),
        known_native_present_outside_scope_codes=sorted(outside))
    return result


def registry_seed():
    """Read only public identities and explicit calendar exceptions from the account DB."""
    if not settings.DB_PATH.exists():
        return [], {}
    with DatabaseManager(read_only=True) as db:
        def restrict(action, table, column, *_):
            import sqlite3
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_READ and table not in ('symbols','market_calendar') else sqlite3.SQLITE_OK
        db.conn.set_authorizer(restrict)
        return scope_rows(db.get_universe_symbols(include_inactive=True)), db.get_market_calendar()


def activate(root=None, *, new_run=False, seed=None):
    root = root_path(root)
    root.mkdir(parents=True, exist_ok=True)
    with DatabaseManager(root/'radar.db') as db:
        repo = RadarRepository(db)
        current = repo.current()
        if current and not new_run:
            protocol = json.loads(current['protocol_json'])
            if protocol['source_files_sha256'] != code_identity():
                raise ValueError('Radar code changed; start a new registered run without replacing the old ledger')
            return protocol
        inherited = repo.retained_scope()
        registry, calendar = registry_seed()
        registry = scope_rows([*inherited['identities'], *registry])
        if seed is not None:
            registry=scope_rows([*registry,*seed])
        registered = now_tehran_iso()
        inherited_news = news_boundary(root,db,as_of=registered)
        news_since = inherited_news['required_since']
        protocol = dict(id='RADAR-'+uuid.uuid4().hex, version=VERSION, registered_at=registered,
            source_files_sha256=code_identity(), initial_scope=registry, calendar_exceptions=calendar,
            inherited_scope_runs=inherited['source_runs'],
            observation_rules=REASONS, market_types=[300,303], minimum_calendar_symbols=20,
            history_rows_requested=100, price_scan_after='13:15', price_retry_seconds=900, price_daily_limit=3,
            news_interval_open_seconds=600, news_interval_closed_seconds=3600, news_pages=50,
            news_window_version=NEWS_VERSION, news_initial_since=news_since,
            news_native_page_version=NEWS_NATIVE_VERSION,
            news_overlap_seconds=7200, news_time_budget_seconds=120,
            polling_schedule='after collection; wake at 08:30 and price-scan boundary on scheduled sessions; no catch-up burst',
            scope='ordinary Bourse/Farabourse; inactive retained; current public identities merged',
            source_archives='native daily-history and approved public Codal JSON bytes with deterministic replay; legacy news pages retain explicit unsupported native evidence',
            history_evidence_version=HISTORY_VERSION,
            dated_price_composition_version=COMPOSITION_VERSION, dated_cache_version=CACHE_VERSION,
            composition_session_selection_version=SESSION_SELECTION_VERSION,
            dated_cache_policy=dict(CACHE_POLICY),
            publication_producer_policy=dict(PRODUCER_POLICY),
            bootstrap_price_policy='latest evidenced inferred session only if no current-day view; historical baseline labelled',
            watch_coverage_version=WATCH_COVERAGE_VERSION,
            continuity_version=CONTINUITY_VERSION,news_boundary_registration=inherited_news,
            price_retry_version=PRICE_RETRY_VERSION,
            historical_research='surge-pattern-diagnostics-v1; observational definitions only; none accepted',
            classification='factual research attention, not purchase/sale or predicted growth',
            upcoming_outcome_horizons_sessions=[20,60],
            evaluation_plan='retain all daily scope rows, including false/unknown flags and missed collection; evaluate future price paths and false alarms only after outcomes mature',
            independent_external_timestamp=False, holdout_unsealed=False, models_fitted=0,
            accepted_for_signals=False, automatic_orders=False)
        run_folder=root/protocol['id']
        run_folder.mkdir(exist_ok=False)
        for name in SOURCE_FILES:
            target=run_folder/'code'/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes((settings.BASE_DIR/name).read_bytes().replace(b'\r\n',b'\n'))
        write_private_report(run_folder/'protocol.json',protocol)
        repo.activate(protocol)
        return protocol


def _history_result(provider, code):
    """One request feeds both the retained native receipt and analyzed values."""
    if not getattr(provider, 'supports_history_observations', False):
        frame = provider.get_symbol_history(code, last_n=100)
        return frame, dict(status='unsupported', ins_code=code, observation=None)
    observation = provider.get_symbol_history_observation(code, last_n=100)
    if observation is None:
        return None, dict(status='source_unavailable', ins_code=code, observation=None)
    evidence = dict(status='native', ins_code=code, observation=archive_observation(observation))
    if observation.get('ins_code') != code or observation.get('last_n') != 100:
        return None, dict(evidence, status='request_mismatch')
    try:
        return normalize_observation(observation), evidence
    except ValueError:
        return None, dict(evidence, status='normalization_rejected')


def _histories(scope, provider, provider_factory, progress=None, archive=None):
    """At most two independent providers; stop scheduling after five consecutive failures."""
    results, problems, pool, contexts = {}, {}, local(), []
    def fetch(item):
        if not hasattr(pool,'provider'):
            pool.provider = provider_factory() if provider_factory else provider
            contexts.append(pool.provider)
        try:
            frame, evidence = _history_result(pool.provider, item['ins_code'])
            return (frame if isinstance(frame,pd.DataFrame) and not frame.empty else None), evidence
        except Exception:
            return None, dict(status='request_failed', ins_code=item['ins_code'], observation=None)
    pending, queue, failures, stopped = {}, iter(scope), 0, False
    workers=2 if provider_factory else 1
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for _ in range(min(workers,len(scope))):
                item=next(queue); pending[executor.submit(fetch,item)]=item
            while pending:
                ready,_=wait(pending,return_when=FIRST_COMPLETED)
                for future in ready:
                    item=pending.pop(future); (frame,evidence)=future.result(); code=item['ins_code']
                    if archive:
                        # Archive errors abort the cycle; unarchived values must not enter the ledger.
                        archive(code,evidence)
                    results[code]=frame
                    failures=failures+1 if frame is None else 0
                    stopped = stopped or failures >= 5
                    if frame is None:
                        problems[code]='history_request_failed_or_empty'
                    if progress:
                        progress(len(results),len(scope))
                    if not stopped:
                        item=next(queue,None)
                        if item is not None:
                            pending[executor.submit(fetch,item)]=item
            for item in queue:
                problems[item['ins_code']]='not_requested_after_source_failures'
    finally:
        for context in contexts:
            if context is not provider:
                context.close()
    return results, problems


def collect_cycle(root, protocol, *, provider, codal, price_scan=False, provider_factory=None, bootstrap=False):
    root=root_path(root)
    if protocol['source_files_sha256'] != code_identity():
        raise ValueError('Source changed after radar registration')
    run_id=protocol['id']
    with DatabaseManager(root/'radar.db',read_only=True) as db:
        latest=RadarRepository(db).latest(run_id)
    prior=latest['report'] if latest else None
    known=prior['scope'] if prior else protocol['initial_scope']
    folder=root/run_id/('CYCLE-'+uuid.uuid4().hex)
    folder.mkdir(parents=True,exist_ok=False)
    started=now_tehran_iso()
    with DatabaseManager(root/'radar.db',read_only=True) as db, read_snapshot(db.conn):
        boundary=news_boundary(root,db,as_of=started,overlap_seconds=protocol['news_overlap_seconds'])
        budget=price_budget(root,db,as_of=started,retry_seconds=protocol['price_retry_seconds'],
                            daily_limit=protocol.get('price_daily_limit'))
    write_private_report(folder/'news-boundary-source.json',boundary)
    write_private_report(folder/'price-budget-source.json',budget)
    requested_prices=price_scan
    price_scan=bool(price_scan and not budget['deferred'])
    write_private_report(root/'heartbeat.json',dict(run_id=run_id,status='collecting',started_at=started,
        heartbeat_at=started,price_scan=price_scan,accepted_for_signals=False))
    write_private_report(folder/'request.json',dict(registered_at=started,run_id=run_id,
        price_scan=price_scan,price_scan_requested=requested_prices,bootstrap=bootstrap,known_instruments=len(known)))
    try:
        watch=provider.get_market_observation()
    except Exception:
        watch=None
    observed,_=market_scope(watch)
    present=[item['metadata'] for item in observed]
    scope=scope_rows([*known,*present])
    if not scope:
        raise ValueError('No verified ordinary-stock identities available; fallback test symbols are forbidden')
    write_private_report(folder/'market-source.json',watch or {'unavailable':True})
    source_meta=watch_coverage(watch,scope)
    since = boundary['required_since']
    backoff_path=root/'news-backoff.json'
    backoff_record=read_private_report(backoff_path) if backoff_path.exists() else dict(version=BACKOFF_VERSION,state=None)
    if set(backoff_record)!={'version','state'} or backoff_record.get('version')!=BACKOFF_VERSION:
        raise ValueError('Unknown news rate-limit state version')
    backoff=checked_backoff(backoff_record.get('state'))
    write_private_report(folder/'news-backoff-source.json',dict(version=BACKOFF_VERSION,state=backoff))
    def news_progress(page):
        write_private_report(root/'heartbeat.json',dict(run_id=run_id,status='collecting',started_at=started,
            heartbeat_at=now_tehran_iso(),collection_phase='news',news_pages_fetched=page,
            price_scan=price_scan,accepted_for_signals=False))
    try:
        requested_at=now_tehran_iso()
        if deferred(backoff,at=requested_at):
            scan=NewsWindow(since=since,requested_at=requested_at,initial_rate_limit_state=backoff)
            news=scan.result(stop_reason='rate_limit_cooldown')
        else:
            news=codal.get_news_window(since=since,pages=protocol['news_pages'],
                time_budget_seconds=protocol['news_time_budget_seconds'],progress=news_progress)
    except Exception:
        news=dict(letters=[],failed_page=1,errors=['news_request_failed'],page_limit_reached=False,
                  window_start=since,window_scan_satisfied=False,checkpoint_through=None)
    write_private_report(folder/'news-source.json',news)
    if 'rate_limit_state' in news:
        write_private_report(backoff_path,dict(version=BACKOFF_VERSION,state=checked_backoff(news['rate_limit_state'])))
    if news.get('window_start')!=since:
        raise ValueError('News provider changed the requested continuity boundary')
    rows=prior['rows'] if prior else []
    price_observed_at=prior.get('price_observed_at') if prior else None
    price_date=prior.get('price_session_date') if prior else None
    price_problems={}
    price_composition=prior.get('price_composition') if prior else None
    composition_selection=prior.get('composition_session_selection') if prior else None
    requested_price_session=prior.get('requested_price_session') if prior else None
    publication_producer=prior.get('publication_producer') if prior else None
    if price_scan:
        today=now_tehran().date().isoformat()  # One requested session even if collection crosses midnight.
        requested_price_session=today
        write_private_report(folder/'price-attempt.json',attempt_intent(root,protocol,folder,
            attempted_at=now_tehran_iso(),scope_count=len(scope)))
        last_progress=[time.monotonic()]
        def progress(completed,total):
            if time.monotonic()-last_progress[0]>=15 or completed==total:
                write_private_report(root/'heartbeat.json',dict(run_id=run_id,status='collecting',
                    heartbeat_at=now_tehran_iso(),completed=completed,total=total,started_at=started,
                    price_scan=True,accepted_for_signals=False))
                last_progress[0]=time.monotonic()
        if protocol.get('publication_producer_policy') is not None:
            def publication_progress(completed,total,symbol):
                progress(completed,total)
            publication_producer=produce(folder,scope,protocol,provider=provider,
                provider_factory=provider_factory,progress=publication_progress,clock=now_tehran())
        history_index={}
        def archive(code,evidence):
            name=f'history-{code}.json'
            write_private_report(folder/name,evidence)
            history_index[code]=dict(file=name,status=evidence['status'])
        histories,price_problems=_histories(scope,provider,provider_factory,progress,archive=archive)
        write_private_report(folder/'history-index.json',dict(version=HISTORY_VERSION,items=history_index))
        frames=[frame.assign(symbol=item['symbol_fa'],ins_code=item['ins_code']) for item in scope
                if (frame:=histories.get(item['ins_code'])) is not None]
        prices=pd.concat(frames,ignore_index=True) if frames else pd.DataFrame(columns=['date','symbol','ins_code','volume'])
        prices.to_parquet(folder/'prices.parquet',index=False)
        provenance=None
        if protocol.get('dated_price_composition_version') == COMPOSITION_VERSION:
            if protocol.get('dated_cache_version') != CACHE_VERSION or protocol.get('dated_cache_policy') != CACHE_POLICY:
                raise ValueError('Registered dated cache contract differs')
            if protocol.get('composition_session_selection_version') == SESSION_SELECTION_VERSION:
                histories,provenance,composition_selection=collect_session_composition(folder,histories,scope,
                    session_date=today,observed_at=now_tehran_iso(),registered_at=protocol['registered_at'],
                    bootstrap=bootstrap,minimum_calendar_symbols=protocol['minimum_calendar_symbols'])
            else:
                histories,provenance=collect_composition(folder,histories,scope,session_date=today,
                    observed_at=now_tehran_iso(),registered_at=protocol['registered_at'])
            prices=prices_from_views(histories,scope)
            price_composition=composition_summary(provenance)
        if not prices.empty:
            counts=prices.loc[prices.volume.gt(0)].groupby('date').ins_code.nunique()
            calendar=sorted(counts.index[counts.ge(protocol['minimum_calendar_symbols'])])
        else:
            calendar=[]
        # Bootstrap can show a previous session, explicitly marked as retrospective baseline.
        has_today=not prices.empty and prices.date.eq(today).any()
        price_date=calendar[-1] if calendar and bootstrap and not has_today else today
        if composition_selection is not None:
            price_date=composition_selection['selected_session']
        if price_date not in calendar:
            calendar=sorted(set(calendar)|{price_date})
        rows=[]
        for item in scope:
            code=item['ins_code']
            facts=(composition_facts(histories.get(code),provenance[code],calendar,session_date=price_date)
                   if provenance is not None else observe_history(histories.get(code),calendar,session_date=price_date))
            rows.append(dict(symbol=item['symbol_fa'],ins_code=code,sector_code=item.get('sector_code'),
                instrument_type_code=item['instrument_type_code'],**facts))
        price_observed_at=now_tehran_iso()
        write_private_report(folder/'price-calendar.json', dict(days=calendar, inferred=True,
            minimum_symbols=protocol['minimum_calendar_symbols'], official_calendar_verified=False))
    else:
        # New identities must remain visible before their first price collection.
        old_codes={row['ins_code'] for row in rows}
        rows=[*rows,*[dict(symbol=item['symbol_fa'],ins_code=item['ins_code'],date=None,status='awaiting_price_scan',
            reasons={key:None for key in REASONS},accepted_for_signals=False) for item in scope if item['ins_code'] not in old_codes]]
    at=now_tehran_iso()
    notices,excluded_notices=notice_records(news,scope,received_at=at)
    artifacts={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
    write_private_report(folder/'manifest.json',dict(version=VERSION,files=artifacts,run_id=run_id))
    report=dict(version=VERSION,run_id=run_id,started_at=started,observed_at=at,scope=scope,rows=rows,
        price_scan=price_scan,bootstrap=bootstrap,price_observed_at=price_observed_at,price_session_date=price_date,
        source_coverage=source_meta,price_request_problems=price_problems,notices=notices,
        price_composition=price_composition,
        requested_price_session=requested_price_session,composition_session_selection=composition_selection,
        publication_producer=publication_producer,
        price_scan_requested=requested_prices,price_retry=budget,
        excluded_or_unmapped_notices=excluded_notices,
        news_coverage=dict(pages_fetched=news.get('pages_fetched'),failed_page=news.get('failed_page'),
            page_limit_reached=news.get('page_limit_reached'),reached_end=news.get('reached_end'),
            errors=news.get('errors',[]),complete_coverage_verified=False,
            **{key:news.get(key) for key in ('window_start','checkpoint_through','window_scan_satisfied',
                'head_recheck_stable','boundary_reached','oldest_published_at','newest_published_at',
                'stop_reason','invalid_rows','rate_limit_state','rate_limit_deferred')}),
        artifacts_folder=str(folder.relative_to(root)),artifacts_sha256=artifacts,
        accepted_for_signals=False,models_fitted=0,automatic_orders=False)
    def require_unchanged_source():
        if protocol['source_files_sha256'] != code_identity():
            raise ValueError('Source changed during radar collection; cycle was not committed')
    with DatabaseManager(root/'radar.db') as db:
        receipt=RadarRepository(db).append(run_id,report,before_commit=require_unchanged_source)
    health='observed_with_coverage_limits' if (rows and all(r['status']=='observed' for r in rows)
        and present and not news.get('failed_page')) else 'degraded'
    write_private_report(root/'heartbeat.json',dict(run_id=run_id,status=health,heartbeat_at=at,
        last_cycle_id=receipt['cycle_id'],new_attention=receipt['new_attention'],price_observed_at=price_observed_at,
        price_session_date=price_date,news_coverage=report['news_coverage'],accepted_for_signals=False))
    return report,receipt


def status(root=None):
    root=root_path(root)
    if not (root/'radar.db').exists():
        return dict(active=False,root=str(root))
    with DatabaseManager(root/'radar.db',read_only=True) as db, read_snapshot(db.conn):
        repo=RadarRepository(db); run=repo.current()
        if run is None:
            return dict(active=False,root=str(root))
        latest=repo.latest(run['id'])
        events=repo.events(run['id'])
        heartbeat=read_private_report(root/'heartbeat.json') if (root/'heartbeat.json').exists() else None
        return dict(active=True,protocol=json.loads(run['protocol_json']),latest=latest,events=events,heartbeat=heartbeat)


@contextmanager
def worker_lock(root):
    """One collector per root, released by the OS even after an abrupt process exit."""
    root.mkdir(parents=True,exist_ok=True)
    with (root/'worker.lock').open('a+b') as stream:
        if stream.seek(0,os.SEEK_END)==0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        if sys.platform.startswith('win'):
            import msvcrt
            msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if sys.platform.startswith('win'):
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def verify(root=None):
    """Read-only local chain, artifact and daily factual-flag replay."""
    root=root_path(root)
    checks=news_checks=native_checks=native_news_checks=0
    with DatabaseManager(root/'radar.db',read_only=True) as db, read_snapshot(db.conn):
        repo=RadarRepository(db); run=repo.current()
        if run is None:
            raise ValueError('No radar run to verify')
        protocol=json.loads(run['protocol_json'])
        if protocol['source_files_sha256']!=code_identity():
            raise ValueError('Current code differs from registered replay code')
        for name,expected in protocol['source_files_sha256'].items():
            source=(root/run['id']/'code'/name).resolve()
            if not source.is_relative_to(root/run['id']/'code') or hashlib.sha256(source.read_bytes()).hexdigest()!=expected:
                raise ValueError('Archived radar code differs')
        result=repo.verify(run['id'])
        ordinal=db.conn.execute('SELECT rowid FROM radar_runs WHERE id=?',(run['id'],)).fetchone()[0]
        if protocol.get('continuity_version')==CONTINUITY_VERSION:
            receipt=protocol['news_boundary_registration']
            limit=predecessor_cycle_limit(db,run_limit=ordinal-1,as_of=protocol['registered_at'])
            expected=news_boundary(root,db,as_of=protocol['registered_at'],run_limit=ordinal-1,cycle_limit=limit)
            if expected!=receipt or protocol['news_initial_since']!=expected['required_since']:
                raise ValueError('Registered inherited news debt differs')
        for cycle in db.conn.execute('SELECT id,report_json FROM radar_cycles WHERE run_id=? ORDER BY id',(run['id'],)):
            report=json.loads(cycle['report_json'])
            folder=(root/report['artifacts_folder']).resolve()
            if not folder.is_relative_to(root):
                raise ValueError('Artifact path escapes radar root')
            for name,expected in report['artifacts_sha256'].items():
                path=(folder/name).resolve()
                if not path.is_relative_to(folder) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                    raise ValueError('Radar source artifact differs')
            if protocol.get('watch_coverage_version') == WATCH_COVERAGE_VERSION:
                market=read_private_report(folder/'market-source.json')
                if watch_coverage(market,report['scope']) != report['source_coverage']:
                    raise ValueError('Radar watch coverage differs from archived observations')
            if protocol.get('continuity_version')==CONTINUITY_VERSION:
                receipt=read_private_report(folder/'news-boundary-source.json')
                expected=news_boundary(root,db,as_of=report['started_at'],overlap_seconds=protocol['news_overlap_seconds'],
                                       run_limit=ordinal,cycle_limit=cycle['id']-1)
                if receipt!=expected:
                    raise ValueError('Radar predecessor news inventory differs')
                news_source=read_private_report(folder/'news-source.json')
                if news_source.get('window_start')!=expected['required_since']:
                    raise ValueError('Radar scan narrowed its inherited news debt')
            if protocol.get('price_retry_version')==PRICE_RETRY_VERSION:
                budget=read_private_report(folder/'price-budget-source.json')
                refs=[row for row in budget['attempts']if row['kind']=='registered_intent_only']
                expected=price_budget(root,db,as_of=report['started_at'],retry_seconds=protocol['price_retry_seconds'],
                    run_limit=ordinal,cycle_limit=cycle['id']-1,partial_refs=refs,
                    daily_limit=protocol.get('price_daily_limit'))
                if expected!=budget or report['price_retry']!=expected or report['price_scan']!=bool(
                        report['price_scan_requested'] and not expected['deferred']):
                    raise ValueError('Radar price retry budget differs')
                if report['price_scan']:
                    intent=read_private_report(folder/'price-attempt.json')
                    if intent!=attempt_intent(root,protocol,folder,attempted_at=intent['attempted_at'],scope_count=len(report['scope'])):
                        raise ValueError('Radar price attempt differs from its request')
            news = read_private_report(folder/'news-source.json')
            if news.get('version') in (NEWS_VERSION, LEGACY_NEWS_VERSION):
                backoff_record=read_private_report(folder/'news-backoff-source.json')
                if set(backoff_record)!={'version','state'} or backoff_record.get('version')!=BACKOFF_VERSION:
                    raise ValueError('Unknown archived news rate-limit version')
                initial=checked_backoff(backoff_record.get('state'))
                suppressed=deferred(initial,at=news['requested_at'])
                if news.get('rate_limit_deferred') is not suppressed:
                    raise ValueError('News rate-limit request decision differs')
                if news.get('initial_rate_limit_state') != (initial if suppressed else None):
                    raise ValueError('News deferred source differs')
                replay = NewsWindow(since=news['window_start'], requested_at=news['requested_at'],
                                    initial_rate_limit_state=news.get('initial_rate_limit_state'),
                                    version=news['version'])
                for page in news['pages']:
                    replay.add(page)
                expected = replay.result(head_recheck=news['head_recheck'], stop_reason=news['stop_reason'])
                if expected != news:
                    raise ValueError('Radar news-window replay differs')
                for key, value in report['news_coverage'].items():
                    if key in expected and value != expected[key]:
                        raise ValueError('Radar news coverage differs from archived observations')
                notices, excluded = notice_records(news, report['scope'], received_at=report['observed_at'])
                if notices != report['notices'] or excluded != report['excluded_or_unmapped_notices']:
                    raise ValueError('Radar news matching replay differs')
                news_checks += 1
                native_news_checks += sum(page.get('native_evidence', {}).get('status') == 'captured'
                    for page in [*news['pages'], *([news['head_recheck']] if news['head_recheck'] else [])])
            if report['price_scan']:
                prices=pd.read_parquet(folder/'prices.parquet')
                if protocol.get('history_evidence_version') == HISTORY_VERSION:
                    index=read_private_report(folder/'history-index.json')
                    if index.get('version')!=HISTORY_VERSION:
                        raise ValueError('History evidence version differs')
                    scope_codes={item['ins_code'] for item in report['scope']}
                    if set(index['items'])-scope_codes:
                        raise ValueError('History evidence identity outside scope')
                    missing=scope_codes-set(index['items'])
                    if any(report['price_request_problems'].get(code)!='not_requested_after_source_failures' for code in missing):
                        raise ValueError('History evidence missing for a requested identity')
                    native_frames=[]
                    unsupported_codes=set()
                    for code,item in index['items'].items():
                        name=f'history-{code}.json'
                        if item.get('file')!=name or name not in report['artifacts_sha256']:
                            raise ValueError('History evidence file is not bound to manifest')
                        evidence=read_private_report(folder/name)
                        if evidence.get('ins_code')!=code or evidence.get('status')!=item.get('status'):
                            raise ValueError('History evidence request differs')
                        if item['status']=='unsupported':
                            unsupported_codes.add(code)
                        elif item['status'] in ('native','normalization_rejected','request_mismatch'):
                            observation=restore_observation(evidence['observation'])
                            same_request=observation.get('ins_code')==code and observation.get('last_n')==100
                            if item['status']=='request_mismatch':
                                if same_request:
                                    raise ValueError('History request mismatch was not reproduced')
                                continue
                            if not same_request:
                                raise ValueError('History evidence response identity differs')
                            try:
                                frame=normalize_observation(observation)
                            except ValueError:
                                if item['status']!='normalization_rejected':
                                    raise ValueError('History normalization no longer matches receipt') from None
                                continue
                            if item['status']!='native':
                                raise ValueError('History rejection was not reproduced')
                            native_frames.append(frame.assign(ins_code=code))
                            native_checks+=1
                        elif item['status'] not in ('source_unavailable','request_failed'):
                            raise ValueError('Unknown history evidence status')
                    native=pd.concat(native_frames,ignore_index=True) if native_frames else pd.DataFrame(columns=prices.columns)
                    columns=[c for c in prices.columns if c!='symbol']
                    def canonical(frame):
                        return frame.reindex(columns=columns).sort_values(['ins_code','date']).reset_index(drop=True)
                    actual=prices.loc[~prices.ins_code.isin(unsupported_codes)]
                    try:
                        pd.testing.assert_frame_equal(canonical(actual),canonical(native),check_dtype=False,check_exact=True)
                    except AssertionError:
                        raise ValueError('Native history differs from normalized price archive') from None
                provenance=None
                if protocol.get('publication_producer_policy') is not None:
                    produced=replay_producer(folder,report['scope'],protocol,
                        allowed_files=report['artifacts_sha256'],observed_at=report['observed_at'])
                    if produced!=report.get('publication_producer'):
                        raise ValueError('Radar producer receipt differs from its source-bound replay')
                if protocol.get('dated_price_composition_version') == COMPOSITION_VERSION:
                    if protocol.get('dated_cache_version') != CACHE_VERSION or protocol.get('dated_cache_policy') != CACHE_POLICY:
                        raise ValueError('Registered dated cache contract differs')
                    histories={item['ins_code']:prices.loc[prices.ins_code.eq(item['ins_code'])].copy()
                               if not prices.empty else None for item in report['scope']}
                    if protocol.get('composition_session_selection_version') == SESSION_SELECTION_VERSION:
                        selected=replay_session_selection(folder,histories,report['scope'],
                            session_date=report['requested_price_session'],registered_at=protocol['registered_at'],
                            allowed_files=report['artifacts_sha256'],bootstrap=report['bootstrap'],
                            minimum_calendar_symbols=protocol['minimum_calendar_symbols'])
                        if (selected!=report.get('composition_session_selection')
                                or report['price_session_date']!=selected['selected_session']
                                or report['price_composition']['requested_session']!=selected['selected_session']):
                            raise ValueError('Displayed baseline date differs from its frozen session selection')
                    histories,provenance=replay_composition(folder,histories,report['scope'],
                        session_date=report['price_composition']['requested_session'],registered_at=protocol['registered_at'],
                        allowed_files=report['artifacts_sha256'])
                    if (report.get('price_composition') != composition_summary(provenance)
                            or any(not stamp(report['started_at']) <= stamp(p['cache_consumed_at']) <= stamp(report['observed_at'])
                                   for p in provenance.values())):
                        raise ValueError('Dated composition coverage or consumption clock differs')
                    prices=prices_from_views(histories,report['scope'])
                calendar=read_private_report(folder/'price-calendar.json')['days']
                if provenance is not None:
                    counts=(prices.loc[prices.volume.gt(0)].groupby('date').ins_code.nunique()
                            if not prices.empty else pd.Series(dtype=int))
                    expected=sorted(set(counts.index[counts.ge(protocol['minimum_calendar_symbols'])])|{report['price_session_date']})
                    if calendar!=expected:
                        raise ValueError('Dated view session calendar differs')
                for row in report['rows']:
                    frame=prices.loc[prices.ins_code.eq(row['ins_code'])] if not prices.empty else None
                    facts=(composition_facts(frame,provenance[row['ins_code']],calendar,session_date=report['price_session_date'])
                           if provenance is not None else observe_history(frame,calendar,session_date=report['price_session_date']))
                    if any(row.get(key)!=value for key,value in facts.items()):
                        raise ValueError('Radar price observation replay differs')
                    checks+=1
    return dict(result,replayed_instruments=checks,replayed_news_windows=news_checks,
                replayed_native_histories=native_checks,replayed_native_news_pages=native_news_checks,
                source_artifacts_verified=True)


def watch(root=None, *, new_run=False, once=False, force_prices=False):
    root=root_path(root)
    with worker_lock(root):
        return _watch(root,new_run=new_run,once=once,force_prices=force_prices)


def next_poll_delay(clock, protocol, *, price_not_before=None):
    """Bound the quiet-period sleep by today's scheduled collection boundaries.

    This is the recorder's schedule, not verification that the exchange is open.
    Use completion time: a slow request can itself cross a boundary.
    """
    clock = stamp(clock.isoformat())
    scheduled = is_trading_session(clock, protocol['calendar_exceptions'])
    hhmm = clock.strftime('%H:%M')
    active = scheduled and '08:30' <= hhmm <= '15:00'
    delay = protocol['news_interval_open_seconds'] if active else protocol['news_interval_closed_seconds']
    if scheduled:
        for boundary in ('08:30', protocol['price_scan_after']):
            hour, minute = map(int, boundary.split(':'))
            target = clock.replace(hour=hour, minute=minute, second=0, microsecond=0)
            until = (target - clock).total_seconds()
            if until > 0:
                delay = min(delay, until)
    if price_not_before:
        until=(stamp(price_not_before)-clock).total_seconds()
        if until>0:
            delay=min(delay,until)
    return float(delay)


def _wait_for_poll(root, protocol):
    """Sleep in short pieces; resume once after suspension without a request burst."""
    clock = now_tehran()
    budget=dict(not_before=None)
    if (root/'radar.db').exists():
        with DatabaseManager(root/'radar.db',read_only=True) as db:
            budget=price_budget(root,db,as_of=clock.isoformat(),retry_seconds=protocol['price_retry_seconds'],
                                daily_limit=protocol.get('price_daily_limit'))
        previous=status(root).get('latest')
        report=previous['report']if previous else {}
        if (report.get('price_scan_requested') is True and report.get('price_scan') is False
                and report.get('price_retry',{}).get('deferred') is True and not budget['deferred']):
            return  # The cooldown expired during another source's slow collection.
    delay = next_poll_delay(clock, protocol,price_not_before=budget['not_before'])
    wall_deadline = clock + timedelta(seconds=delay)
    deadline = time.monotonic() + delay
    while True:
        # A forward clock change can bring the session boundary closer; a backward
        # change must not extend the monotonic maximum. This is not clock calibration.
        remaining = min(deadline-time.monotonic(), (wall_deadline-now_tehran()).total_seconds())
        if remaining <= 0:
            return
        time.sleep(min(30, remaining))
        heartbeat = read_private_report(root/'heartbeat.json')
        remaining = min(deadline-time.monotonic(), (wall_deadline-now_tehran()).total_seconds())
        heartbeat.update(heartbeat_at=now_tehran_iso(), worker_state='waiting',
            seconds_until_poll=max(0, round(remaining)), next_poll_at=wall_deadline.isoformat())
        write_private_report(root/'heartbeat.json', heartbeat)


def _watch(root, *, new_run, once, force_prices):
    """Owned local worker. No financial account access, orders, or external messaging."""
    from data_collector.codal import CodalProvider
    from data_collector.tsetmc import TsetmcProvider
    root=root_path(root)
    protocol=activate(root,new_run=new_run)
    provider,codal=TsetmcProvider(realtime=True),CodalProvider()
    codal.timeout=5
    try:
        while True:
            clock=now_tehran()
            latest=status(root)['latest']
            previous=latest['report'] if latest else None
            needs_baseline=previous is None or previous.get('price_observed_at')is None
            due=needs_baseline or force_prices or (is_trading_session(clock,protocol['calendar_exceptions'])
                and clock.strftime('%H:%M')>=protocol['price_scan_after']
                and (previous.get('price_session_date')!=clock.date().isoformat()
                     or any(r['status']!='observed' for r in previous['rows'])))
            try:
                report,receipt=collect_cycle(root,protocol,provider=provider,codal=codal,price_scan=due,
                    provider_factory=lambda:TsetmcProvider(realtime=True),bootstrap=needs_baseline)
                print(json.dumps(dict(recorded_at=report['observed_at'],scope=len(report['scope']),
                    valid_prices=sum(r['status']=='observed' for r in report['rows']),
                    price_scan=report['price_scan'],new_attention=receipt['new_attention'],accepted_for_signals=False),ensure_ascii=False),flush=True)
            except Exception as error:
                write_private_report(root/'heartbeat.json',dict(run_id=protocol['id'],status='error',
                    heartbeat_at=now_tehran_iso(),error_type=type(error).__name__,message=str(error),accepted_for_signals=False))
                if once:
                    raise
                print(type(error).__name__+': '+str(error),flush=True)
            if once:
                return 0
            force_prices=False
            _wait_for_poll(root, protocol)
    finally:
        provider.close()
        codal.session.close()
