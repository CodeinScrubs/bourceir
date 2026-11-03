"""Forensic price claims, peer coverage and participant contradictions; temporary data only."""

import numpy as np
import pandas as pd
import pytest

from core.flow_filters import filter_diagnostics, flow_metrics
from core.stock_study import (
    compare_peers,
    describe_pre_window,
    describe_window,
    observed_market_caps,
    peer_summary,
    validate_panel,
)
from core.symbol_review import volume_comparability
from tests.test_flow_filters import history


def test_impossible_retail_value_cannot_activate_filter():
    p,c=history(70)
    p['volume'],p['value']=30_000_000.,30_000_000_000.
    c['buy_i_volume'],c['buy_n_volume'],c['sell_i_volume']=30_000_000.,0.,10_000_000.
    c['buy_i_value'],c['sell_i_value']=30_000_000_000.,10_000_000_000.
    c['buy_i_count'],c['sell_i_count']=200,100
    c['buy_n_value'],c['sell_n_volume'],c['sell_n_value']=0.,20_000_000.,20_000_000_000.
    c.loc[69,'buy_i_value']=90_000_000_000.
    result=flow_metrics(p,c)
    assert np.isnan(result.iloc[-1].buyer_power_value)
    assert not filter_diagnostics(result.tail(1),'retail_acceleration').iloc[0].match


def test_initial_comparability_requires_n_prior_observations():
    p,_=history(22)
    mask=volume_comparability(p,20)
    assert not mask.iloc[19] and mask.iloc[20]


@pytest.mark.parametrize('column,value', [
    ('buy_i_value', 8_008_000_000.),  # تومان به جای ریال
    ('buy_i_volume', 110_000_000.),
    ('buy_n_value', 40_000_000_000.),
    ('buy_i_count', True),
])
def test_inconsistent_side_is_unknown_and_other_side_independent(column, value):
    p,c=history()
    if isinstance(value,bool):c[column]=c[column].astype(object)
    c.loc[c.index[-1],column]=value
    row=flow_metrics(p,c).iloc[-1]
    assert pd.isna(row.per_capita_buy_rial)
    assert pd.notna(row.per_capita_sell_rial)


def test_each_side_reconciles_separately_and_small_rounding_is_allowed():
    p,c=history()
    p['value']=100_045_000_000.
    c['buy_n_value']=p.value-c.buy_i_value
    c['sell_n_volume']=p.volume-c.sell_i_volume
    c['sell_n_value']=p.value-c.sell_i_value
    c.loc[c.index[-1],'buy_n_value']+=1
    row=flow_metrics(p,c).iloc[-1]
    assert row.client_buy_value_quality=='verified_side_total'
    assert row.client_sell_value_quality=='verified_side_total'
    assert row.buyer_power_value>2
    assert row.net_real_value_rial==30_130_000_000


def test_market_value_unit_error_cannot_validate_partial_retail_values():
    p,c=history()
    p['value']/=10
    row=flow_metrics(p,c).iloc[-1]
    assert pd.isna(row.per_capita_sell_rial)


def test_exact_dates_raw_price_and_synthetic_action_model_are_distinct():
    p,_=history(5)
    p.loc[2:,['open_raw','high_raw','low_raw','close_raw','close_final_raw']]/=2
    p.loc[2:,'ref_price_raw']/=2
    w=describe_window(p,p.date.iloc[0],p.date.iloc[-1])
    assert w['last_price_multiple']==.5
    assert w['synthetic_multiple'] is None
    assert w['observed_only_synthetic_multiple']==1
    assert len(w['reference_gaps'])==1
    assert w['economic_return'] is None and w['executable_return'] is None
    assert describe_window(p.iloc[1:],p.date.iloc[0],p.date.iloc[-1])['observed_only_synthetic_multiple'] is None
    p.loc[3,'ref_price_raw']=np.nan
    assert describe_window(p,p.date.iloc[0],p.date.iloc[-1])['observed_only_synthetic_multiple'] is None


def test_missing_intermediate_session_cannot_become_a_complete_return_or_peer_result():
    p,_=history(5)
    p.loc[2:,'close_final_raw']=1100.
    p.loc[3:,'ref_price_raw']=1100.
    start,end=p.date.iloc[0],p.date.iloc[-1]
    complete=describe_window(p,start,end)
    omitted=describe_window(p.drop(index=2),start,end)
    assert complete['exact_endpoints'] and omitted['exact_endpoints']
    assert complete['observed_only_synthetic_multiple']==pytest.approx(1.1)
    assert omitted['observed_only_synthetic_multiple']==pytest.approx(1.0)
    assert omitted['unknown_returns']==0  # Missing rows cannot be counted from the incomplete panel.
    assert complete['synthetic_multiple'] is None and omitted['synthetic_multiple'] is None
    assert complete['synthetic_close_drawdown'] is None and omitted['synthetic_close_drawdown'] is None
    assert len(omitted['reference_gaps'])==1
    summary=peer_summary([dict(synthetic_multiple=1.1, observation_calendar_verified=False,
                               observed_only_synthetic_multiple=1.0)])
    assert summary['known']==0 and summary['mean_multiple'] is None
    assert summary['observed_only_mean_multiple']==1.0


