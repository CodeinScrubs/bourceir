"""Bounded dated public-price production; no database, orders, or invented stock IDs."""

import hashlib
import os
import re
import sys
import threading
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta

from config import settings
from core.daily_publications import _clock
from core.source_cooldown import VERSION as SOURCE_VERSION
from core.source_cooldown import checked_backoff, deferred
from core.text import normalize_symbol
from core.timeutils import is_trading_session, now_tehran, now_tehran_iso
from data_collector.daily_publications import DailyPublicationArchive
from data_collector.publication_cache import _child, _json, _no_reparse, _scope, capture_cache, replay_cache
from data_collector.source_cooldown import source_status
from ops.report_files import write_private_report

VERSION = 'radar-dated-producer-v1'
POLICY = dict(version=VERSION,after='13:15',workers=2,max_daily_captures=3,retry_seconds=900,
              stop_on_source_limit=True,consecutive_failure_limit=5,
              observed_zero_is_not_full_session_finality=True,
              database_writes=False,synthetic_symbol_ids=False)
_LOCAL_LOCK = threading.RLock()
INTENT = re.compile(r'PUBLISH-[0-9a-f]{32}\.json')


@contextmanager
def _lock(root):
    with _LOCAL_LOCK, (root/'publisher.lock').open('a+b') as stream:
        if stream.seek(0,os.SEEK_END)==0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if sys.platform.startswith('win'):
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except (BlockingIOError,PermissionError):
            yield False
            return
        try:
            yield True
        finally:
            stream.seek(0)
            if sys.platform.startswith('win'):
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def _bytes(folder,name,body):
    with (folder/name).open('xb') as stream:
        stream.write(body); stream.flush(); os.fsync(stream.fileno())


def _intent(value, *, day, at):
    if (not isinstance(value,dict) or set(value) != {'version','id','registered_at','session_date','policy','tasks','source_code_sha256'}
            or value['version'] != VERSION or value['policy'] != POLICY
            or not isinstance(value['id'],str) or not re.fullmatch(r'PUBLISH-[0-9a-f]{32}',value['id'])
            or value['session_date'] != day or _clock(value['registered_at']).date().isoformat() != day
            or _clock(value['registered_at']) > _clock(at)
            or not isinstance(value['source_code_sha256'],dict) or not value['source_code_sha256']
            or any(not isinstance(k,str) or not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for k,v in value['source_code_sha256'].items())):
        raise ValueError('Invalid preserved daily producer intent')
    from data_collector.daily_publications import _tasks
    _tasks(value['tasks'],value['registered_at'])
    if any(t['symbol_id'] is not None or t['kind']!='price' or t['date']!=day for t in value['tasks']):
        raise ValueError('Producer intents are evidence-only ordinary prices with no database IDs')
    return value


def _decision(snapshot,read_file,scope,prior,gate,*,day,at,eligible):
    if (not isinstance(gate,dict) or set(gate)!={'version','last_admitted_at','backoff'} or gate['version']!=SOURCE_VERSION
            or _clock(snapshot['observed_at'])>_clock(at)):
        raise ValueError('Producer source-clock evidence differs')
    checked_backoff(gate['backoff'])
    if gate['last_admitted_at'] is not None and _clock(gate['last_admitted_at'])>_clock(at):
        raise ValueError('Producer source admission is in the future')
    receipts = replay_cache(snapshot,read_file,scope,day,observed_at=snapshot['observed_at'])
    observed = {code for code,items in receipts.items() if items}
    pending = [item['ins_code'] for item in scope if item['ins_code'] not in observed]
    for value in prior:
        _intent(value,day=day,at=at)
    if len({v['id'] for v in prior}) != len(prior):
        raise ValueError('Repeated producer intent cannot reset the daily budget')
    last = max((_clock(v['registered_at']) for v in prior),default=None)
    wait = None if last is None else (last+timedelta(seconds=POLICY['retry_seconds'])).isoformat()
    if not eligible:
        mode = 'outside_registered_session_boundary'
    elif deferred(gate['backoff'],at=at):
        mode = 'source_cooldown'
    elif not pending:
        mode = 'no_unobserved_price_tasks'
    elif len(prior) >= POLICY['max_daily_captures']:
        mode = 'daily_budget_exhausted'
    elif wait is not None and _clock(at) < _clock(wait):
        mode = 'producer_retry_cooldown'
    else:
        mode = 'capture'
    return dict(mode=mode,pending_codes=pending,observed_codes=sorted(observed),
                prior_intents=len(prior),retry_not_before=wait)


