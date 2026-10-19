"""خواندن فقط‌خواندنی آرشیو پژوهش و تطبیق ثبت، کد، خروجی و منحنی سرمایه."""

import copy
import hashlib
import io
import json
import math
import re
from datetime import date
from pathlib import Path, PurePosixPath

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from config import settings
from core.research_review import finite, summarize_evidence
from core.timeutils import now_tehran_iso

EXPERIMENT_PATTERN = r'EXP-\d{4}-\d{2}-\d{2}-(PORTFOLIO|FLOW)-[a-f0-9]{32}'


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _load_json(path):
    def unique(pairs):
        values = {}
        for key, value in pairs:
            if key in values:
                raise ValueError('کلید تکراری در سند پژوهش')
            values[key] = value
        return values

    def invalid(value):
        raise ValueError('عدد غیرمتناهی در سند پژوهش: ' + value)

    return json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique, parse_constant=invalid)


def _relative_file(root, name):
    if (not isinstance(name, str) or not name or '\\' in name or ':' in name
            or PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/'))):
        raise ValueError('مسیر شواهد باید نسبی و درون آرشیو باشد')
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError('فایل شواهد موجود یا درون آرشیو نیست')
    return path


def _verify_inventory(root, expected, *, parquet=False):
    if not isinstance(expected, dict) or not expected:
        raise ValueError('فهرست اثر انگشت شواهد خالی یا نامعتبر است')
    verified = {}
    for name, expected_sha in expected.items():
        if not isinstance(expected_sha, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha):
            raise ValueError('اثر انگشت فایل نامعتبر است')
        path = _relative_file(root, name)
        if parquet and (path.suffix != '.parquet' or path.parent != root):
            raise ValueError('مسیر خروجی پژوهش نامعتبر است')
        if sha256(path) != expected_sha:
            raise ValueError('اثر انگشت شواهد ناسازگار است: ' + name)
        verified[name] = expected_sha
    actual = {p.relative_to(root).as_posix() for p in (root.glob('*.parquet') if parquet else root.rglob('*')) if p.is_file()}
    if set(expected) != actual:
        raise ValueError('فهرست شواهد با فایل‌های موجود یکسان نیست')
    return verified


def _iso(value):
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('تاریخ برش پژوهش باید ISO باشد')
    return value


def read_development_parquet(path, expected_sha, cutoff, *, require_date=True):
    """کنترل تاریخ قبل از مقدار، از همان تصویر بایتی هش‌شده؛ بدون پنجره تعویض فایل."""
    if (_iso(cutoff) > settings.RESEARCH_CONFIG['holdout_start']
            or not isinstance(expected_sha, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha)):
        raise ValueError('مرز یا اثر انگشت پژوهش معتبر نیست')
    path = Path(path)
    with path.open('rb') as stream:
        contents = stream.read(512 * 1024 * 1024 + 1)
    if len(contents) > 512 * 1024 * 1024 or hashlib.sha256(contents).hexdigest() != expected_sha:
        raise ValueError('اندازه یا اثر انگشت تصویر پژوهش نامعتبر است')
    buffer = io.BytesIO(contents)
    if 'date' in pq.read_schema(buffer).names:
        buffer.seek(0)
        dates = pd.read_parquet(buffer, columns=['date']).date
        if any(_iso(day) >= cutoff for day in dates.unique()):
            raise ValueError('خواندن مقادیر دوره مهروموم‌شده در بازبینی مجاز نیست')
    elif require_date or pq.ParquetFile(io.BytesIO(contents)).metadata.num_rows:
        raise ValueError('ورودی غیرخالی پژوهش باید تاریخ داشته باشد')
    buffer.seek(0)
    return pd.read_parquet(buffer)


def _equal_number(actual, expected, label):
    if not finite(actual) or not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-10):
        raise ValueError('آمار گزارش با منحنی محفوظ ناسازگار است: ' + label)