def test_pre_window_strictly_excludes_start_and_future_and_does_not_zero_missing():
    p,_=history(70)
    before=p.date.iloc[60]
    expected=describe_pre_window(p,before,20)
    p.loc[60:,'close_final_raw']*=1000
    assert describe_pre_window(p,before,20)==expected
    p.loc[59,'ref_price_raw']=np.nan
    assert describe_pre_window(p,before,20)['synthetic_return'] is None
    assert describe_pre_window(p.iloc[59:],before,20)['observed_rows']==1
    assert not expected['observation_calendar_verified']
    assert expected['calendar_days_since_last_observation']==(pd.Timestamp(before)-pd.Timestamp(p.date.iloc[59])).days


def test_pre_window_reports_unknown_lock_coverage_without_losing_observed_matches():
    p, _ = history(5)
    before = p.date.iloc[3]
    p.loc[1, ['low_raw', 'high_raw', 'ref_price_raw']] = [1050., 1050., 1000.]
    p.loc[2, 'ref_price_raw'] = np.nan
    result = describe_pre_window(p, before, 2)
    assert result['lock_coverage'] == 1 and result['lock_unknown_rows'] == 1
    assert result['observed_locked_up_rows'] == 1
    assert result['locked_up_rows'] is None and result['locked_down_rows'] is None
    partial = describe_pre_window(p, before, 10)
    assert partial['observed_rows'] == 3 and partial['locked_up_rows'] is None


def test_old_stock_study_archive_is_read_without_reinterpretation(tmp_path, monkeypatch):
    import hashlib
    import json

    from ops.stock_study import load_latest_study

    folder = tmp_path / 'OLD'
    folder.mkdir()
    legacy = {'version': 'stock-study-v2', 'study_id': 'OLD', 'inputs': {},
              'source_files_sha256': {}, 'created_at': '2024-01-01T12:00:00+03:30',
              'cases': [], 'locked_up_rows': 0}
    study = json.dumps(legacy).encode('utf-8')
    html = b'<p>historic report</p>'
    (folder / 'study.json').write_bytes(study)
    (folder / 'report.html').write_bytes(html)
    pointer = {'version': 'stock-study-v2', 'study_id': 'OLD', 'folder': 'OLD',
               'study_sha256': hashlib.sha256(study).hexdigest(),
               'html_sha256': hashlib.sha256(html).hexdigest()}
    (tmp_path / 'latest.json').write_text(json.dumps(pointer), encoding='utf-8')
    loaded, path = load_latest_study(tmp_path)
    assert loaded == legacy and path == folder
    assert (folder / 'study.json').read_bytes() == study
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr('dashboard.stock_study.load_latest_study', lambda: (loaded, path))
    app = AppTest.from_string('from dashboard.stock_study import page_stock_study\npage_stock_study()').run()
    assert not app.exception
    assert any('شمار الگوی تک‌قیمتی' in warning.value and 'نسخهٔ قدیمی' in warning.value for warning in app.warning)


def test_controls_use_prior_liquidity_and_missing_member_stays_in_denominator():
    p,_=history(70)
    peers=[p.assign(symbol='الف',value=p.value*.9),p.assign(symbol='ب',value=p.value*1.1)]
    meta=[dict(symbol_fa=name,instrument_type_code=300,sector_code='43') for name in ['کیان','الف','ب','گمشده']]
    panel=pd.concat([p,*peers],ignore_index=True)
    start,end=p.date.iloc[60],p.date.iloc[-1]
    first=compare_peers(panel,meta,'کیان',start,end)
    cache={}
    assert first==compare_peers(panel,meta,'کیان',start,end,_window_cache=cache)
    assert first==compare_peers(panel,meta,'کیان',start,end,_window_cache=cache)
    assert first['market']['expected']==3 and first['market']['known']==0
    assert first['market']['mean_multiple'] is None
    assert first['market']['known_subset_mean_multiple'] is None
    assert first['market']['observed_only_known']==2
    assert first['market']['observed_only_mean_multiple']==1
    panel.loc[(panel.symbol=='الف') & (panel.date==end),'close_final_raw']*=100
    second=compare_peers(panel,meta,'کیان',start,end)
    assert [r['symbol'] for r in first['matched_by_prior_liquidity']]==[r['symbol'] for r in second['matched_by_prior_liquidity']]
    assert all(r['symbol']!='کیان' for r in first['sector_members'])


