"""داده مصنوعی فقط برای آزمون؛ هیچ ردیف/بایت آزمایشی در DB عملیاتی ذخیره نمی‌شود."""

import copy
import hashlib
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core.capture_evidence import binding_problems, receipts_from_cycle
from core.quote_context import (
    MAX_JSON_BYTES,
    VERSION,
    archive_source,
    context_requests,
    parse_context,
    parse_source,
    restore_source,
)
from core.risk import intraday_entry_constraints
from data_collector.scheduler import DataCollector
from data_collector.tsetmc import TsetmcProvider
from database.capture import CaptureRepository
from ops.intraday_audit import audit_intraday
from tests.test_dashboard import dashboard_db as dashboard_db
from tests.test_dividend_evidence import Response
from tests.test_legacy_quotes import observation, packet

DAY='2026-10-14'
CLOCK='2026-10-14T10:00:01+03:30'


def metadata():
    return dict(instrumentInfo=dict(insCode='123',dEven=20261014,lVal18AFC='آزمون',yVal='300',baseVol=1,
        staticThreshold=dict(insCode=None,dEven=0,hEven=0,psGelStaMin=970.,psGelStaMax=1030.)))


def limits():
    return dict(staticThreshold=[dict(insCode='0',dEven=20261013,hEven=1,psGelStaMin=950.,psGelStaMax=1010.),
        dict(insCode='0',dEven=20261014,hEven=60126,psGelStaMin=970.,psGelStaMax=1030.)])


def native(payload,url):
    content=json.dumps(payload,ensure_ascii=False,allow_nan=False).encode()
    return dict(source='tsetmc',ins_code='123',url=url,requested_at=CLOCK,received_at=CLOCK,
                content=content,response_bytes=len(content),body_sha256=hashlib.sha256(content).hexdigest())


def bundle(*,info=None,thresholds=None,quote=None):
    i,t=context_requests('123',DAY)
    return dict(version=VERSION,ins_code='123',requested_at=CLOCK,received_at=CLOCK,
        quote=observation() if quote is None else quote,
        instrument=native(metadata() if info is None else info,i),
        limits=native(limits() if thresholds is None else thresholds,t))


def test_native_same_day_limits_are_selected_after_previous_seed_and_corroborated_by_named_identity():
    parsed=parse_context(bundle(),'آزمون')
    assert (parsed['price_limit_down'],parsed['price_limit_up'])==(970,1030)
    assert parsed['base_volume']==1 and parsed['buy_queue_volume']==parsed['sell_queue_volume']==0
    assert parsed['real_buy_count'] is None and parsed['bid_price_2'] is None
    blockers,caps=intraday_entry_constraints(parsed,1001,at=datetime.fromisoformat(CLOCK))
    assert not blockers and min(caps.values())>0
    # عبور قیود محاسباتی، اجرای سفارش یا ساعت مستقل عمق را ثابت نمی‌کند.
    archived=archive_source(bundle(),'dated-quote-context')
    assert 'content' not in archived['instrument'] and 'cookies' not in archived['instrument']
    assert parse_context(restore_source(archived,'dated-quote-context'),'آزمون')==parsed


def test_unsorted_rows_use_latest_same_day_record_and_do_not_use_future_or_previous_values():
    rows=limits()
    rows['staticThreshold'] += [dict(insCode='0',dEven=20261014,hEven=90000,psGelStaMin=970,psGelStaMax=1020)]
    info=metadata()
    info['instrumentInfo']['staticThreshold']['psGelStaMax']=1020
    rows['staticThreshold'].reverse()
    assert parse_context(bundle(info=info,thresholds=rows),'آزمون')['price_limit_up']==1020
    rows['staticThreshold'][0]['hEven']=100001
    with pytest.raises(ValueError,match='پس از زمان قیمت'):
        parse_context(bundle(info=info,thresholds=rows),'آزمون')


@pytest.mark.parametrize('changes',[
    dict(insCode='999'),dict(insCode=True),dict(dEven=0),dict(dEven=20261013),dict(dEven=True),
    dict(yVal='305'),dict(yVal=True),dict(lVal18AFC='دیگری'),dict(baseVol=True),dict(baseVol=-1),dict(baseVol=10**1000),
])
def test_missing_conflicting_identity_date_type_and_invalid_base_volume_cannot_enrich_quote(changes):
    info=metadata()
    info['instrumentInfo'].update(changes)
    with pytest.raises(ValueError):
        parse_context(bundle(info=info),'آزمون')