def produce(folder,scope,protocol,*,provider,provider_factory=None,progress=None,clock=None):
    """Retain a frozen before-image, reserve the daily attempt, then capture missing keys."""
    _scope(scope)
    if protocol.get('publication_producer_policy') != POLICY:
        raise ValueError('The registered producer policy differs')
    clock = now_tehran() if clock is None else clock
    at,day = clock.isoformat(),clock.date().isoformat()
    if getattr(provider,'supports_dated_publications',False) is not True:
        value=dict(version=VERSION,policy=POLICY,provider_supports_dated=False,observed_at=at,
            mode='unsupported_provider',scope_codes=[r['ins_code']for r in scope],
            database_writes=False,accepted_for_signals=False)
        write_private_report(folder/'publication-producer.json',value)
        return value
    snapshot,bodies = capture_cache(scope,day,observed_at=at)
    for name,body in bodies.items():
        _bytes(folder,'producer-before-'+name,body)
    write_private_report(folder/'producer-before-snapshot.json',snapshot)
    gate = source_status()
    at = now_tehran_iso()
    if _clock(at).date().isoformat()!=day:
        raise ValueError('Producer session changed while capturing its before-image')
    root = settings.DATA_DIR/'private'/'radar-publication-producer'/day
    for path in (settings.DATA_DIR,settings.DATA_DIR/'private',root.parent,root,root/'publisher.lock'):
        _no_reparse(path)
    root.mkdir(parents=True,exist_ok=True)
    with _lock(root) as acquired:
        if not acquired:
            raise ValueError('Another daily publisher owns the producer admission lock')
        prior,files = [],[]
        for path in sorted(root.glob('PUBLISH-*.json')):
            if not INTENT.fullmatch(path.name):
                raise ValueError('Unknown producer intent path')
            path=_child(root,path.name)
            with path.open('rb') as stream:
                body = stream.read(4*1024*1024+1)
            if len(body)>4*1024*1024:
                raise ValueError('Producer intent exceeds its byte budget')
            value = _intent(_json(body),day=day,at=at)
            if path.name!=value['id']+'.json':
                raise ValueError('Producer intent identity differs from its filename')
            name='producer-prior-'+path.name
            _bytes(folder,name,body)
            prior.append(value); files.append(name)
            if len(prior)>POLICY['max_daily_captures']:
                raise ValueError('Preserved daily producer intent count exceeds its policy')
        eligible = is_trading_session(clock,protocol['calendar_exceptions']) and clock.strftime('%H:%M')>=POLICY['after']
        decision = _decision(snapshot,bodies.__getitem__,scope,prior,gate,day=day,at=at,eligible=eligible)
        receipt = dict(version=VERSION,session_date=day,observed_at=at,registered_at=protocol['registered_at'],
            policy=POLICY,provider_supports_dated=True,source_gate=gate,prior_intent_files=files,eligible_session=eligible,decision=decision,
            created_intent=None,created_archive_id=None,archive_statuses=None,
            database_writes=False,accepted_for_signals=False)
        if decision['mode']=='capture':
            by_code={item['ins_code']:item for item in scope}
            tasks=[dict(symbol_id=None,symbol=normalize_symbol(by_code[code]['symbol_fa']),ins_code=code,
                instrument_type_code=by_code[code]['instrument_type_code'],date=day,kind='price')for code in decision['pending_codes']]
            intent=dict(version=VERSION,id='PUBLISH-'+uuid.uuid4().hex,registered_at=now_tehran_iso(),
                session_date=day,policy=POLICY,tasks=tasks,source_code_sha256=protocol['source_files_sha256'])
            _intent(intent,day=day,at=intent['registered_at'])
            target=_child(root,intent['id']+'.json')
            if target.exists():
                raise ValueError('Producer intent cannot overwrite an earlier reservation')
            write_private_report(target,intent)
            write_private_report(folder/'producer-current-intent.json',intent)
            receipt['created_intent']=intent['id']
            archive=DailyPublicationArchive(settings.DATA_DIR/'private'/'daily-publications',tasks,workers=POLICY['workers'])
            records=archive.capture(provider,provider_factory=provider_factory,progress=progress)
            archive.finish(dict(evidence_only=True,database_writes=False,synthetic_symbol_ids=False))
            receipt['created_archive_id']=archive.folder.name
            receipt['archive_statuses']=dict(Counter(r['parsed']['status']for r in records))
        write_private_report(folder/'publication-producer.json',receipt)
        return receipt


