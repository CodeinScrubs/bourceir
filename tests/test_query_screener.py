"""داده تمام نمونه‌ها مصنوعی است و فقط در پوشه/دیتابیس موقت آزمون می‌ماند."""

import copy

import pytest

from core.query_screener import daily_evidence, predicate, rank_caps, summarize_symbol, trade_evidence, validate_spec


def candle(**updates):
    return dict(date='2024-02-03',ins_code='123',open_raw=100,low_raw=98,high_raw=102,close_raw=101,
        close_final_raw=102,ref_price_raw=100,volume=4,value=401,trade_count=4,source='tsetmc',price_unit='Rial',**updates)


def bar(**updates):
    return candle() | updates


def trades(prices=(100,98,102,101),**updates):
    return dict(source='tsetmc',price_unit='Rial',ins_code='123',requested_date='2024-02-03',endpoint_is_grouped=False,
        response={'tradeHistory':[dict(nTran=i+1,hEven=90001+i,pTran=p,qTitTran=1,canceled=0,dEven=20240203,insCode='123')
                                  for i,p in enumerate(prices)]}) | updates


def test_daily_green_bar_and_official_final_do_not_prove_order():
    row=daily_evidence(bar())
    assert row['event_possible'] is True and row['event_confirmed'] is None
    assert row['evidence']=='order_unknown'
    assert daily_evidence(bar(close_raw=102))['event_confirmed'] is True
    assert daily_evidence(bar(open_raw=98))['event_confirmed'] is True


@pytest.mark.parametrize('changes',[{'low_raw':98.01},{'high_raw':101.99}])
def test_thresholds_are_exact_not_relaxed(changes):
    row=daily_evidence(bar(**changes))
    assert row['event_possible'] is False and row['event_confirmed'] is False


def test_candle_proofs_are_sound_for_every_short_path():
    from itertools import product
    for path in product((97,98,100,102,103),repeat=5):
        actual=any(a<=98 and b>=102 for i,a in enumerate(path) for b in path[i+1:])
        scored=daily_evidence(bar(open_raw=path[0],close_raw=path[-1],low_raw=min(path),high_raw=max(path)))
        if scored['event_confirmed'] is True:assert actual
        if scored['event_confirmed'] is False:assert not actual


def test_noninteger_percentile_boundaries_select_by_position_from_smallest():
    # برای ۶۱۳ عضو: کوچک‌ترین ۶۱ کنار می‌روند؛ تا کوچک‌ترین ۲۴۵ شامل می‌شود.
    meta=[dict(id=i,ins_code=str(i+1),symbol_fa=f'نماد{i}الف',instrument_type_code=300,
               shares_outstanding=1000,is_active=1) for i in range(613)]
    quotes={i:dict(close_raw=i+1,source='tsetmc',price_unit='Rial') for i in range(613)}
    scored=rank_caps(meta,quotes)
    assert (scored['slice_first_rank'],scored['slice_last_rank'])==(369,552)
    assert scored['membership_counts']['inside']==184


@pytest.mark.parametrize('changes',[{'ref_price_raw':None},{'volume':0},{'high_raw':99},{'close_raw':200},
    {'source':'unverified'},{'price_unit':'Toman'},{'open_raw':float('nan')},{'low_raw':True}])
def test_invalid_or_missing_daily_is_unknown(changes):
    row=daily_evidence(bar(**changes))
    assert row['event_confirmed'] is None and row['event_possible'] is None


def test_complete_tick_order_and_identical_duplicate_are_reconciled():
    source=trades()
    source['response']['tradeHistory'].append(copy.deepcopy(source['response']['tradeHistory'][1]))
    source['response']['tradeHistory'].reverse()
    row=trade_evidence(source,bar())
    assert row['event_confirmed'] is True and row['complete'] and row['duplicate_rows']==1
    assert row['witness']['dip_number']==2 and row['witness']['rally_number']==3


def test_high_then_low_is_not_event_but_a_second_rally_is():
    row=trade_evidence(trades((100,102,98,101)),bar())
    assert row['complete'] and row['event_confirmed'] is False
    row=trade_evidence(trades((100,102,98,102,101)),bar(volume=5,value=503,trade_count=5))
    assert row['complete'] and row['event_confirmed'] is True
    assert row['witness']['dip_number']==3 and row['witness']['rally_number']==4


