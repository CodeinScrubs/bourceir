"""خروجی allowlist پژوهش بازار؛ هیچ DB، حساب، رمز یا کد محیط خصوصی بسته‌بندی نمی‌شود."""

import hashlib
import json
import math
import os
import re
import uuid
import zipfile
from pathlib import Path

from config import settings
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from features.provenance import feature_environment
from features.store import ALL_FEATURES
from ops.research_review import _load_json
from research.baselines import STRATEGIES
from research.portfolio_run import verified_baseline_inputs


def export_research_bundle(db, source_id, *, root=None, output=None):
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    source_id, frames, hashes = verified_baseline_inputs(root, source_id)
    allowed = dict(features={'date', 'symbol', *ALL_FEATURES},
        prices={'date', 'symbol', 'open_raw', 'low_raw', 'high_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value'})
    for name, frame in frames.items():
        if name == 'labels':
            permitted = {'date', 'symbol', 'value', 'entry_date', 'entry_executable'}
            permitted.update(c for c in frame if re.fullmatch(r'(label_status|planned_exit_date|exit_date|corporate_actions|net_ret|mfe|mae|exit_delay)_[1-9][0-9]*d(_worst)?', c))
        else:
            permitted = allowed[name]
        if not set(frame) <= permitted:
            raise ValueError('ستون خارج از allowlist بازار در آرشیو است؛ خروجی ساخته نشد')
    report_path = root / (source_id + '.json')
    report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
    report = _load_json(report_path)
    row = db.conn.execute('SELECT * FROM experiments WHERE experiment_id=? AND status=?', (source_id, 'completed')).fetchone()
    if row is None or json.loads(row['parameters_json']) != report['params']:
        raise ValueError('ثبت منبع پژوهش با گزارش سازگار نیست')
    params = report['params']
    start, cutoff = params['periods']['development']
    if (not is_canonical_iso_date(start) or not is_canonical_iso_date(cutoff) or start >= cutoff
            or cutoff > settings.RESEARCH_CONFIG['holdout_start']
            or params['strategies'] != [s.name for s in STRATEGIES]
            or type(params['top_k']) is not int or not 1 <= params['top_k'] <= 50
            or any(type(h) is not int or h < 1 for h in params['horizons'])
            or type(params['random_trials']) is not int or params['random_trials'] < 1
            or type(params['min_daily_value_rial']) not in (int, float)
            or not math.isfinite(params['min_daily_value_rial']) or params['min_daily_value_rial'] < 0
            or params['features']['sha256'] != hashes['features'] or params['labels']['sha256'] != hashes['labels']):
        raise ValueError('مشخصات محدود منبع بازار معتبر نیست')
    # فقط مشخصات لازم برای بازتولید؛ metadata اصلی ممکن است اطلاعات آزاد و خصوصی داشته باشد.
    original_manifest = _load_json(root / source_id / 'manifest.json')
    if not is_canonical_iso_date(original_manifest['exclusive_end_date']):
        raise ValueError('مرز محفوظ ورودی بازار باید تاریخ ISO باشد')
    feature_manifest = params['research_provenance']['feature_manifest']
    environment = feature_environment()
    safe_feature_manifest = dict(version=feature_manifest['version'], provenance=dict(
        requirements_lock_sha256=environment['requirements_lock_sha256'], feature_environment=environment,
        raw_inputs=dict(version=feature_manifest['provenance']['raw_inputs']['version'])))
    safe_params = dict(strategies=params['strategies'], top_k=params['top_k'], horizons=params['horizons'],
        min_daily_value_rial=params['min_daily_value_rial'], random_trials=params['random_trials'],
        periods=dict(development=[start, cutoff]), features=dict(sha256=hashes['features']),
        labels=dict(version=original_manifest['version'], sha256=hashes['labels']),
        research_provenance=dict(feature_manifest=safe_feature_manifest),
        raw_feature_inputs_verified=True, holdout_selection=None, portfolio_simulated=False)
    safe_report = dict(params=safe_params, results={}, market_bundle_projection=True,
                       original_source_report_sha256=report_sha)
    safe_manifest = dict(version=original_manifest['version'], exclusive_end_date=original_manifest['exclusive_end_date'],
        parquet_sha256=hashes['labels'], raw_prices_sha256=hashes['prices'], fees=dict(settings.FEES),
        slippage_bps_each_side=settings.BACKTEST_CONFIG['default_slippage_bps'],
        lock_min_move_pct=settings.BACKTEST_CONFIG['lock_min_move_pct'])
    target = Path(output) if output is not None else settings.DATA_DIR / 'private' / 'research-exports' / ('MARKET-' + uuid.uuid4().hex + '.zip')
    target = target.resolve()
    if (not target.is_relative_to((settings.DATA_DIR / 'private').resolve()) or target.suffix.lower() != '.zip'
            or target.exists() or target.is_relative_to((root / source_id).resolve())):
        raise ValueError('خروجی باید ZIP جدید خارج از آرشیو منبع و در data/private باشد')
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '-' + uuid.uuid4().hex + '.tmp')
    try:
        inventory = {}
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            def add(name, body, expected=None):
                digest = hashlib.sha256(body).hexdigest()
                if expected is not None and digest != expected:
                    raise ValueError('ورودی هنگام بسته‌بندی تغییر کرد')
                archive.writestr(name, body)
                inventory[name] = digest
            for name, expected in hashes.items():
                add(f'{source_id}/{name}.parquet', (root / source_id / (name + '.parquet')).read_bytes(), expected)
            add(f'{source_id}/manifest.json', json.dumps(safe_manifest, ensure_ascii=False, allow_nan=False).encode())
            add(source_id + '.json', json.dumps(safe_report, ensure_ascii=False, allow_nan=False).encode())
            registration = dict(experiment_id=source_id, strategy_name='market-baseline-projection', status='completed',
                parameters_json=json.dumps(safe_params, ensure_ascii=False, allow_nan=False), metrics_json='{}')
            add('source-registration.json', json.dumps(registration, ensure_ascii=False, allow_nan=False).encode())
            manifest = dict(version='development-market-bundle-v2', created_at=now_tehran_iso(), source_id=source_id,
                holdout_start=settings.RESEARCH_CONFIG['holdout_start'], files_sha256=inventory,
                rows={name: len(frame) for name, frame in frames.items()}, account_tables_included=False,
                contains_local_research_metadata_paths=False, arbitrary_source_metadata_included=False,
                original_source_report_sha256=report_sha, raw_provenance_partitions_omitted=True,
                full_original_report_or_registration=False, externally_authenticated=False)
            archive.writestr('bundle-manifest.json', json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode())
        with zipfile.ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError('بسته خروجی ناسازگار است')
        if hashlib.sha256(report_path.read_bytes()).hexdigest() != report_sha:
            raise ValueError('گزارش منبع هنگام ساخت خروجی تغییر کرد')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return dict(path=str(target), files=len(inventory)+1, rows=manifest['rows'], account_tables_included=False,
                contains_local_research_metadata_paths=False, arbitrary_source_metadata_included=False)


def main(argv=None):
    import argparse

    from database.db import DatabaseManager

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    if not settings.DB_PATH.is_file():
        raise ValueError('دیتابیس موجود پژوهش لازم است؛ پایگاه خالی ساخته نمی شود')
    with DatabaseManager(read_only=True) as db:
        result = export_research_bundle(db, args.source, output=args.output)
    print(result['path'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
