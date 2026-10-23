"""Registered census of synthetic doubles in a frozen development-price cohort."""

import argparse
import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso
from ops.research_review import _load_json, _relative_file, _verify_inventory, read_development_parquet
from research.labels import CALENDAR_MIN_SYMBOLS, market_calendar
from research.rare_events import HORIZONS, VERSION, compute_rare_events, summarize_rare_events
from research.run import _json_safe, _sha256, archive_research_code, research_code_fingerprint


def _write(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _register(db, identifier, protocol, metrics):
    result = db.log_experiment(experiment_id=identifier, strategy_name=VERSION,
        symbols_tested='all observed symbols on predetermined development anchor dates; membership unverified',
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics), ensure_ascii=False, allow_nan=False),
        start_date=protocol['development_start'], end_date=protocol['holdout_start'],
        dataset_version=protocol['prices_sha256'], features_version='none', model_version=VERSION)
    if result is None:
        raise RuntimeError('ثبت مطالعه رخداد ناموفق بود')


def _inputs(path, digest, cutoff, start, min_symbols, stride):
    prices = read_development_parquet(path, digest, cutoff)
    if prices.empty or prices.date.min() > start:
        raise ValueError('سابقه خام باید شروع دوره توسعه را پوشش دهد')
    calendar = market_calendar(prices, min_symbols=min_symbols).tolist()
    dates = [d for d in calendar if d >= start][::stride]
    if not dates:
        raise ValueError('هیچ روز تصمیمی در تقویم داده نیست')
    decisions = prices.loc[prices.date.isin(dates), ['symbol', 'date']].copy()
    return prices, calendar, decisions


def run_census(db, *, prices_path, prices_sha256, root, anchor_stride=20, min_symbols=CALENDAR_MIN_SYMBOLS):
    """Register before outcome computation. No model selection or financial strategy is fitted."""
    if type(anchor_stride) is not int or anchor_stride < 1 or type(min_symbols) is not int or min_symbols < 1:
        raise ValueError('گام تصمیم و حد پوشش تقویم باید صحیح مثبت باشند')
    cfg = settings.RESEARCH_CONFIG
    path, root = Path(prices_path).resolve(), Path(root).resolve()
    prices, calendar, decisions = _inputs(path, prices_sha256, cfg['holdout_start'], cfg['dev_start'], min_symbols, anchor_stride)
    identifier = f'EXP-{now_tehran_iso()[:10]}-RAREEVENT-{uuid.uuid4().hex}'
    folder = root/identifier
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(path, folder/'prices.parquet')
    if _sha256(folder/'prices.parquet') != prices_sha256 or _sha256(path) != prices_sha256:
        raise ValueError('ورودی خام حین کپی عوض شد')
    decisions.to_parquet(folder/'cohort.parquet', index=False)
    protocol = dict(version=VERSION, registered_at=now_tehran_iso(), development_start=cfg['dev_start'],
        holdout_start=cfg['holdout_start'], observed_through=str(prices.date.max()),
        prices_sha256=prices_sha256, cohort_sha256=_sha256(folder/'cohort.parquet'),
        source_code=archive_research_code(folder/'code'), code_sha256=research_code_fingerprint(),
        anchor_stride=anchor_stride, calendar_min_symbols=min_symbols, horizons=list(HORIZONS), threshold=2.,
        calendar=calendar, source_rows=len(prices), opportunities=len(decisions),
        cohort='all raw observations on every fixed nth observed market session from development start; no future-return selection',
        event='official-close PIT synthetic index first reaches 2 within H future sessions after decision close',
        endpoint='separate synthetic index at exact H; no delayed exit or intraday-high substitution',
        unknowns='all retained; missing/invalid/off-calendar prefix cannot establish no-hit or later first-hit',
        limits='no fees, fills, economic entitlement, historical membership or independence proof; no market prevalence claim',
        calibration_performed=False, strategy_trials_added=0, descriptive_target_definitions=len(HORIZONS),
        prospective_independence=False, holdout_opened=False, accepted_for_signals=False, profitability_proven=False)
    _write(folder/'protocol.json', protocol)
    _register(db, identifier+'-PROTOCOL', protocol, {})
    outcomes = compute_rare_events(prices, decisions, calendar, observed_through=protocol['observed_through'])
    outcomes.to_parquet(folder/'outcomes.parquet', index=False)
    summary = summarize_rare_events(outcomes)
    artifacts = {name:_sha256(folder/name) for name in ('prices.parquet', 'cohort.parquet', 'outcomes.parquet')}
    if research_code_fingerprint() != protocol['code_sha256']:
        raise ValueError('کد پژوهش هنگام اجرا تغییر کرد؛ نتیجه پذیرفته نمی‌شود')
    result = dict(experiment_id=identifier, protocol=protocol, summary=summary, artifacts_sha256=artifacts,
        completed_at=now_tehran_iso(), accepted_for_signals=False, profitability_proven=False)
    _register(db, identifier, protocol, summary)
    _write(folder/'result.json', result)
    return result