@pytest.mark.parametrize('changes',[
    dict(insCode='999'),dict(insCode=True),dict(dEven=0),dict(dEven=20261015),dict(dEven=True),
    dict(hEven=True),dict(hEven='1000001'),dict(hEven=240000),dict(hEven='۰۶۰۰۰۰'),
    dict(psGelStaMin=True),dict(psGelStaMax='1030'),dict(psGelStaMin=0),dict(psGelStaMin=1040),
])
def test_invalid_native_threshold_row_is_not_replaced_by_a_good_row_or_metadata(changes):
    rows=limits()
    rows['staticThreshold'][1].update(changes)
    with pytest.raises(ValueError):
        parse_context(bundle(thresholds=rows),'آزمون')


@pytest.mark.parametrize('kind',['previous_only','duplicate','ambiguous','mismatch','inner_date','inner_boolean'])
def test_corroboration_rejects_previous_seed_duplicates_conflicts_and_same_day_numeric_mismatch(kind):
    rows,info=limits(),metadata()
    if kind=='previous_only': rows['staticThreshold']=rows['staticThreshold'][:1]
    elif kind in ('duplicate','ambiguous'):
        extra=copy.deepcopy(rows['staticThreshold'][1])
        if kind=='ambiguous': extra['psGelStaMax']=1050
        rows['staticThreshold'].append(extra)
    elif kind=='mismatch': info['instrumentInfo']['staticThreshold']['psGelStaMax']=1040
    elif kind=='inner_date': info['instrumentInfo']['staticThreshold']['dEven']=20261013
    else: info['instrumentInfo']['staticThreshold']['dEven']=False
    with pytest.raises(ValueError):
        parse_context(bundle(info=info,thresholds=rows),'آزمون')


def test_wrong_metadata_day_is_rejected_before_any_financial_value_is_consumed(monkeypatch):
    import core.quote_context as context

    info=metadata()
    info['instrumentInfo']['dEven']=20261013
    monkeypatch.setattr(context,'_number',lambda *_a,**_k:pytest.fail('wrong day reached financial values'))
    with pytest.raises(ValueError,match='هم‌روز'):
        parse_context(bundle(info=info),'آزمون')


@pytest.mark.parametrize('kind',['partial','reordered','too_long','wrong_day','changed_body','changed_hash',
                                'duplicate_keys','nonfinite','overflow_float','missing_body'])
def test_partial_tampered_or_noncontemporaneous_context_never_falls_back_to_legacy_quote(kind):
    source=bundle()
    if kind=='partial': source['instrument']=None
    elif kind=='reordered': source['instrument']['requested_at']='2026-10-14T09:59:59+03:30'
    elif kind=='too_long': source['received_at']='2026-10-14T10:00:32+03:30'
    elif kind=='wrong_day': source['received_at']='2026-10-15T10:00:01+03:30'
    elif kind=='changed_body': source['instrument']['content']+=b' '
    elif kind=='changed_hash': source['instrument']['body_sha256']='0'*64
    elif kind=='missing_body': source['limits']['content']=None
    else:
        content=(b'{"instrumentInfo":{},"instrumentInfo":{}}' if kind=='duplicate_keys' else
                 b'{"x":1e999}' if kind=='overflow_float' else b'{"x":NaN}')
        source['instrument'].update(content=content,response_bytes=len(content),body_sha256=hashlib.sha256(content).hexdigest())
    with pytest.raises(ValueError):
        parse_context(source,'آزمون')


def test_unknown_sources_and_versions_cannot_select_a_parser_by_payload():
    with pytest.raises(ValueError): parse_source(bundle(),'آزمون','unregistered')
    source=bundle()
    source['version']='next-version'
    with pytest.raises(ValueError): parse_context(source,'آزمون')
    with pytest.raises(ValueError): restore_source({},'dated-quote-context')