def test_payload_missing_date_identity_requires_full_daily_reconciliation():
    source=trades()
    for row in source['response']['tradeHistory']:
        row.update(insCode=None,dEven=0)
    found=trade_evidence(source,bar())
    assert found['complete'] and found['event_confirmed'] is True
    assert not found['payload_date_verified'] and not found['payload_identity_verified']
    source['response']['tradeHistory'].pop()
    partial=trade_evidence(source,bar())
    assert not partial['complete'] and partial['witness'] and partial['event_confirmed'] is None


def test_partial_named_dated_witness_proves_existence_but_not_absence():
    source=trades()
    source['response']['tradeHistory']=source['response']['tradeHistory'][1:3]
    result=trade_evidence(source,bar())
    assert result['event_confirmed'] is True and not result['complete']
    source['response']['tradeHistory'].reverse()
    assert trade_evidence(source,bar())['event_confirmed'] is True
    source['response']['tradeHistory']=source['response']['tradeHistory'][:1]
    assert trade_evidence(source,bar())['event_confirmed'] is None


@pytest.mark.parametrize('damage',['conflict','date','identity','cancel','time','unknown_cancel','outside','value'])
def test_tick_damage_never_becomes_confirmed_absence(damage):
    source=trades()
    rows=source['response']['tradeHistory']
    day=bar()
    if damage=='conflict':rows.append(dict(rows[1],pTran=99))
    elif damage=='date':rows[1]['dEven']=20240204
    elif damage=='identity':rows[1]['insCode']='456'
    elif damage=='cancel':rows[1]['canceled']=1
    elif damage=='time':rows[1]['hEven']=86000
    elif damage=='unknown_cancel':rows[1].pop('canceled')
    elif damage=='outside':rows[1]['pTran']=97
    elif damage=='value':day['value']=10000
    result=trade_evidence(source,day)
    assert not result['complete'] and result['event_confirmed'] is not False
    if damage in ('conflict','date','identity','time','unknown_cancel','outside'):
        assert result['event_confirmed'] is None


def test_canceled_trade_not_in_path_and_same_second_has_number_order():
    source=trades()
    for row in source['response']['tradeHistory']:row['hEven']=90100
    source['response']['tradeHistory'].append(dict(nTran=5,hEven=90100,pTran=102,qTitTran=20,canceled=1,dEven=20240203,insCode='123'))
    result=trade_evidence(source,bar())
    assert result['complete'] and result['event_confirmed'] is True


@pytest.mark.parametrize('payload',[None,[],{'tradeHistory':[None]},{'tradeHistory':[123]},{'tradeHistory':{}}])
def test_malformed_trade_shape_is_unknown(payload):
    result=trade_evidence(trades(response=payload),bar())
    assert result['event_confirmed'] is None and not result['complete'] and result['issues']


def universe(n=10):
    names=['الف','ب','پ','ت','ث','ج','چ','ح','خ','د']
    return [dict(id=i,ins_code=str(i+1),symbol_fa=names[i],instrument_type_code=300,is_active=1,
                 shares_outstanding=1000,sector_code='43') for i in range(n)]


def test_cap_uses_last_and_keeps_missing_in_universe_denominator():
    meta=universe()
    quotes={i:dict(close_raw=100-i,close_final_raw=1,source='tsetmc',price_unit='Rial',date='2024-02-03') for i in range(9)}
    result=rank_caps(meta,quotes)
    assert result['universe_total']==10 and result['caps_unknown']==1
    assert (result['slice_first_rank'],result['slice_last_rank'])==(7,9)
    assert result['rows'][0]['market_cap_rial']==100000
    assert result['rows'][0]['rank_lower']==1 and result['rows'][0]['rank_upper']==2
    assert result['rows'][5]['slice_membership']=='boundary_uncertain'
    assert result['rows'][6]['slice_membership']=='inside'
    assert result['rows'][-1]['slice_membership']=='unknown'


def test_ties_do_not_get_arbitrary_unique_rank_and_fractional_shares_not_estimated():
    meta=universe(3)
    quotes={i:dict(close_raw=100,source='tsetmc',price_unit='Rial') for i in range(3)}
    result=rank_caps(meta,quotes,low=0,high=50)
    assert all(r['slice_membership']=='boundary_uncertain' for r in result['rows'])
    meta[0]['shares_outstanding']=None
    meta[1]['shares_outstanding']=100.5
    assert rank_caps(meta,quotes)['caps_unknown']==2
    with pytest.raises(ValueError):rank_caps(meta+meta[:1],quotes)