def review_census(db, folder):
    folder = Path(folder).resolve()
    result, protocol = _load_json(folder/'result.json'), _load_json(folder/'protocol.json')
    if result['protocol'] != protocol or protocol['version'] != VERSION or folder.name != result['experiment_id']:
        raise ValueError('پروتکل یا هویت آرشیو ناسازگار است')
    if (any(result.get(key) is not False for key in ('accepted_for_signals', 'profitability_proven'))
            or any(protocol.get(key) is not False for key in ('calibration_performed', 'prospective_independence',
                'holdout_opened', 'accepted_for_signals', 'profitability_proven'))
            or type(protocol.get('strategy_trials_added')) is not int or protocol['strategy_trials_added'] != 0
            or protocol.get('descriptive_target_definitions') != len(HORIZONS)):
        raise ValueError('ادعای پذیرش یا پیش‌بینی با قرارداد توصیفی رخداد ناسازگار است')
    if protocol['horizons'] != list(HORIZONS) or protocol['threshold'] != 2.:
        raise ValueError('هدف رخداد محفوظ تغییر کرده است')
    _verify_inventory(folder/'code', protocol['source_code'])
    _verify_inventory(folder, result['artifacts_sha256'], parquet=True)
    for identifier, metrics in ((result['experiment_id']+'-PROTOCOL', {}), (result['experiment_id'], result['summary'])):
        row = db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?',
                              (identifier, 'completed')).fetchone()
        if row is None or json.loads(row['parameters_json']) != protocol or json.loads(row['metrics_json']) != metrics:
            raise ValueError('ثبت مستقل با آرشیو رخداد برابر نیست')
    if result['artifacts_sha256']['prices.parquet'] != protocol['prices_sha256']:
        raise ValueError('قیمت محفوظ با ثبت قبلی برابر نیست')
    prices, calendar, cohort = _inputs(_relative_file(folder, 'prices.parquet'), protocol['prices_sha256'],
        protocol['holdout_start'], protocol['development_start'], protocol['calendar_min_symbols'], protocol['anchor_stride'])
    if calendar != protocol['calendar'] or len(prices) != protocol['source_rows'] or len(cohort) != protocol['opportunities']:
        raise ValueError('تقویم یا مخرج فرصت‌ها عوض شد')
    if str(prices.date.max()) != protocol['observed_through'] or _sha256(folder/'cohort.parquet') != protocol['cohort_sha256']:
        raise ValueError('زمان مشاهده یا جامعه محفوظ عوض شد')
    pd.testing.assert_frame_equal(cohort.reset_index(drop=True), pd.read_parquet(folder/'cohort.parquet').reset_index(drop=True))
    replay = compute_rare_events(prices, cohort, calendar, observed_through=protocol['observed_through'])
    pd.testing.assert_frame_equal(replay, pd.read_parquet(folder/'outcomes.parquet'))
    summary = summarize_rare_events(replay)
    if _json_safe(summary) != result['summary']:
        raise ValueError('خلاصه رخداد با بازحساب برابر نیست')
    return dict(experiment_id=result['experiment_id'], verified_at=now_tehran_iso(),
        outcomes_recomputed=len(replay), cohort_reconstructed=len(cohort),
        source_rows=len(prices), current_code_matches_capture=research_code_fingerprint()==protocol['code_sha256'],
        accepted_for_signals=False, profitability_proven=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prices', type=Path)
    parser.add_argument('--sha256')
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--root', type=Path, default=settings.DATA_DIR/'private'/'rare-event-studies')
    parser.add_argument('--review', type=Path)
    args = parser.parse_args(argv)
    from database.db import DatabaseManager
    if args.review:
        with DatabaseManager(args.db, read_only=True) as db:
            output = review_census(db, args.review)
    else:
        if args.prices is None or args.sha256 is None:
            parser.error('prices and sha256 must be explicit')
        if args.db.resolve() == settings.DB_PATH.resolve():
            parser.error('A separate experiment DB is required')
        with DatabaseManager(args.db) as db:
            result = run_census(db, prices_path=args.prices, prices_sha256=args.sha256, root=args.root)
        output = dict(experiment_id=result['experiment_id'], summary=result['summary'], accepted_for_signals=False)
    print(json.dumps(_json_safe(output), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