def test_missing_depth_and_explicit_zero_remain_distinct_and_boundary_queue_blocks_entry():
    absent=parse_context(bundle(quote=observation(packet(orders=''))),'آزمون')
    assert absent['buy_queue_volume'] is None and absent['sell_queue_volume'] is None
    empty=parse_context(bundle(quote=observation(packet(orders='0@0@0@0@0@0,'))),'آزمون')
    assert empty['buy_queue_volume']==empty['sell_queue_volume']==0
    queued=parse_context(bundle(quote=observation(packet(orders='2@400@1030@1030@500@3,'))),'آزمون')
    assert queued['buy_queue_volume']==400
    assert intraday_entry_constraints(queued,1030,at=datetime.fromisoformat(CLOCK))[0]


@pytest.mark.parametrize('orders',['2@400@1031@1031@500@3,','2@400@1002@1001@500@3,'])
def test_out_of_range_or_crossed_book_is_rejected(orders):
    with pytest.raises(ValueError): parse_context(bundle(quote=observation(packet(orders=orders))),'آزمون')


class SequenceSession:
    def __init__(self,responses):
        self.responses=list(responses)
        self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        response=self.responses.pop(0)
        response.url=response.supplied_url or url
        return response


@pytest.fixture
def context_clock(monkeypatch):
    clock=SimpleNamespace(at=datetime.fromisoformat(CLOCK))
    for module in ('data_collector.tsetmc','data_collector.scheduler','database.capture'):
        monkeypatch.setattr(module+'.now_tehran_iso',lambda:clock.at.isoformat())
    return clock


def provider():
    return TsetmcProvider(session=SequenceSession([Response(body=packet()),
        Response(body=json.dumps(metadata()).encode()),Response(body=json.dumps(limits()).encode())]))


def test_provider_preserves_three_bodies_with_fixed_https_urls_and_no_redirects(context_clock):
    source=provider()
    result=source.get_quote_context_observation('123')
    assert parse_context(result,'آزمون')['price_limit_up']==1030
    assert len(source.session.calls)==3 and all(not c[1]['allow_redirects'] for c in source.session.calls)
    assert [c[0] for c in source.session.calls[1:]]==list(context_requests('123',DAY))
    assert all(c[0].startswith('https://') for c in source.session.calls)


@pytest.mark.parametrize('response',[Response(body=b'x'*(MAX_JSON_BYTES+1)),Response(status=206),
                                   Response(status=302),Response(url='https://example.org/'),
                                   Response(body=b'<html>captcha</html>')])
def test_native_response_errors_remain_partial_archived_context(context_clock,response):
    source=provider()
    source.session.responses[1]=response
    result=source.get_quote_context_observation('123')
    assert result['instrument'] is None and result['quote']['content']
    assert archive_source(result,'dated-quote-context')['quote']['body_base64']
    with pytest.raises(ValueError): parse_context(result,'آزمون')


def context_cycle(db,source):
    scope=db.get_universe_symbols()
    repo=CaptureRepository(db)
    run=repo.start(scope,interval=30,cycles=1,forced=False,provider='tsetmc',explicit_scope=True,
                   quote_source='dated-quote-context')
    repo.append(run,1,'poll_started')
    result=source.collect_market_snapshot(instruments=scope,quote_source='dated-quote-context')
    repo.append(run,1,'poll_finished',dict(status=result['status'],coverage=result['coverage']))
    return repo,run,result


def test_collector_and_receipt_replay_all_three_bodies_and_never_turn_bound_context_into_fill(db,context_clock):
    db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    source=DataCollector(db=db,provider=provider())
    repo,_,result=context_cycle(db,source)
    assert result['status']=='success' and result['coverage']['valid']==1
    snapshot=dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    receipt=receipts_from_cycle(repo.latest_cycles([snapshot['symbol_id']])[0])[snapshot['symbol_id']]
    assert not binding_problems(snapshot,receipt,CLOCK)
    assert not intraday_entry_constraints(snapshot,1001,at=context_clock.at)[0]
    checked=audit_intraday(db,DAY)
    assert checked['observable_ask_rows']==1 and checked['capture']['bound_snapshot_receipts']==1
    assert not checked['own_fill_calibration_available'] and not checked['independent_book_timestamp_available']
    raw=receipt['observation']['source_observation']['instrument']
    changed=metadata()
    changed['instrumentInfo']['staticThreshold']['psGelStaMax']=1040
    content=json.dumps(changed).encode()
    import base64
    raw.update(body_base64=base64.b64encode(content).decode(),response_bytes=len(content),body_sha256=hashlib.sha256(content).hexdigest())
    assert 'quote_source_evidence_invalid' in binding_problems(snapshot,receipt,CLOCK)