def test_large_cap_product_stays_exact_integer_and_does_not_create_false_ties():
    meta=universe(2)
    value=2**53-1
    for row in meta:row['shares_outstanding']=value
    prices={i:dict(close_raw=value-i,source='tsetmc',price_unit='Rial') for i in range(2)}
    ranked=rank_caps(meta,prices,low=0,high=100)
    assert ranked['rows'][0]['market_cap_rial']==value*value
    assert ranked['rows'][1]['market_cap_rial']==value*(value-1)
    assert ranked['rows'][0]['rank_upper']==1


def test_dsl_is_bounded_whitelisted_and_three_valued():
    spec=dict(start='2024-01-01',end='2024-02-03',where={'all':[
        {'field':'market_cap_rial','op':'gt','value':100},
        {'field':'symbol_fa','op':'in','value':['الف','ب']} ]})
    validate_spec(spec)
    assert predicate(spec['where'],dict(symbol_fa='الف')) is None
    assert predicate(spec['where'],dict(symbol_fa='الف',market_cap_rial=200)) is True
    assert predicate(spec['where'],dict(symbol_fa='پ')) is False
    with pytest.raises(ValueError):validate_spec(spec|{'where':{'field':'__import__','op':'eq','value':1}})
    with pytest.raises(ValueError):validate_spec(spec|{'where':{'any':[{'all':[spec['where']]*20}]*20}})
    with pytest.raises(ValueError):validate_spec(spec|{'cap_low':40,'cap_high':10})


def test_observed_zero_is_distinct_from_missing_history():
    known=summarize_symbol({},[daily_evidence(bar(low_raw=99,high_raw=101))],market_dates=['2024-02-03','2024-02-04'])
    absent=summarize_symbol({},[],market_dates=['2024-02-03','2024-02-04'])
    assert known['confirmed_count']==0 and known['missing_market_dates']==1
    assert absent['observed_sessions']==0 and absent['event_rate_among_valid_pct'] is None
    assert not absent['full_year_count_verified']


def make_query(db,tmp_path,monkeypatch):
    from config import settings
    from ops.query_screener import start_query
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    meta=universe(1)[0]
    sid=db.upsert_symbol(meta)
    assert db.insert_daily_price(sid,'2024-02-03',100,102,98,101,volume=4,value=401,trade_count=4,
                                 close_final_raw=102,ref_price_raw=100)
    return start_query(db,dict(start='2024-02-03',end='2024-02-03',cap_low=0,cap_high=100))


def test_query_registers_freezes_resumes_and_rejects_tampering(db,tmp_path,monkeypatch):
    from ops.query_screener import evaluate_query, read_query
    folder=make_query(db,tmp_path,monkeypatch)
    result,revision=read_query(folder)
    assert result['results'][0]['ambiguous_count']==1 and result['candidate_days']==1
    assert not result['protocol']['holdout_opened'] and not result['accepted_for_signals']
    assert len(db.get_experiments())==1
    html=(revision/'report.html').read_text(encoding='utf-8')
    assert 'همه شرکت‌ها' in html and '__SUMMARY__' not in html
    class Provider:
        def get_trade_session(self,ins_code,day):
            source=trades(ins_code=ins_code)
            for row in source['response']['tradeHistory']:row['insCode']=ins_code
            return source
    result,new=evaluate_query(folder,provider=Provider(),max_trade_days=1)
    assert new!=revision and revision.exists()
    assert result['results'][0]['confirmed_count']==1
    again,_=evaluate_query(folder)
    assert again['results']==result['results']
    (new/'summary.csv').write_text('damaged',encoding='utf-8')
    # اشاره‌گر اکنون به نسخه سوم است؛ خراب کردن نسخه قدیمی آن را تغییر نمی‌دهد.
    read_query(folder)
    (folder/'daily-prices.json').write_text('[]',encoding='utf-8')
    with pytest.raises(ValueError):read_query(folder)