def _checked_mode(folder, index, name, record, fold, protocol, artifact_names):
    if not re.fullmatch('[a-z][a-z0-9_-]*', name):
        raise ValueError('نام حالت پژوهش نامعتبر است')
    filenames = {kind: f'fold-{index:02d}-{name}-{kind}.parquet' for kind in ('equity', 'executions', 'picks', 'decisions')}
    if not set(filenames.values()) <= set(artifact_names):
        raise ValueError('خروجی یک برش/حالت از شواهد حذف شده است')
    metrics, assumptions = record['metrics'], record['assumptions']
    config = assumptions['config']
    initial = protocol['config']['initial_capital_rial']
    loss_limit = protocol['config']['max_loss_pct']
    if (not finite(initial) or initial <= 0 or config['initial_capital_rial'] != initial
            or not finite(loss_limit) or not 0 < loss_limit < 1 or config['max_loss_pct'] != loss_limit):
        raise ValueError('سرمایه یا سقف زیان برش با پروتکل ناسازگار است')
    if assumptions['version'] != protocol['portfolio_version'] or assumptions['config']['stress'] is not (
            name in ('stress', 'dip_stress', 'past_baseline_stress') or name.endswith('-stress')):
        raise ValueError('قرارداد یا سناریوی فشار اجرا ناسازگار است')
    equity = read_development_parquet(folder / filenames['equity'], artifact_names[filenames['equity']], protocol['holdout_start'])
    if equity.empty or not {'date', 'equity_rial', 'positions', 'stale_positions'} <= set(equity):
        raise ValueError('منحنی سرمایه خالی یا ناقص است')
    dates = equity['date'].tolist()
    if dates != sorted(set(dates)) or any(not fold['test_start'] <= _iso(d) < fold['test_end_exclusive'] for d in dates):
        raise ValueError('تقویم منحنی با برش از پیش ثبت‌شده ناسازگار است')
    values = pd.to_numeric(equity['equity_rial'], errors='coerce').to_numpy(dtype=float)
    if any(isinstance(v, (str, bool)) for v in equity['equity_rial']):
        raise ValueError('مقدار سرمایه باید عددی باشد')
    count_columns = ['positions', 'stale_positions']
    if protocol['portfolio_version'] == 'portfolio-v4':
        if 'stale_fractional_exposures' not in equity:
            raise ValueError('شمار ارزش‌گذاری کهنه کسر سهم در قرارداد جدید موجود نیست')
        count_columns.append('stale_fractional_exposures')
    for column in count_columns:
        if any(not finite(v) or v < 0 or v != int(v) for v in equity[column].tolist()):
            raise ValueError('شمار پوزیشن‌های منحنی باید عدد صحیح نامنفی باشد')
    if equity['stale_positions'].gt(equity['positions']).any():
        raise ValueError('شمار قیمت کهنه از پوزیشن‌های باز بیشتر است')
    complete = metrics['status'] == 'complete'
    if not complete and metrics['status'] != 'invalid_data':
        raise ValueError('وضعیت نتیجه پژوهش ناشناخته است')
    checked = dict(metrics)
    if complete:
        if not np.isfinite(values).all() or (values < 0).any():
            raise ValueError('سرمایه نامعلوم در برش کامل مجاز نیست')
        total = values[-1] / initial - 1
        with_initial = np.insert(values, 0, initial)
        drawdown = float(-(with_initial / np.maximum.accumulate(with_initial) - 1).min())
        loss = max(0.0, 1 - float(values.min()) / initial)
        for label, expected in (('total_return', total), ('max_drawdown', drawdown),
                                ('final_equity_rial', values[-1]), ('initial_capital_rial', initial)):
            _equal_number(metrics[label], expected, label)
        if 'max_loss_from_initial' in metrics:
            _equal_number(metrics['max_loss_from_initial'], loss, 'max_loss_from_initial')
        checked.update(total_return=float(total), max_drawdown=drawdown, max_loss_from_initial=loss,
                       loss_floor_breached=loss > config['max_loss_pct'] + 1e-9)
    else:
        if metrics['total_return'] is not None or metrics['max_drawdown'] is not None or metrics['final_equity_rial'] is not None:
            raise ValueError('برش نامعتبر نباید سود یا افت عددی داشته باشد')
        if np.isfinite(values).all():
            raise ValueError('شاهد نامعلوم بودن منحنی در برش نامعتبر موجود نیست')
        checked.update(total_return=None, max_drawdown=None, max_loss_from_initial=None, loss_floor_breached=False)
    fills = read_development_parquet(folder / filenames['executions'], artifact_names[filenames['executions']],
                                    protocol['holdout_start'], require_date=False)
    for label in ('executions', 'open_positions', 'stale_mark_days', 'corporate_action_events'):
        if isinstance(metrics[label], bool) or not isinstance(metrics[label], int) or metrics[label] < 0:
            raise ValueError('شمار شواهد برش نامعتبر است')
    if metrics['executions'] != len(fills) or metrics['open_positions'] != int(equity['positions'].iloc[-1]):
        raise ValueError('شمار اجرا یا پوزیشن با خروجی محفوظ سازگار نیست')
    stale_days = equity['stale_positions'].gt(0)
    if protocol['portfolio_version'] == 'portfolio-v4':
        stale_days |= equity['stale_fractional_exposures'].gt(0)
    stale = int(stale_days.sum())
    if metrics['stale_mark_days'] != stale:
        raise ValueError('شمار قیمت کهنه با منحنی سازگار نیست')
    if metrics['actual_corporate_entitlements_verified'] is not (metrics['corporate_action_events'] == 0):
        raise ValueError('وضعیت حقوق اقتصادی با شمار عبور از اقدام شرکتی سازگار نیست')
    return dict(metrics=checked, assumptions=assumptions)