def test_market_cap_rank_exposes_missing_scope_and_duplicate_quotes():
    meta=[dict(symbol_fa=s,ins_code=str(i),instrument_type_code=300,shares_outstanding=1000) for i,s in enumerate(['الف','ب','پ'])]
    quotes=[dict(ins_code='0',last_price=10),dict(ins_code='1',last_price=20)]
    rows=observed_market_caps(meta,quotes)
    assert rows[0]['market_cap_rial']==10_000
    assert rows[0]['rank_lower_bound']==2 and rows[0]['rank_upper_bound']==3
    assert rows[1]['rank_among_known']==1 and rows[2]['rank_among_known'] is None
    assert all(r['universe_total']==3 and r['universe_known']==2 and r['historic_rank'] is None for r in rows)
    assert observed_market_caps(meta,quotes+quotes[:1])[0]['market_cap_rial'] is None


@pytest.mark.parametrize('damage', ['duplicate','date','symbol'])
def test_panel_rejects_ambiguous_keys(damage):
    p,_=history(4)
    if damage=='duplicate':p=pd.concat([p,p.tail(1)])
    elif damage=='date':p.loc[0,'date']='2024-1-1'
    else:p.loc[0,'symbol']=''
    with pytest.raises(ValueError):validate_panel(p)


def test_study_registers_before_computation_and_pins_inputs_and_publication_times(db,tmp_path,monkeypatch):
    from config import settings
    from ops.stock_study import build_stock_study, load_latest_study
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    sid=db.upsert_symbol(dict(symbol_fa='کیان',ins_code='1',instrument_type_code=300,sector_code='43'))
    p,c=history(70)
    db.upsert_daily_prices_df(sid,p.drop(columns='symbol'))
    db.upsert_client_types_df(sid,c.drop(columns='symbol'))
    start,end=p.date.iloc[60],p.date.iloc[-1]
    spec=dict(cases=[dict(symbol='كيان',start=start,end=end)])
    news=[dict(symbol='کیان',title='<script>untrusted</script>',tracing_no=n,url='javascript:alert(1)',published_at=stamp)
          for n,stamp in enumerate([start+'T05:29:00+00:00',start+'T05:30:00+00:00',start+'T08:00:00',None])]
    build_stock_study(db,spec,news=news)
    result,folder=load_latest_study()
    case=result['cases'][0]
    assert case['symbol']=='کیان' and len(case['news_before_start'])==1
    assert len(case['news_start_day_after_open'])==1 and case['news_unknown_publication_time']==2
    assert not result['holdout_opened'] and not result['predictive_validation']
    html=(folder/'report.html').read_text(encoding='utf-8')
    assert '&lt;script&gt;untrusted&lt;/script&gt;' in html
    assert 'href="javascript:' not in html
    assert 'شاخص فقط مشاهدات موجود' in html
    assert 'این عدد شاخص کامل بازه یا بازده اقتصادی نیست' in html
    from streamlit.testing.v1 import AppTest
    script='from dashboard.stock_study import page_stock_study\npage_stock_study()'
    at=AppTest.from_string(script,default_timeout=30).run()
    assert not at.exception and len(at.dataframe[0].value)==1
    assert len(at.get('download_button'))==2
    assert db.conn.execute("SELECT COUNT(*) FROM experiments WHERE experiment_id LIKE ?",(result['study_id']+'%',)).fetchone()[0]==2
    (folder/'prices.parquet').write_bytes(b'changed')
    with pytest.raises(ValueError,match='اثر انگشت'):load_latest_study()
    at=AppTest.from_string(script,default_timeout=30).run()
    assert not at.exception and len(at.error)==1


@pytest.mark.parametrize('fail_at',[1,2])
def test_failed_registration_cannot_publish_latest_study(db,tmp_path,monkeypatch,fail_at):
    from config import settings
    from ops.stock_study import build_stock_study
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    calls=[]
    original=db.log_experiment
    def register(*args,**kwargs):
        calls.append(args)
        return False if len(calls)==fail_at else original(*args,**kwargs)
    monkeypatch.setattr(db,'log_experiment',register)
    spec=dict(cases=[dict(symbol='کیان',start='2024-01-01',end='2024-01-02')])
    with pytest.raises(RuntimeError,match='ثبت'):build_stock_study(db,spec,news=[])
    assert len(calls)==fail_at
    assert not (settings.DATA_DIR/'private'/'stock-studies'/'latest.json').exists()


def test_source_change_during_render_prevents_publication(db,tmp_path,monkeypatch):
    from config import settings
    from ops import stock_study_report
    from ops.stock_study import build_stock_study
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    monkeypatch.setattr(settings,'BASE_DIR',tmp_path/'code')
    source=settings.BASE_DIR/'core'/'example.py'
    source.parent.mkdir(parents=True)
    source.write_text('old = 1\n',encoding='utf-8')
    original=stock_study_report.render_stock_study
    def render(*args):
        original(*args)
        source.write_text('changed = 2\n',encoding='utf-8')
    monkeypatch.setattr(stock_study_report,'render_stock_study',render)
    with pytest.raises(RuntimeError,match='کد مطالعه'):
        build_stock_study(db,dict(cases=[dict(symbol='کیان',start='2024-01-01',end='2024-01-02')]),news=[])
    assert not (settings.DATA_DIR/'private'/'stock-studies'/'latest.json').exists()