def test_failed_context_is_rejected_with_legacy_body_retained_and_no_snapshot_written(db,context_clock):
    db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    source=provider()
    source.session.responses[1]=Response(status=500)
    _,_,result=context_cycle(db,DataCollector(db=db,provider=source))
    assert result['coverage']['rejected']==1 and result['status']=='failed'
    assert result['coverage']['per_instrument'][0]['source_observation']['quote']['body_base64']
    assert db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==0


def test_unchanged_context_rebinds_original_row_and_later_failure_does_not_reuse_old_success(db,context_clock):
    db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    source=DataCollector(db=db,provider=provider())
    repo,run,_=context_cycle(db,source)
    context_clock.at+=timedelta(seconds=20)
    source.provider=provider()
    repo.append(run,2,'poll_started')
    result=source.collect_market_snapshot(instruments=db.get_universe_symbols(),quote_source='dated-quote-context')
    repo.append(run,2,'poll_finished',dict(status=result['status'],coverage=result['coverage']))
    assert result['unchanged_skipped']==1 and db.conn.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0]==1
    snapshot=dict(db.conn.execute('SELECT * FROM snapshots').fetchone())
    receipt=receipts_from_cycle(repo.latest_cycles([snapshot['symbol_id']])[0])[snapshot['symbol_id']]
    assert not binding_problems(snapshot,receipt,context_clock.at.isoformat())
    context_clock.at+=timedelta(seconds=1)
    source.provider=provider()
    source.provider.session.responses[2]=Response(status=500)
    repo.append(run,3,'poll_started')
    failed=source.collect_market_snapshot(instruments=db.get_universe_symbols(),quote_source='dated-quote-context')
    repo.append(run,3,'poll_finished',dict(status=failed['status'],coverage=failed['coverage']))
    receipt=receipts_from_cycle(repo.latest_cycles([snapshot['symbol_id']])[0])[snapshot['symbol_id']]
    assert receipt['cycle']==3 and 'capture_observation_unavailable' in binding_problems(snapshot,receipt,context_clock.at.isoformat())


def test_unbounded_or_wrong_type_scope_is_rejected_before_any_network(db,context_clock):
    source=DataCollector(db=db,provider=SimpleNamespace(get_quote_context_observation=lambda *_:pytest.fail('network')))
    with pytest.raises(ValueError): source.collect_market_snapshot(quote_source='dated-quote-context')
    db.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=305))
    with pytest.raises(ValueError): source.collect_market_snapshot(instruments=db.get_all_symbols(),quote_source='dated-quote-context')


def test_dashboard_passes_new_mode_and_requires_explicit_scope_without_real_process(dashboard_db,monkeypatch):
    from streamlit.testing.v1 import AppTest

    from database.db import DatabaseManager
    from tests.test_dashboard import DASHBOARD

    calls=[]
    monkeypatch.setattr('ops.jobs.start_job',lambda name,args:calls.append((name,args)) or True)
    with DatabaseManager(db_path=dashboard_db) as database:
        database.upsert_symbol(dict(symbol_fa='آزمون',ins_code='123',instrument_type_code=300))
    app=AppTest.from_file(DASHBOARD,default_timeout=60).run()
    next(w for w in app.selectbox if w.label=='منبع پایش').set_value('قیمت با دامنه هم‌روز (آزمایشی)').run()
    assert not app.exception
    assert next(b for b in app.button if b.label=='شروع پایش زنده').disabled
    next(w for w in app.multiselect if w.label=='نمادهای پایش تاریخ‌دار').set_value(['آزمون']).run()
    next(b for b in app.button if b.label=='شروع پایش زنده').click().run()
    assert not app.exception and calls==[('live',['live','--quote-source','dated-quote-context','--symbols','آزمون'])]