def review_research(db, experiment_id, *, root=None):
    """این مسیر داده خام، مدل، پارامتر، دفتر یا holdout را تغییر نمی‌دهد."""
    if not isinstance(experiment_id, str) or not re.fullmatch(EXPERIMENT_PATTERN, experiment_id):
        raise ValueError('شناسه صریح آزمایش سرمایه مشترک یا جریان لازم است')
    root = Path(root) if root is not None else settings.DATA_DIR / 'research'
    folder = root / experiment_id
    if not folder.resolve().is_relative_to(root.resolve()):
        raise ValueError('مسیر آزمایش خارج از آرشیو است')
    path = _relative_file(root, experiment_id + '.json')
    report_sha = sha256(path)
    payload = _load_json(path)
    protocol_path = _relative_file(folder, 'protocol.json')
    protocol_sha = sha256(protocol_path)
    protocol = payload['protocol']
    supported = {'expanding-portfolio-v3': 'PORTFOLIO', 'flow-portfolio-v3': 'FLOW', 'flow-portfolio-v4': 'FLOW'}
    if (payload['experiment_id'] != experiment_id or protocol.get('version') not in supported
            or '-' + supported[protocol['version']] + '-' not in experiment_id
            or protocol.get('portfolio_version') not in ('portfolio-v3', 'portfolio-v4')
            or payload.get('accepted_for_signals') is not False
            or protocol.get('prospective_independence') is not False):
        raise ValueError('این بازبین فقط قراردادهای توسعه شناخته‌شده و فاقد مجوز سیگنال را می‌پذیرد')
    if _load_json(protocol_path) != protocol:
        raise ValueError('پروتکل گزارش با پروتکل محفوظ ناسازگار است')
    # ثبت پروتکل و نتیجه نهایی در یک تصویر SQLite بررسی می‌شوند؛ این تصدیق بیرونی زمان نیست.
    with db.conn:
        db.conn.execute('BEGIN')
        records = [dict(r) for r in db.conn.execute('SELECT * FROM experiments WHERE experiment_id IN (?,?)',
                                                   (experiment_id, experiment_id + '-PROTOCOL'))]
    registered = {r['experiment_id']: r for r in records}
    if set(registered) != {experiment_id, experiment_id + '-PROTOCOL'}:
        raise ValueError('ثبت پروتکل و نتیجه در دفتر آزمایش‌ها موجود نیست')
    for row in records:
        if json.loads(row['parameters_json']) != protocol or row['status'] != 'completed':
            raise ValueError('پروتکل دفتر آزمایش‌ها ناسازگار است')
    if json.loads(registered[experiment_id]['metrics_json']) != payload['summary']:
        raise ValueError('خلاصه گزارش با دفتر نتیجه ناسازگار است')
    cutoff = _iso(protocol['holdout_start'])
    if cutoff > settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('گزارش مجاز به مصرف دوره مهروموم‌شده فعلی نیست')
    folds, results = protocol['folds'], payload['results']
    if not folds or len(folds) != len(results):
        raise ValueError('برش پژوهش حذف شده یا دامنه برش‌ها ناسازگار است')
    previous_end = None
    for fold, result in zip(folds, results):
        if (fold != result['fold'] or not _iso(fold['train_start']) < _iso(fold['train_end_exclusive']) <
                _iso(fold['test_start']) < _iso(fold['test_end_exclusive']) <= cutoff
                or previous_end is not None and fold['test_start'] < previous_end):
            raise ValueError('ترتیب یا مرز برش‌ها نامعتبر است')
        previous_end = fold['test_end_exclusive']
    artifacts = _verify_inventory(folder, payload['artifacts_sha256'], parquet=True)
    sources = _verify_inventory(folder / 'code', protocol['source_files'])
    inputs = protocol.get('inputs')
    required_inputs = {'features', 'labels', 'prices'} if supported[protocol['version']] == 'PORTFOLIO' else {
        'prices', 'clients', 'baseline_features', 'baseline_labels'}
    if (not isinstance(inputs, dict) or set(inputs) != required_inputs
            or any(artifacts.get(name + '.parquet') != h for name, h in inputs.items())):
        raise ValueError('اثر انگشت ورودی با پروتکل سازگار نیست')
    expected_modes = {'selected', 'stress', 'random', 'baseline', 'dip', 'dip_stress', 'dip_random', 'dip_vwap'}
    if supported[protocol['version']] == 'FLOW':
        expected_modes = {'past_baseline', 'past_baseline_stress'} | {
            f'{name}-{h}d{suffix}' for name in ('retail_acceleration', 'volume_breakout', 'daily_pressure')
            for h in protocol['horizons'] for suffix in ('', '-stress', '-random')}
    if protocol['version'] == 'flow-portfolio-v4':
        from core.flow_filters import FLOW_FILTER_V4_VERSION, V4_FILTERS
        from research.flow_v4_run import HORIZONS, SEEDS
        candidates = [f'{name}-{h}d' for name in V4_FILTERS for h in HORIZONS]
        suffixes = ('', '-stress', '-random', '-random97', '-random173',
                    '-random-stress', '-random97-stress', '-random173-stress')
        expected_modes = {'past_baseline', 'past_baseline_stress'} | {c + s for c in candidates for s in suffixes}
        if (protocol.get('filter_version') != FLOW_FILTER_V4_VERSION or protocol.get('horizons') != list(HORIZONS)
                or protocol.get('random_seeds') != list(SEEDS) or protocol.get('candidate_modes') != candidates
                or set(protocol.get('modes', [])) != expected_modes):
            raise ValueError('خانواده ثابت v4 با پروتکل سازگار نیست')
        uncertainty_path = folder / 'uncertainty.json'
        if sha256(uncertainty_path) != payload.get('uncertainty_sha256'):
            raise ValueError('اثر انگشت عدم قطعیت ناسازگار است')
    checked = copy.deepcopy(results)
    for i, result in enumerate(results):
        if set(result['modes']) != expected_modes:
            raise ValueError('یکی از حالت‌های لازم پژوهش حذف یا اضافه شده است')
        checked[i]['modes'] = {name: _checked_mode(folder, i, name, r, result['fold'], protocol, artifacts)
                               for name, r in result['modes'].items()}
    output = summarize_evidence(payload, checked)
    output['portfolio_version'] = protocol['portfolio_version']
    output['fractional_valuation_corrected'] = protocol['portfolio_version'] == 'portfolio-v4'
    if protocol['version'] == 'flow-portfolio-v4':
        output['uncertainty_sha256_verified'] = True
        output['uncertainty_statistics_recomputed'] = False
    if protocol['portfolio_version'] == 'portfolio-v3':
        output['limitations'].append('portfolio-v3 freezes synthetic fractional value; archives are readable, not corrected replays')
    # تطبیق خلاصه از منحنی بازسازی‌شده؛ خلاصه JSON به‌تنهایی منبع حقیقت نیست.
    for name, summary in output['summaries'].items():
        declared = payload['summary'][name]
        for key, derived in [('folds', summary['folds']), ('valid_folds', summary['known_folds']),
                             ('positive_folds', summary['positive_folds']),
                             ('mean_fold_return', summary['mean_known_fold_return']),
                             ('worst_fold_drawdown', summary['worst_known_drawdown'])]:
            if derived is None:
                if declared[key] is not None:
                    raise ValueError('خلاصه، نتیجه نامعلوم را عددی کرده است')
            else:
                _equal_number(declared[key], derived, key)
    if sha256(path) != report_sha or sha256(protocol_path) != protocol_sha:
        raise ValueError('گزارش یا پروتکل هنگام خواندن تغییر کرد')
    # تغییر فایل‌ها حین بازسازی گزارش نیز خروجی را متوقف می‌کند.
    _verify_inventory(folder, artifacts, parquet=True)
    _verify_inventory(folder / 'code', sources)
    if protocol['version'] == 'flow-portfolio-v4' and sha256(folder / 'uncertainty.json') != payload['uncertainty_sha256']:
        raise ValueError('عدم قطعیت حین بازبینی تغییر کرد')
    output.update(reviewed_at=now_tehran_iso(), verified=dict(report_sha256=report_sha, protocol_sha256=protocol_sha,
                  artifact_count=len(artifacts), source_file_count=len(sources), registered_protocol_and_result=True,
                  archived_files_match_declared_hashes=True, curve_metrics_recomputed=True,
                  external_authenticity_verified=False))
    return output


