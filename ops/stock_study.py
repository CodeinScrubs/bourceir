"""مطالعه محلیِ توصیفی با ورودی محفوظ؛ مستقل از مسیر پذیرش استراتژی و holdout."""

import hashlib
import json
import os
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from config import settings
from core.flow_filters import FLOW_FILTER_VERSION, flow_metrics
from core.stock_study import (
    STOCK_STUDY_VERSION,
    compare_peers,
    describe_pre_window,
    describe_window,
    observed_market_caps,
    validate_panel,
)
from core.text import normalize_symbol
from core.timeutils import now_tehran, now_tehran_iso


def _news_time(row):
    try:
        stamp = datetime.fromisoformat(row.get('published_at', ''))
        return stamp.astimezone(now_tehran().tzinfo) if stamp.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def _write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_source_unchanged(manifest):
    for name,digest in manifest.items():
        source=settings.BASE_DIR/name
        if not source.exists() or hashlib.sha256(source.read_bytes().replace(b'\r\n',b'\n')).hexdigest()!=digest:
            raise RuntimeError('کد مطالعه هنگام اجرا تغییر کرده است؛ اجرا با کد ثابت تکرار شود')


def load_latest_study(root=None):
    root=Path(root or settings.DATA_DIR/'private'/'stock-studies').resolve()
    pointer_file=root/'latest.json'
    if not pointer_file.exists():
        return None
    pointer=json.loads(pointer_file.read_text(encoding='utf-8'))
    if pointer.get('version')!=STOCK_STUDY_VERSION:
        raise ValueError('قرارداد مطالعه قدیمی است؛ نتیجه جاری فرض نمی‌شود')
    folder=(root/pointer['folder']).resolve()
    if not folder.is_relative_to(root) or folder==root:
        raise ValueError('مسیر مطالعه نامعتبر است')
    for name,key in (('study.json','study_sha256'),('report.html','html_sha256')):
        if _sha(folder/name)!=pointer[key]:
            raise ValueError('اثر انگشت گزارش مطالعه ناسازگار است')
    result=json.loads((folder/'study.json').read_text(encoding='utf-8'))
    if result.get('version')!=STOCK_STUDY_VERSION or result.get('study_id')!=pointer['study_id']:
        raise ValueError('هویت گزارش مطالعه ناسازگار است')
    for prefix,manifest in ((folder,result['inputs']),(folder/'code',result['source_files_sha256'])):
        for name,digest in manifest.items():
            path=(prefix/name).resolve()
            if not path.is_relative_to(prefix.resolve()) or _sha(path)!=digest:
                raise ValueError('اثر انگشت ورودی/کد محفوظ مطالعه ناسازگار است')
    return result,folder