def test_query_does_not_publish_partial_revision_or_follow_outside_path(db,tmp_path,monkeypatch):
    import ops.query_report as report
    from ops.query_screener import evaluate_query, read_query, write_json
    folder=make_query(db,tmp_path,monkeypatch)
    _,old=read_query(folder)
    monkeypatch.setattr(report,'write_report',lambda *_:(_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):evaluate_query(folder)
    assert read_query(folder)[1]==old
    write_json(folder/'trade-references.json',{'1:2024-02-03':dict(path=str(tmp_path/'secret'),sha256='x')})
    with pytest.raises(ValueError):evaluate_query(folder)


def test_query_dashboard_opens_verified_result(db,tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    make_query(db,tmp_path,monkeypatch)
    at=AppTest.from_string('from dashboard.query_screener import page_query_screener\npage_query_screener()',default_timeout=30).run()
    assert not at.exception and len(at.get('download_button'))==3


def test_parser_exposes_safe_contract_and_explicit_network_budget():
    from brs import build_parser, cmd_query
    args=build_parser().parse_args(['query','--dip=-2','--max-trade-days','1'])
    with pytest.raises(ValueError):cmd_query(args)
    assert build_parser().parse_args(['archive-site']).url=='https://algosaham.ir/'


def test_archive_content_hash_and_retry_unavailable(tmp_path):
    from data_collector.trade_archive import TradeArchive
    archive=TradeArchive(tmp_path/'archive')
    failed,_=archive.capture(None,'123','2024-02-03')
    assert failed['status']=='unavailable'
    class Provider:
        def get_trade_session(self,*_):return trades()
    record,path=archive.capture(Provider(),'123','2024-02-03')
    assert record['status']=='ok' and archive.load(path)==record
    path.write_bytes(b'damaged')
    with pytest.raises((ValueError,OSError)):archive.load(path)


def test_archive_never_publishes_a_record_it_cannot_read(tmp_path):
    from data_collector.trade_archive import TradeArchive
    archive=TradeArchive(tmp_path,max_bytes=1024)
    class Large:
        def get_trade_session(self,*_):return trades()|{'large':'x'*5000}
    record,path=archive.capture(Large(),'123','2024-02-03')
    assert record['status']=='oversized' and record['observation'] is None
    assert record['raw_payload_preserved'] is False
    assert archive.load(path)==record


def test_empty_trade_response_can_be_retried_and_outage_stops_after_three(db,tmp_path,monkeypatch):
    from config import settings
    from data_collector.trade_archive import TradeArchive
    from ops.query_screener import evaluate_query, start_query
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    sid=db.upsert_symbol(universe(1)[0])
    for day in ['2024-02-03','2024-02-04','2024-02-05','2024-02-06']:
        db.insert_daily_price(sid,day,100,102,98,101,volume=4,value=401,trade_count=4,close_final_raw=102,ref_price_raw=100)
    folder=start_query(db,dict(start='2024-02-03',end='2024-02-06',cap_low=0,cap_high=100))
    class Unavailable:
        calls=0
        def get_trade_session(self,code,day):
            self.calls+=1
            return None
    provider=Unavailable()
    result,_=evaluate_query(folder,provider=provider,max_trade_days=4)
    assert provider.calls==3 and result['collection_stopped_reason']
    assert result['results'][0]['ambiguous_count']==4
    archive=TradeArchive(settings.DATA_DIR/'private'/'trade-archive')
    archive.capture(provider,'1','2024-02-03')
    assert provider.calls==4
    class Empty:
        calls=0
        def get_trade_session(self,code,day):
            self.calls+=1
            return dict(response={'tradeHistory':[]})
    empty=Empty()
    record,_=archive.capture(empty,'123','2024-02-03')
    assert record['status']=='empty'
    archive.capture(empty,'123','2024-02-03')
    assert empty.calls==2
    # ادامهٔ قبلی، روزهای ناموجودِ محفوظ را دوباره درخواست نمی‌کند؛ از آن‌ها عبور می‌کند.
    continued,_=evaluate_query(folder,provider=empty,max_trade_days=4)
    assert continued['collection_stopped_reason'] is None and continued['tick_attempts_this_run']==1


def test_file_lock_excludes_another_writer_and_releases_after_error(tmp_path):
    from core.file_lock import exclusive_file
    path=tmp_path/'file.lock'
    with pytest.raises(ValueError):
        with exclusive_file(path):
            with pytest.raises(RuntimeError):
                with exclusive_file(path):pass
            raise ValueError('release')
    with exclusive_file(path):pass


def test_report_escapes_source_markup_and_spreadsheet_formula():
    from ops.query_report import cell, escaped
    assert cell('=WEBSERVICE("url")').startswith("'=")
    assert cell(-2)==-2
    assert escaped('<script>')=='&lt;script&gt;'