def format_review(report):
    pct = lambda v: 'نامعلوم' if v is None else f'{100 * v:+.2f}٪'  # noqa: E731
    lines = ['# بازبینی شواهد پژوهش توسعه', '', report['experiment_id'], '',
             'آمار از منحنی محفوظ بازسازی شده؛ هیچ روش تأییدشده‌ای برای سیگنال اعلام نمی‌شود.', '',
             '| روش | برش معلوم عادی/کل | میانگین عادیِ معلوم | میانه عادیِ معلوم | برش معلوم فشار/کل | میانگین فشارِ معلوم | عبور کف زیان فشار/معلوم |',
             '|---|---:|---:|---:|---:|---:|---:|']
    for name, item in report['candidates'].items():
        a, s = item['normal'], item['stress']
        lines.append(f"| {name} | {a['known_folds']}/{a['folds']} | {pct(a['mean_known_fold_return'])} | "
                     f"{pct(a['median_known_fold_return'])} | {s['known_folds']}/{s['folds']} | "
                     f"{pct(s['mean_known_fold_return'])} | {s['loss_floor_breached_folds']}/{s['known_folds']} |")
    lines += ['', 'میانگین حساب‌های مستقل، CAGR یا سود کل حساب نیست. نامعلوم در شمار می‌ماند؛ مخرج هر مقایسه در JSON صریح است.',
              'تفاوت با یک کنترل تصادفی، معنی‌داری آماری یا ریسک یکسان را ثابت نمی‌کند. حقوق واقعی اقدامات شرکتی، اجرای صف و آزمون مستقل آینده هنوز لازم‌اند.']
    return '\n'.join(lines) + '\n'
