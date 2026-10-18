"""گزارش خصوصی DIP از آرشیو قدیمی تأییدشده؛ بدون بازنویسی نتیجه مالی."""

import io
import uuid

import pandas as pd
import pyarrow.parquet as pq

from config import settings
from core.dip_diagnostics import VERSION, diagnose_dip_fold, witness_summary
from core.query_screener import daily_evidence, trade_evidence
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from data_collector.trade_archive import TradeArchive
from ops.report_files import write_private_report
from ops.research_review import _load_json, review_research, sha256
from research.labels import market_calendar


def _development_frame(path, cutoff):
    content = path.read_bytes()
    stream = io.BytesIO(content)
    metadata = pq.ParquetFile(stream)
    if metadata.metadata.num_rows == 0 and 'date' not in metadata.schema.names:
        return pd.DataFrame()
    stream.seek(0)
    dates = pd.read_parquet(stream, columns=['date']).date
    if dates.ge(cutoff).any() or any(not is_canonical_iso_date(d) for d in dates.unique()):
        raise ValueError('تشخیص توسعه، دوره مهر و موم را باز نمی‌کند')
    stream.seek(0)
    return pd.read_parquet(stream)


def run_dip_diagnostics(db, experiment_id, *, provider=None, max_trade_days=50, root=None):
    if type(max_trade_days) is not int or not 0 <= max_trade_days <= 50:
        raise ValueError('سقف دریافت شاهد باید بین صفر و پنجاه باشد')
    review = review_research(db, experiment_id)
    source_folder = settings.DATA_DIR / 'research' / experiment_id
    source = _load_json(source_folder.with_suffix('.json'))
    if source['protocol']['version'] != 'expanding-portfolio-v3' or 'dip' not in source['summary']:
        raise ValueError('آرشیو DIP نقد مشترک لازم است')
    cutoff = source['protocol']['holdout_start']
    folder = (root or settings.DATA_DIR / 'private' / 'dip-diagnostics') / ('DIPDIAG-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    prices = _development_frame(source_folder / 'prices.parquet', cutoff)
    calendar = market_calendar(prices)
    candidate_keys, candidate_days, folds = set(), {}, []
    for index, result in enumerate(source['results']):
        prefix = source_folder / f'fold-{index:02d}-dip'
        executions = _development_frame(prefix.with_name(prefix.name + '-executions.parquet'), cutoff) if (
            result['modes']['dip']['metrics']['executions']) else pd.DataFrame()
        picks = _development_frame(prefix.with_name(prefix.name + '-picks.parquet'), cutoff)
        decisions = _development_frame(prefix.with_name(prefix.name + '-decisions.parquet'), cutoff)
        symbols = executions.symbol.unique() if 'symbol' in executions else []
        fold_prices = prices.loc[prices.symbol.isin(symbols) & (prices.date >= result['fold']['test_start'])
                                 & (prices.date < result['fold']['test_end_exclusive'])]
        diagnostic = diagnose_dip_fold(fold_prices, calendar.tolist(), executions, decisions, picks,
            result['modes']['dip']['metrics'], hold=source['protocol']['dip']['rule']['hold'])
        folds.append(dict(fold=index, boundary=result['fold'], diagnostic=diagnostic))
        for row in picks.to_dict('records'):
            next_index = calendar.searchsorted(row['date'], side='right')
            if next_index >= len(calendar):
                continue
            candidate_keys.add((row['symbol'], calendar[next_index]))
    selected_prices = prices.set_index(['symbol', 'date']).reindex(pd.MultiIndex.from_tuples(sorted(candidate_keys), names=['symbol', 'date'])).reset_index()
    for price in selected_prices.to_dict('records'):
        evidence = daily_evidence(dict(price, source='tsetmc', price_unit='Rial'), dip_pct=-2, rally_pct=3)
        if evidence['event_possible'] is True:
            candidate_days[price['symbol'], price['date']] = price
    # فهرست به ترتیب زمان/نماد است؛ زیان یا سود هیچ نقشی در انتخاب دریافت ندارد.
    selected = sorted(candidate_days, key=lambda key: (key[1], key[0]))[:max_trade_days]
    plan = dict(version=VERSION, source_experiment=experiment_id, registered_at=now_tehran_iso(),
                source_report_sha256=sha256(source_folder.with_suffix('.json')), source_review=review['verified'],
                all_possible_days=len(candidate_days), selected=[dict(symbol=s, date=d, archived_daily=candidate_days[s, d]) for s, d in selected],
                selection='first date,symbol among all frozen DIP decision next-session candles touching ref -2% and ref +3%; no PnL selection',
                thresholds='both relative to day reference; distinct from +3% of entry price DIP target',
                max_trade_days=max_trade_days, cutoff=cutoff, network_requested=provider is not None,
                frozen_financial_results_rewritten=False, accepted_for_signals=False)
    write_private_report(folder / 'plan.json', plan)
    sources = ('ops/dip_diagnostics.py', 'core/dip_diagnostics.py', 'core/query_screener.py', 'data_collector/trade_archive.py')
    fingerprints = {}
    for name in sources:
        content = (settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')
        path = folder / 'code' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        fingerprints[name] = sha256(path)
    archive, observations = TradeArchive(folder / 'trades'), []
    for ordinal, (symbol, day) in enumerate(selected):
        meta = db.get_symbol(symbol)
        current = db.get_daily_prices_df(symbol, start_date=day, end_date=day)
        before = candidate_days[symbol, day]
        compared = ['open_raw', 'close_raw', 'high_raw', 'low_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value']
        same = len(current) == 1 and all(pd.notna(before[k]) and before[k] == current.iloc[0][k] for k in compared)
        evidence = dict(event_confirmed=None, complete=False, issues=['current candle differs from frozen financial input'])
        saved = None
        if same and meta and meta['instrument_type_code'] in (300, 303):
            # جمع تعداد از مشاهده جاری است؛ ثبات تاریخی/اولین انتشار آن ادعا نمی‌شود.
            daily = dict(current.iloc[0], ins_code=str(meta['ins_code']), source='tsetmc', price_unit='Rial')
            record, path = archive.capture(provider, meta['ins_code'], day)
            saved = dict(path=path.relative_to(folder).as_posix(), sha256=sha256(path), status=record['status'])
            evidence = trade_evidence(record.get('observation'), daily, dip_pct=-2, rally_pct=3)
        item = dict(symbol=symbol, date=day, current_candle_matches_frozen=bool(same), evidence=evidence, archive=saved)
        observations.append(item)
        write_private_report(folder / f'observation-{ordinal:03d}.json', item)
    if any(sha256(settings.BASE_DIR / name) != digest for name, digest in fingerprints.items()):
        # مقایسه بالا باید پایان‌خط ویندوز را نیز همانند نسخه محفوظ قانونمند کند.
        import hashlib
        if any(hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest() != digest
               for name, digest in fingerprints.items()):
            raise ValueError('کد تشخیص حین دریافت تغییر کرد')
    review_research(db, experiment_id)
    output = dict(version=VERSION, source_experiment=experiment_id, created_at=now_tehran_iso(), folds=folds,
                  plan=plan, observations=observations,
                  intraday_witnesses=witness_summary([r['evidence'] for r in observations], all_possible_days=len(candidate_days)),
                  accepted_for_signals=False, financial_results_frozen=True,
                  evidence_scope='frozen portfolio accounting plus limited current same-source path audit; not independent historical verification')
    path = write_private_report(folder / 'result.json', output)
    return dict(report_path=str(path), **output)