def build_stock_study(db, spec, *, market_observation=None, news=None, output_root=None):
    cases = [dict(row, symbol=normalize_symbol(row['symbol'])) for row in spec['cases']]
    if not cases or len({row['symbol'] for row in cases}) != len(cases):
        raise ValueError('مطالعه به نمادهای یکتا نیاز دارد')
    for row in cases:
        start, end = (date.fromisoformat(row[key]) for key in ('start', 'end'))
        if start > end or end > now_tehran().date() or start.isoformat() != row['start'] or end.isoformat() != row['end']:
            raise ValueError('تاریخ بازه مطالعه نامعتبر یا در آینده است')
    root = Path(output_root or settings.DATA_DIR/'private'/'stock-studies').resolve()
    if not root.is_relative_to((settings.DATA_DIR/'private').resolve()):
        raise ValueError('خروجی مطالعه باید در data/private/ باشد')
    study_id = 'STUDY-'+uuid.uuid4().hex
    folder = root/study_id
    folder.mkdir(parents=True, exist_ok=False)
    protocol = dict(version=STOCK_STUDY_VERSION, registered_at=now_tehran_iso(), cases=cases,
                    pre_windows=[20,60], source_spec=spec, predictive_validation=False, signals_enabled=False,
                    current_membership_only=True, holdout_opened=False,
                    exposed_period_start=min(row['start'] for row in cases), exposed_period_end=max(row['end'] for row in cases),
                    access_purpose='owner-requested descriptive audit, not model/feature/parameter selection',
                    independent_validation_for_derived_hypotheses=False, prior_flow_version=FLOW_FILTER_VERSION)
    _write(folder/'protocol.json', protocol)
    registered = db.log_experiment(study_id+'-PROTOCOL', 'descriptive_stock_study_protocol',
        ','.join(row['symbol'] for row in cases), json.dumps(protocol,ensure_ascii=False,allow_nan=False), '{}',
        start_date=protocol['exposed_period_start'], end_date=protocol['exposed_period_end'], status='registered',
        dataset_version='public-raw', features_version='not_used', model_version='not_a_model')
    if not registered:
        raise RuntimeError('ثبت پروتکل مطالعه ناموفق بود؛ محاسبه آغاز نشد')
    from research.run import archive_research_code
    source_hashes = archive_research_code(folder/'code')
    for name in ('ops/stock_study.py','ops/stock_study_report.py'):
        source = settings.BASE_DIR/name
        if source.exists():
            destination = folder/'code'/name
            destination.parent.mkdir(exist_ok=True,parents=True)
            destination.write_bytes(source.read_bytes().replace(b'\r\n',b'\n'))
            source_hashes[name]=_sha(destination)
    _assert_source_unchanged(source_hashes)
    earliest=(date.fromisoformat(protocol['exposed_period_start'])-timedelta(days=366)).isoformat()
    own = not db.conn.in_transaction
    if own:
        db.conn.execute('BEGIN')
    try:
        metadata=db.get_universe_symbols(include_inactive=True)
        p=pd.read_sql_query("SELECT s.symbol_fa AS symbol,p.date,p.open_raw,p.high_raw,p.low_raw,p.close_raw,p.close_final_raw,"
            "p.ref_price_raw,p.volume,p.value,p.trade_count FROM daily_prices p JOIN symbols s ON s.id=p.symbol_id "
            "WHERE p.source='tsetmc' AND p.date>=? AND p.date<=? AND s.instrument_type_code IN (300,303)",
            db.conn,params=(earliest,protocol['exposed_period_end']))
        c=pd.read_sql_query("SELECT s.symbol_fa AS symbol,c.* FROM client_types_daily c JOIN symbols s ON s.id=c.symbol_id "
            "WHERE c.date>=? AND c.date<=? AND s.instrument_type_code IN (300,303)",
            db.conn,params=(earliest,protocol['exposed_period_end']))
        if news is None:
            news=[dict(row) for row in db.conn.execute('SELECT s.symbol_fa AS symbol,a.* FROM announcements a '
                                                       'JOIN symbols s ON s.id=a.symbol_id')]
    finally:
        if own:
            db.conn.rollback()
    metadata=[row for row in metadata if row['instrument_type_code'] in (300,303)
              and not row['symbol_fa'][-1:].isdigit()]
    symbols={row['symbol_fa'] for row in metadata}
    p=validate_panel(p.loc[p.symbol.isin(symbols)])
    c=validate_panel(c.loc[c.symbol.isin(symbols)])
    p.to_parquet(folder/'prices.parquet',index=False)
    c.to_parquet(folder/'clients.parquet',index=False)
    _write(folder/'metadata.json',metadata)
    _write(folder/'news.json',news)
    _write(folder/'market-observation.json',market_observation)
    groups={symbol:group.reset_index(drop=True) for symbol,group in p.groupby('symbol',sort=False)}
    client_groups={symbol:group for symbol,group in c.groupby('symbol',sort=False)}
    meta={row['symbol_fa']:row for row in metadata}
    live_meta=(market_observation or {}).get('details') or {}
    caps=[]
    if market_observation:
        instruments={str(row['ins_code']):row for row in market_observation['universe']}
        instruments.update({str(row['ins_code']):row for row in live_meta.values() if row})
        caps=observed_market_caps(list(instruments.values()),market_observation['snapshots'])
    caps_by_symbol={row['symbol']:row for row in caps}
    rows=[]
    # حافظه موقت فقط در همین مطالعه و روی panel محفوظ و تغییرناپذیر است.
    peer_window_cache={}
    for case in cases:
        symbol,start,end=(case[key] for key in ('symbol','start','end'))
        if symbol not in meta or symbol not in groups:
            rows.append(dict(**case,status='missing_history',causality_proven=False))
            continue
        history=groups[symbol]
        peers=compare_peers(p,metadata,symbol,start,end,_window_cache=peer_window_cache)
        peers.pop('market_members')
        metrics=flow_metrics(history,client_groups.get(symbol,pd.DataFrame(columns=['symbol','date'])))
        prior=metrics.loc[metrics.date<start].tail(20)
        flow={}
        for field in ('buyer_power_value','net_real_value_rial','volume_ratio_20d'):
            values=prior[field].dropna()
            flow[field]=dict(known=len(values),expected=20,
                known_subset_median=float(values.median()) if len(values) else None,
                full_mean=float(values.mean()) if len(values)==20 else None)
        for side in ('buy','sell'):
            flow[f'client_{side}_quality_counts']=prior[f'client_{side}_value_quality'].value_counts().to_dict()
        window=describe_window(history,start,end)
        exact=history.loc[history.date.isin((start,end))]
        endpoints=exact[['date','close_raw','close_final_raw','volume','value']].where(pd.notna(exact),None).to_dict('records')
        symbol_news=[row for row in news if normalize_symbol(row.get('symbol',row.get('symbol_fa','')))==symbol]
        news_rows=sorted([dict(row,published_at=_news_time(row).isoformat()) for row in symbol_news
                          if _news_time(row) is not None],key=_news_time)
        boundary=datetime.fromisoformat(start+'T09:00:00+03:30')
        before=[row for row in news_rows if _news_time(row)<boundary]
        same_day=[row for row in news_rows if _news_time(row).date().isoformat()==start and _news_time(row)>=boundary]
        row=dict(**case,status='descriptive',name=meta[symbol]['name_fa'],sector_code=meta[symbol]['sector_code'],
            sector_name=(live_meta.get(symbol) or {}).get('sector_name'), endpoints=endpoints,window=window,
            pre_20=describe_pre_window(history,start,20),pre_60=describe_pre_window(history,start,60),
            flow_pre_20=flow,peers=peers,market_cap=caps_by_symbol.get(symbol),
            news_before_start=before,news_start_day_after_open=same_day,
            news_during_window=[row for row in news_rows if start<=row['published_at'][:10]<=end],
            news_unknown_publication_time=sum(_news_time(row) is None for row in symbol_news),
            original_disclosure_content_verified=False,causality_proven=False)
        rows.append(row)
        print(f'مطالعه توصیفی {symbol} ثبت محلی شد ({len(rows)}/{len(cases)})؛ سیگنال ندارد.',flush=True)
    result=dict(version=STOCK_STUDY_VERSION,study_id=study_id,created_at=now_tehran_iso(),cases=rows,
                market_cap_universe=caps,market_observation_at=(market_observation or {}).get('observed_at'),
                signals_enabled=False,predictive_validation=False,economic_actions_reconciled=False,
                historical_membership_verified=False,holdout_opened=False,
                independent_validation_for_derived_hypotheses=False,
                inputs={name:_sha(folder/name) for name in ('prices.parquet','clients.parquet','metadata.json',
                        'news.json','market-observation.json','protocol.json')},source_files_sha256=source_hashes)
    # pandas/NumPy scalar JSON is normalized without replacing unknown with zero.
    from research.run import _json_safe
    result=_json_safe(result)
    _write(folder/'study.json',result)
    from ops.stock_study_report import render_stock_study
    render_stock_study(result,p,folder/'report.html')
    _assert_source_unchanged(source_hashes)
    registered=db.log_experiment(study_id,'descriptive_stock_study',','.join(row['symbol'] for row in cases),
        json.dumps(dict(protocol_id=study_id+'-PROTOCOL',inputs=result['inputs'],source_files_sha256=source_hashes),ensure_ascii=False),
        json.dumps(dict(symbols=len(rows),signals_enabled=False,predictive_validation=False),ensure_ascii=False),
        start_date=protocol['exposed_period_start'],end_date=protocol['exposed_period_end'],
        dataset_version='public-raw',features_version='not_used',model_version='not_a_model')
    if not registered:
        raise RuntimeError('ثبت نتیجه ناموفق بود؛ گزارش آخر منتشر نشد')
    pointer=dict(version=STOCK_STUDY_VERSION,study_id=study_id,folder=folder.relative_to(root).as_posix(),
                 study_sha256=_sha(folder/'study.json'),html_sha256=_sha(folder/'report.html'))
    tmp=root/(uuid.uuid4().hex+'.tmp')
    _write(tmp,pointer)
    os.replace(tmp,root/'latest.json')
    return dict(study_id=study_id,folder=str(folder),symbols=len(rows),signals_enabled=False)
