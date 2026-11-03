"""Read-only, predecessor-bound radar continuity and history-request budgets."""

import hashlib
import json
import re
from datetime import timedelta

from core.news_continuity import LEGACY_VERSION as LEGACY_NEWS_VERSION
from core.news_continuity import VERSION as NEWS_VERSION
from core.news_continuity import NewsWindow
from core.opportunity_radar import VERSION as RADAR_VERSION
from core.opportunity_radar import digest, stamp
from database.opportunity_radar import read_snapshot
from ops.report_files import read_private_report

VERSION = 'registered-radar-continuity-v1'
PRICE_VERSION = 'registered-history-attempt-v1'
GLOBAL_FEED = 'https://search.codal.ir/api/search/v2/q?&PageNumber='
# Audited archived implementations used this unfiltered feed before receipts
# contained their own URL/HTTP status. This is code attestation, not HTTP proof.
LEGACY_GLOBAL_TRANSPORT = frozenset({
    '65f3ae9089e3c1862075b9211c288b77e4dfd8bd636c52ead4bdef4691fc4111',
    'de8b12874b9ce0c60ef0e0c9e2582804d8064a37fc099da137a6e517acb70176',
})


def _path(root, relative):
    path = (root/relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Continuity evidence escapes radar root')
    return path


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _child(folder, relative):
    path=(folder/relative).resolve()
    if not path.is_relative_to(folder.resolve()):
        raise ValueError('Continuity evidence escapes its owning folder')
    return path


def inventory(db, *, as_of, run_limit=None, cycle_limit=None):
    """Verify exact ledger prefixes; never bind a past receipt to future tips."""
    clock = stamp(as_of)
    runs, cycles = [], []
    previous_registration=None
    with read_snapshot(db.conn):
        for row in db.conn.execute('SELECT rowid AS ordinal,* FROM radar_runs ORDER BY rowid'):
            if run_limit is not None and row['ordinal']>run_limit:
                continue
            p=json.loads(row['protocol_json'])
            registered=stamp(p['registered_at'])
            if (digest(p)!=row['protocol_sha256'] or p.get('id')!=row['id']
                    or p.get('version')!=RADAR_VERSION or registered!=stamp(row['registered_at']) or registered>clock
                    or (previous_registration is not None and registered<previous_registration)):
                raise ValueError('Invalid or future radar registration')
            previous_registration=registered
            runs.append(dict(ordinal=row['ordinal'],id=row['id'],registered_at=p['registered_at'],
                protocol_sha256=row['protocol_sha256'],initial_since=p.get('news_initial_since'),
                codal_sha256=p.get('source_files_sha256',{}).get('data_collector/codal.py')))
            previous=row['protocol_sha256']
            previous_time=registered
            for cycle in db.conn.execute('SELECT * FROM radar_cycles WHERE run_id=? ORDER BY id',(row['id'],)):
                if cycle_limit is not None and cycle['id']>cycle_limit:
                    continue
                report=json.loads(cycle['report_json'])
                at=stamp(cycle['recorded_at'])
                expected=digest(dict(previous=previous,report=digest(report),run_id=row['id'],recorded_at=cycle['recorded_at']))
                if (cycle['previous_sha256']!=previous or cycle['report_sha256']!=digest(report)
                        or cycle['chain_sha256']!=expected or at<previous_time or at>clock
                        or stamp(report['observed_at'])!=at or report.get('run_id')!=row['id']
                        or report.get('version')!=p['version']):
                    raise ValueError('Invalid or future radar predecessor cycle')
                events=[json.loads(r[0]) for r in db.conn.execute('SELECT payload_json FROM radar_attention_events '
                    'WHERE run_id=? AND cycle_id=? ORDER BY id',(row['id'],cycle['id']))]
                if events!=report['events']:
                    raise ValueError('Radar predecessor event ledger differs')
                cycles.append(dict(id=cycle['id'],run_id=row['id'],observed_at=cycle['recorded_at'],
                    report_sha256=cycle['report_sha256'],chain_sha256=cycle['chain_sha256'],
                    artifacts_folder=report.get('artifacts_folder'),artifacts_sha256={k:v for k,v in
                        report.get('artifacts_sha256',{}).items()if k=='news-source.json'},
                    coverage=report.get('news_coverage',{}),price_scan=report.get('price_scan'),
                    price_observed_at=report.get('price_observed_at')))
                previous=expected
                previous_time=at
    cycles.sort(key=lambda row:row['id'])
    if any(stamp(b['observed_at'])<stamp(a['observed_at'])for a,b in zip(cycles,cycles[1:])):
        raise ValueError('Global radar cycle clock rollback')
    return dict(runs=runs,cycles=cycles)


def predecessor_cycle_limit(db, *, run_limit, as_of):
    """Registrations cannot use a self-declared cutoff to omit an older debt."""
    clock=stamp(as_of)
    return max((r['id']for r in db.conn.execute('SELECT c.id,c.recorded_at FROM radar_cycles c '
        'JOIN radar_runs r ON r.id=c.run_id WHERE r.rowid<=?',(run_limit,))
        if stamp(r['recorded_at'])<=clock),default=0)


def _positive_news(root, run, cycle):
    coverage=cycle['coverage']
    if coverage.get('window_scan_satisfied') is not True:
        return dict(credited=False,reason='not_satisfied')
    folder=cycle['artifacts_folder']
    expected=cycle['artifacts_sha256'].get('news-source.json')
    if not folder or not expected:
        return dict(credited=False,reason='legacy_without_bound_source')
    owning=_path(root,run['id'])
    folder=_child(owning,str(_path(root,folder).relative_to(owning)))
    path=_child(folder,'news-source.json')
    if _sha(path)!=expected:
        raise ValueError('Historical news source fingerprint differs')
    source=read_private_report(path)
    proof=dict(source_file=str(path.relative_to(root)),source_sha256=expected)
    if source.get('version') not in ('news-window-v1','news-window-v2',LEGACY_NEWS_VERSION,NEWS_VERSION):
        return dict(proof,credited=False,reason='unsupported_news_contract')
    required=('window_start','checkpoint_through','window_scan_satisfied','head_recheck_stable',
              'boundary_reached','reached_end','errors','failed_page','page_limit_reached','invalid_rows','stop_reason')
    if any(coverage.get(k)!=source.get(k) for k in required):
        return dict(proof,credited=False,reason='coverage_source_differs')
    if (source.get('window_scan_satisfied') is not True or source.get('head_recheck_stable') is not True
            or not(source.get('boundary_reached')is True or source.get('reached_end')is True) or source.get('errors')!=[]
            or source.get('failed_page') is not None or source.get('page_limit_reached') is not False
            or type(source.get('invalid_rows')) is not int or source['invalid_rows']!=0
            or source.get('rate_limit_deferred') or source.get('stop_reason') not in ('boundary','end')):
        return dict(proof,credited=False,reason='incomplete_positive_evidence')
    try:
        start,requested,checkpoint=(stamp(source[k]) for k in ('window_start','requested_at','checkpoint_through'))
        observed=stamp(cycle['observed_at'])
        if not start<=requested==checkpoint<=observed or requested<stamp(run['registered_at']):
            raise ValueError('Invalid checkpoint chronology')
        pages=source['pages']; head=source['head_recheck']
        if (not isinstance(pages,list) or not pages or not isinstance(head,dict)
                or type(source.get('pages_fetched'))is not int or source['pages_fetched']!=len(pages)):
            raise ValueError('Missing page or stable-head evidence')
        legacy=False
        last_received=requested
        for i,page in enumerate([*pages,head]):
            number=1 if i==len(pages) else i+1
            if (type(page.get('requested_page'))is not int or page['requested_page']!=number or page.get('status')!='observed'
                    or type(page.get('invalid_rows')) is not int or page['invalid_rows']!=0
                    or not isinstance(page.get('letters'),list) or type(page.get('raw_count')) is not int
                    or page['raw_count']!=len(page['letters'])
                    or type(page.get('response_bytes')) is not int or not 0<page['response_bytes']<=1048576
                    or any(type(page.get(k))is not int or page[k]<0 for k in ('total_claim','page_count_claim'))
                    or page['total_claim']<page['raw_count']
                    or not isinstance(page.get('response_sha256'),str)
                    or not re.fullmatch('[0-9a-f]{64}',page['response_sha256'])):
                raise ValueError('Incomplete native page evidence')
            sent,received=stamp(page['requested_at']),stamp(page['received_at'])
            if not requested<=sent<=received<=observed or sent<last_received:
                raise ValueError('Invalid page chronology')
            last_received=received
            if 'url' in page or 'http_status' in page:
                if page.get('url')!=GLOBAL_FEED+str(number) or type(page.get('http_status'))is not int or page['http_status']!=200:
                    raise ValueError('Filtered, different or failed news feed')
            else:
                legacy=True
        if legacy:
            code=_child(owning,'code/data_collector/codal.py')
            if run['codal_sha256'] not in LEGACY_GLOBAL_TRANSPORT or _sha(code)!=run['codal_sha256']:
                raise ValueError('Legacy global transport not attested')
        replay=NewsWindow(since=source['window_start'],requested_at=source['requested_at'],
                          version=NEWS_VERSION if source['version']==NEWS_VERSION else LEGACY_NEWS_VERSION)
        for page in pages:
            replay.add(page)
        rebuilt=replay.result(head_recheck=head,stop_reason=source['stop_reason'])
        if not rebuilt['window_scan_satisfied'] or any(rebuilt[k]!=source[k] for k in required
                if k not in ('boundary_reached','reached_end')):
            raise ValueError('Positive window cannot be reproduced')
        if any(rebuilt[k]!=source.get(k)for k in ('letters','pages_fetched','oldest_published_at','newest_published_at')):
            raise ValueError('News aggregation omitted or changed observed notices')
    except (KeyError,TypeError,ValueError,OverflowError) as error:
        return dict(proof,credited=False,reason='invalid_positive_evidence',error_type=type(error).__name__)
    return dict(proof,credited=True,reason='legacy_code_attested_global_feed' if legacy else 'explicit_global_feed')


def news_boundary(root, db, *, as_of, overlap_seconds=7200, run_limit=None, cycle_limit=None):
    """Fold debt in event order; a later narrow success cannot bridge an older gap."""
    if type(overlap_seconds)is not int or overlap_seconds<0:
        raise ValueError('Invalid news overlap')
    rows=inventory(db,as_of=as_of,run_limit=run_limit,cycle_limit=cycle_limit)
    runs={r['id']:r for r in rows['runs']}
    events=[(stamp(r['registered_at']),0,r['ordinal'],r)for r in rows['runs']]
    events.extend((stamp(c['observed_at']),1,c['id'],c)for c in rows['cycles'])
    debt=None;decisions=[]
    for _,kind,_,item in sorted(events,key=lambda e:e[:3]):
        candidate=item.get('initial_since')if kind==0 else item['coverage'].get('window_start')
        if candidate is None:
            decisions.append(dict(kind='registration'if kind==0 else 'cycle',id=item.get('id'),
                                  run_id=item.get('run_id',item.get('id')),reason='unsupported_legacy_boundary'))
            continue
        since=stamp(candidate)
        event_at=stamp(item['registered_at']if kind==0 else item['observed_at'])
        if since>event_at:
            raise ValueError('News predecessor boundary is in the future')
        before=debt
        debt=min(debt,since)if debt else since
        proof=dict(credited=False,reason='registration_retains_or_widens_debt')
        if kind:
            proof=_positive_news(root,runs[item['run_id']],item)
            if proof['credited']:
                if since<=debt:
                    # Do not jump to an older max frontier after debt was widened.
                    debt=max(debt,stamp(item['coverage']['checkpoint_through'])-timedelta(seconds=overlap_seconds))
                else:
                    proof=dict(proof,credited=False,reason='narrow_success_does_not_cover_debt')
        decisions.append(dict(kind='registration'if kind==0 else 'cycle',id=item.get('id'),
            run_id=item.get('run_id',item.get('id')),before=before.isoformat()if before else None,
            candidate=since.isoformat(),after=debt.isoformat(),**proof))
    if debt is None:
        debt=stamp(as_of)-timedelta(days=1)
    return dict(version=VERSION,as_of=stamp(as_of).isoformat(),overlap_seconds=overlap_seconds,
        required_since=debt.isoformat(),inventory=rows,inventory_sha256=digest(rows),decisions=decisions,
        event_order='aware event time; registration before cycle; table ordinal breaks equal-time ties',
        complete_coverage_verified=False,externally_timestamped=False)


def attempt_intent(root, protocol, folder, *, attempted_at, scope_count):
    """Registered history-call intent; neither HTTP delivery nor successful data."""
    folder=_child(_path(root,protocol['id']),str(folder.relative_to(root/protocol['id'])))
    request_path=_child(folder,'request.json')
    request=read_private_report(request_path)
    if request.get('run_id')!=protocol['id'] or not request.get('price_scan'):
        raise ValueError('History intent requires its registered price request')
    if stamp(attempted_at)<stamp(request['registered_at']):
        raise ValueError('History attempt predates its request')
    return dict(version=PRICE_VERSION,run_id=protocol['id'],cycle_folder=str(folder.relative_to(root)),
        attempted_at=stamp(attempted_at).isoformat(),request_sha256=_sha(request_path),
        protocol_sha256=digest(protocol),scope_count=scope_count,last_n=100,
        intent_only=True,http_delivery_verified=False)


def price_budget(root, db, *, as_of, retry_seconds, run_limit=None, cycle_limit=None, partial_refs=None,
                 daily_limit=None):
    """Derive a cross-run interval and optional daily limit from retained attempts.

    A completed cycle and its intent are the same attempt. A partial intent also
    consumes the limit because dispatch cannot be disproved after a crash.
    Legacy receipts with no intent are counted from their completion time.
    """
    if type(retry_seconds)is not int or retry_seconds<0:
        raise ValueError('Invalid history retry interval')
    if daily_limit is not None and (type(daily_limit)is not int or daily_limit<1):
        raise ValueError('Invalid daily history attempt limit')
    clock=stamp(as_of);rows=inventory(db,as_of=as_of,run_limit=run_limit,cycle_limit=cycle_limit)
    attempts=[];attempt_days={};completed_folders=set()
    for cycle in rows['cycles']:
        if cycle['price_scan']is True:
            at=stamp(cycle['price_observed_at'])
            if at>stamp(cycle['observed_at']):
                raise ValueError('Price completion is after its recorded cycle')
            attempts.append(dict(kind='completed_price_scan',at=at.isoformat(),run_id=cycle['run_id'],
                                 cycle_id=cycle['id'],chain_sha256=cycle['chain_sha256']))
            if daily_limit is not None:
                folder=cycle.get('artifacts_folder')
                key=(cycle['run_id'],folder)if isinstance(folder,str) and folder else (cycle['run_id'],cycle['id'])
                if key in completed_folders:
                    raise ValueError('Multiple completed price cycles claim one artifact folder')
                completed_folders.add(key)
                attempt_days.setdefault(key,set()).add(at.date())
    for run in rows['runs']:
        paths=(sorted(_path(root,run['id']).glob('CYCLE-*/price-attempt.json')) if partial_refs is None else
               [_path(root,ref['file'])for ref in partial_refs if ref['run_id']==run['id']])
        for path in paths:
            owning=_path(root,run['id'])
            path=_child(owning,str(path.relative_to(owning)))
            if partial_refs is not None and not any(ref['file']==str(path.relative_to(root)) and
                    ref['sha256']==_sha(path) for ref in partial_refs):
                raise ValueError('Recorded price intent changed')
            value=read_private_report(path)
            at=stamp(value['attempted_at'])
            if at>clock:
                raise ValueError('Future history attempt or clock rollback; retry blocked')
            request_path=_child(path.parent,'request.json')
            request=read_private_report(request_path)
            if (value.get('version')!=PRICE_VERSION or value.get('run_id')!=run['id']
                    or value.get('cycle_folder')!=str(path.parent.relative_to(root))
                    or value.get('protocol_sha256')!=run['protocol_sha256']
                    or value.get('request_sha256')!=_sha(request_path)
                    or value.get('intent_only')is not True or value.get('http_delivery_verified')is not False
                    or type(value.get('last_n'))is not int or value['last_n']!=100
                    or type(value.get('scope_count'))is not int or value['scope_count']<0
                    or request.get('run_id')!=run['id'] or request.get('price_scan')is not True
                    or not stamp(run['registered_at'])<=stamp(request['registered_at'])<=at):
                raise ValueError('History attempt is not bound to its registered request')
            attempts.append(dict(kind='registered_intent_only',at=at.isoformat(),run_id=run['id'],
                                 file=str(path.relative_to(root)),sha256=_sha(path)))
            if daily_limit is not None:
                key=(run['id'],str(path.parent.relative_to(root)))
                attempt_days.setdefault(key,set()).add(at.date())
    last=max(attempts,key=lambda row:stamp(row['at']),default=None)
    until=stamp(last['at'])+timedelta(seconds=retry_seconds)if last else None
    result=dict(version=PRICE_VERSION,as_of=clock.isoformat(),retry_seconds=retry_seconds,
        last_attempt=last,not_before=until.isoformat()if until else None,
        deferred=until is not None and clock<until,attempts=attempts,
        history_calls_only=True,market_watch_requests_not_gated=True)
    if daily_limit is not None:
        count=sum(clock.date() in days for days in attempt_days.values())
        capped=count>=daily_limit
        if capped:
            midnight=clock.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=1)
            until=max(until,midnight)if until else midnight
        result.update(daily_limit=daily_limit,attempts_today=count,daily_limit_reached=capped,
                      interval_deferred=result['deferred'],
                      not_before=until.isoformat()if until else None,
                      deferred=until is not None and clock<until)
    return result