def replay_producer(folder,scope,protocol,*,allowed_files,observed_at):
    """Reproduce source/budget/pending-key decisions entirely from sealed cycle members."""
    def read(name):
        path=(folder/name).resolve()
        if name not in allowed_files or not path.is_relative_to(folder.resolve()):
            raise ValueError('Unbound producer evidence member')
        with path.open('rb') as stream:
            body=stream.read(4*1024*1024+1)
        if len(body)>4*1024*1024 or hashlib.sha256(body).hexdigest()!=allowed_files[name]:
            raise ValueError('Producer evidence hash or byte budget differs')
        return body
    value=_json(read('publication-producer.json'))
    if value.get('provider_supports_dated') is False:
        expected=dict(version=VERSION,policy=POLICY,provider_supports_dated=False,observed_at=value.get('observed_at'),
            mode='unsupported_provider',scope_codes=[r['ins_code']for r in scope],database_writes=False,accepted_for_signals=False)
        if (value!=expected or _clock(value['observed_at'])>_clock(observed_at)
                or any(name.startswith(('producer-before-','producer-prior-','producer-current-'))for name in allowed_files)):
            raise ValueError('Unsupported producer cannot claim a native capture')
        return value
    if (value.get('version')!=VERSION or value.get('policy')!=POLICY
            or protocol.get('publication_producer_policy')!=POLICY
            or value.get('registered_at')!=protocol['registered_at']
            or value.get('accepted_for_signals') is not False or value.get('database_writes') is not False):
        raise ValueError('Producer receipt differs from the registered policy')
    snapshot=_json(read('producer-before-snapshot.json'))
    prior=[_json(read(name))for name in value['prior_intent_files']]
    clock=_clock(value['observed_at'])
    eligible=is_trading_session(clock,protocol['calendar_exceptions'])and clock.strftime('%H:%M')>=POLICY['after']
    expected=_decision(snapshot,lambda name:read('producer-before-'+name),scope,prior,value['source_gate'],
                       day=value['session_date'],at=value['observed_at'],eligible=eligible)
    if value['decision']!=expected or value['eligible_session']!=eligible:
        raise ValueError('Producer decision differs from its frozen input and budget')
    if expected['mode']=='capture':
        intent=_intent(_json(read('producer-current-intent.json')),day=value['session_date'],at=observed_at)
        desired=[dict(symbol_id=None,symbol=normalize_symbol(item['symbol_fa']),ins_code=item['ins_code'],instrument_type_code=item['instrument_type_code'],
                      date=value['session_date'],kind='price')for item in scope if item['ins_code']in expected['pending_codes']]
        if (intent['tasks']!=desired or intent['source_code_sha256']!=protocol['source_files_sha256']
                or intent['id']!=value['created_intent'] or not isinstance(value['created_archive_id'],str)
                or not re.fullmatch(r'DAYPUB-[0-9a-f]{32}',value['created_archive_id'])):
            raise ValueError('Producer scope or code identity differs')
        after=_json(read('dated-cache-source.json'))
        archive=after['archives'].get(value['created_archive_id'])
        if archive is None:
            raise ValueError('Produced archive was not bound to the cycle input')
        plan=_json(read(archive['plan_file']))
        if plan['tasks']!=desired or _clock(plan['registered_at'])<_clock(intent['registered_at']):
            raise ValueError('Produced native archive differs from its reserved tasks')
        statuses=Counter(row['status']for row in after['records']if row['archive_id']==value['created_archive_id'])
        if dict(statuses)!=value['archive_statuses']:
            raise ValueError('Produced native outcome denominator differs')
    elif any(value[k]is not None for k in ('created_intent','created_archive_id','archive_statuses')):
        raise ValueError('A deferred/no-work producer cannot claim a new capture')
    return value
