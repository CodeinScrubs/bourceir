"""بازاجرای اندازه سرمایه روی انتخاب‌های محفوظ؛ بدون انتخاب قاعده تازه یا خواندن حساب کارگزاری."""

import hashlib
import json
import math
import os
import re
import shutil
import uuid
from dataclasses import replace
from pathlib import Path

import pandas as pd
import yaml

from backtest.portfolio import PortfolioConfig, replay_portfolio
from config import settings
from core.capital_capacity import VERSION, diagnose_cash_ledger, summarize_cases
from core.forward_observations import canonical
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.workbench import WorkbenchRepository
from ops.dip_diagnostics import _development_frame
from ops.research_review import _load_json, _relative_file, _verify_inventory, review_research, sha256
from research.labels import market_calendar
from research.run import _json_safe, archive_research_code

ENGINE_FILES = ('backtest/portfolio.py', 'backtest/fractions.py', 'core/market_rules.py', 'core/risk.py',
                'config/settings.py', 'requirements.lock.txt')
EXTRA_CODE = ('ops/capital_capacity.py', 'ops/research_review.py', 'ops/dip_diagnostics.py', 'brs.py')


def _write(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def _copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _current_code(files):
    return {name: hashlib.sha256((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n')).hexdigest()
            for name in files}


def _same_metrics(actual, expected):
    if set(actual) != set(expected):
        raise ValueError('ساختار آمار بازاجرا با منبع تفاوت دارد')
    for key, value in expected.items():
        measured = actual[key]
        if isinstance(value, (float, int)) and not isinstance(value, bool):
            if not isinstance(measured, (float, int)) or not math.isclose(value, measured, rel_tol=1e-9, abs_tol=.01):
                raise ValueError('آمار بازاجرا با منبع تفاوت دارد: ' + key)
        elif measured != value:
            raise ValueError('آمار بازاجرا با منبع تفاوت دارد: ' + key)


def _source_lock_rule(source_dir):
    default = yaml.safe_load((source_dir / 'code/config/default.yaml').read_text(encoding='utf-8'))
    production = yaml.safe_load((source_dir / 'code/config/production.yaml').read_text(encoding='utf-8'))
    values = {default['backtest']['lock_min_move_pct'],
              settings._deep_merge(default, production)['backtest']['lock_min_move_pct']}
    if len(values) != 1 or next(iter(values)) != settings.BACKTEST_CONFIG['lock_min_move_pct']:
        raise ValueError('آستانه قفل بازار یا محیط منبع ناسازگار/مبهم است')
    return next(iter(values))


def run_capacity_study(db, experiment_id, *, owner_capital=False, multipliers=(1, 10, 100), root=None):
    if (type(owner_capital) is not bool or not multipliers or len(multipliers) > 3
            or tuple(sorted(set(multipliers))) != tuple(multipliers) or multipliers[0] != 1
            or any(type(m) is not int or m < 1 or m > 1000 for m in multipliers)):
        raise ValueError('اندازه‌های یکتای مرتب با شروع یک و حداکثر سه سطح لازم‌اند')
    review = review_research(db, experiment_id)
    source_dir = settings.DATA_DIR / 'research' / experiment_id
    source = _load_json(source_dir.with_suffix('.json'))
    protocol = source['protocol']
    if protocol['version'] != 'flow-portfolio-v4' or protocol['portfolio_version'] != 'portfolio-v4':
        raise ValueError('مطالعه فعلی فقط منبع صریح FLOW v4 را می‌پذیرد')
    if _current_code(ENGINE_FILES) != {name: protocol['source_files'][name] for name in ENGINE_FILES}:
        raise ValueError('کد موتور یا وابستگی‌ها با منبع تغییر کرده است؛ قیاس اندازه مجاز نیست')
    lock_rule = _source_lock_rule(source_dir)
    profile = WorkbenchRepository(db).profile() if owner_capital else None
    if owner_capital and (profile is None or not math.isfinite(profile['capital_rial']) or profile['capital_rial'] <= 0):
        raise ValueError('سرمایه مرحله در پروفایل محلی موجود نیست')
    scenarios = [dict(name=f'x{m}', multiplier=m) for m in multipliers]
    if profile:
        scenarios.append(dict(name='owner', capital_rial=profile['capital_rial'], profile_updated_at=profile['updated_at']))
    fees = next(iter(source['results'][0]['modes'].values()))['assumptions']['fees']
    if fees != dict(settings.FEES) or any(item['assumptions']['fees'] != fees
            for fold in source['results'] for item in fold['modes'].values()):
        raise ValueError('کارمزد جاری با منبع منجمد برابر نیست')
    base = (root or settings.DATA_DIR / 'private' / 'capital-capacity').resolve()
    folder = base / ('CAPACITY-' + uuid.uuid4().hex)
    folder.mkdir(parents=True, exist_ok=False)
    code = archive_research_code(folder / 'code')
    for name in EXTRA_CODE:
        path = folder / 'code' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((settings.BASE_DIR / name).read_bytes().replace(b'\r\n', b'\n'))
        code[name] = sha256(path)
    inputs = {'source.json': review['verified']['report_sha256'], 'prices.parquet': protocol['inputs']['prices']}
    _copy(source_dir.with_suffix('.json'), folder / 'inputs/source.json')
    _copy(source_dir / 'prices.parquet', folder / 'inputs/prices.parquet')
    for filename in ('default.yaml', 'production.yaml'):
        name = 'source-' + filename
        inputs[name] = protocol['source_files']['config/' + filename]
        _copy(source_dir / 'code/config' / filename, folder / 'inputs' / name)
    for index, fold in enumerate(source['results']):
        for mode in fold['modes']:
            for name in ('picks', 'executions', 'equity', 'decisions'):
                filename = f'fold-{index:02d}-{mode}-{name}.parquet'
                inputs[filename] = source['artifacts_sha256'][filename]
                _copy(source_dir / filename, folder / 'inputs' / filename)
    _verify_inventory(folder / 'inputs', inputs)
    study_id = 'EXP-' + now_tehran_iso()[:10] + '-CAPACITY-' + uuid.uuid4().hex
    case_count = sum(len(f['modes']) for f in source['results']) * len(scenarios)
    plan = dict(version=VERSION, id=study_id, registered_at=now_tehran_iso(), source_experiment=experiment_id,
        source_report_sha256=inputs['source.json'], cutoff=protocol['holdout_start'], scenarios=scenarios,
        folds=protocol['folds'], modes=list(source['results'][0]['modes']), fees=fees, filter_version=protocol['filter_version'],
        lock_min_move_pct=lock_rule,
        planned_replay_cases=case_count, configuration_count=len(scenarios) * len(protocol['modes']),
        additional_capital_configurations=(len(scenarios) - 1) * len(protocol['modes']),
        historical_total_search_count=None, independence_verified=False, inputs=inputs, code=code,
        experiment_kind='fixed_source_capacity_diagnostic', starting_cash_is_hypothetical=True,
        current_holdings_and_available_cash_read=False, owner_capital_read=owner_capital,
        acceptance_changed=False, accepted_for_signals=False, holdout_opened=False,
        source_scope='all frozen v4 modes, controls and folds; no outcome filtering or parameter fitting')
    _write(folder / 'plan.json', plan)
    if db.log_experiment(experiment_id=study_id + '-PROTOCOL', strategy_name=VERSION, symbols_tested='frozen-universe',
            parameters_json=canonical(plan), metrics_json='{}', end_date=plan['cutoff'], dataset_version=experiment_id,
            features_version=plan['filter_version'],
            model_version=VERSION) is None:
        raise RuntimeError('ثبت مطالعه پیش از بازاجرا ناموفق بود')
    _write(folder / 'status.json', dict(status='running', completed_cases=0, planned_cases=case_count))
    cases, files = [], {}
    try:
        # مرز تاریخ پیش از خواندن ستون‌های قیمت کنترل می‌شود؛ فقط فایل منجمد توسعه مصرف می‌شود.
        prices = _development_frame(folder / 'inputs/prices.parquet', plan['cutoff'])
        calendar = market_calendar(prices)
        for index, fold in enumerate(source['results']):
            boundary = fold['fold']
            days = calendar[(calendar >= boundary['test_start']) & (calendar < boundary['test_end_exclusive'])]
            prior = prices.loc[prices.date < boundary['test_start']].groupby('symbol', sort=False).tail(1)
            replay_prices = pd.concat([prior, prices.loc[prices.date.isin(days)]], ignore_index=True)
            for mode, original in fold['modes'].items():
                prefix = f'fold-{index:02d}-{mode}'
                picked = _development_frame(folder / 'inputs' / (prefix + '-picks.parquet'), plan['cutoff'])
                raw = replay_prices.loc[replay_prices.symbol.isin(picked.symbol.unique())] if not picked.empty else replay_prices.iloc[:0]
                for scenario in scenarios:
                    cfg = PortfolioConfig(**original['assumptions']['config'])
                    amount = scenario.get('capital_rial', cfg.initial_capital_rial * scenario.get('multiplier', 1))
                    cfg = replace(cfg, initial_capital_rial=amount)
                    replay = replay_portfolio(raw, picked, calendar, cfg, start=boundary['test_start'], end=boundary['test_end_exclusive'])
                    replay.metrics['max_loss_from_initial'] = None if replay.metrics['total_return'] is None else max(
                        0., 1 - float(replay.equity.equity_rial.min()) / amount)
                    if scenario['name'] == 'x1':
                        _same_metrics(_json_safe(replay.metrics), original['metrics'])
                        for name in ('equity', 'executions', 'decisions'):
                            expected = _development_frame(folder / 'inputs' / (prefix + '-' + name + '.parquet'), plan['cutoff'])
                            pd.testing.assert_frame_equal(getattr(replay, name), expected, check_dtype=False, rtol=1e-9, atol=.01)
                    key = prefix + '-' + scenario['name']
                    diagnostic = diagnose_cash_ledger(replay.equity, replay.executions, replay.decisions, replay.metrics, len(picked))
                    cases.append(dict(key=key, fold=index, mode=mode, scenario=scenario['name'], metrics=replay.metrics,
                                      assumptions=replay.assumptions,
                                      selected_opportunities=len(picked), diagnostic=diagnostic))
                    for name in ('equity', 'executions', 'decisions'):
                        path = folder / 'cases' / (key + '-' + name + '.parquet')
                        path.parent.mkdir(exist_ok=True)
                        getattr(replay, name).to_parquet(path, index=False, compression='zstd')
                        files[path.relative_to(folder).as_posix()] = sha256(path)
            _write(folder / 'status.json', dict(status='running', completed_cases=len(cases), planned_cases=case_count))
            print(f'ظرفیت سرمایه: برش {index + 1}/{len(source["results"])} تمام شد.', flush=True)
        if _current_code(code) != code:
            raise RuntimeError('کد حین مطالعه تغییر کرده است؛ نتیجه منتشر نشد')
        _verify_inventory(folder / 'inputs', inputs)
        summary = {scenario['name']: {mode: summarize_cases([c['diagnostic'] for c in cases
                    if c['scenario'] == scenario['name'] and c['mode'] == mode]) for mode in plan['modes']} for scenario in scenarios}
        result = _json_safe(dict(version=VERSION, experiment_id=study_id, plan_sha256=sha256(folder / 'plan.json'),
            created_at=now_tehran_iso(), cases=cases, summary=summary, artifacts=files,
            source_base_replay_parity_verified=True, accepted_for_signals=False, profitability_proven=False,
            limitations=['fold means are not continuous account CAGR', 'recorded fee addback keeps the same fills and is not a zero-cost backtest',
                'unfilled orders and idle folds remain; no outcome-ranked size is selected',
                'actual corporate entitlements, historical fees, membership and execution remain unverified',
                'owner scenario starts with hypothetical cash; it does not reconstruct the current invested portfolio']))
        if len(cases) != case_count or db.log_experiment(experiment_id=study_id, strategy_name=VERSION,
                symbols_tested='frozen-universe', parameters_json=canonical(plan), metrics_json=canonical(summary),
                end_date=plan['cutoff'], dataset_version=experiment_id, features_version=plan['filter_version'], model_version=VERSION) is None:
            raise RuntimeError('ثبت نتیجه مطالعه ناموفق بود')
        _write(folder / 'result.json', result)
        _write(folder / 'result-digest.json', dict(sha256=sha256(folder / 'result.json')))
        _write(folder / 'status.json', dict(status='complete', completed_cases=len(cases), planned_cases=case_count))
        return dict(folder=str(folder), **result)
    except BaseException:
        _write(folder / 'status.json', dict(status='failed_or_interrupted', completed_cases=len(cases), planned_cases=case_count))
        raise


def read_capacity_study(folder, *, deep=False):
    folder = Path(folder)
    if not re.fullmatch(r'CAPACITY-[a-f0-9]{32}', folder.name):
        raise ValueError('مسیر مطالعه معتبر نیست')
    result = _load_json(folder / 'result.json')
    plan = _load_json(folder / 'plan.json')
    if (result['version'] != VERSION or plan['version'] != VERSION or result['accepted_for_signals'] is not False
            or result['profitability_proven'] is not False or result['source_base_replay_parity_verified'] is not True
            or not is_canonical_iso_date(plan['cutoff']) or plan['cutoff'] > settings.RESEARCH_CONFIG['holdout_start']
            or sha256(folder / 'result.json') != _load_json(folder / 'result-digest.json')['sha256']
            or result['plan_sha256'] != sha256(folder / 'plan.json') or result['experiment_id'] != plan['id']
            or len(result['cases']) != plan['planned_replay_cases']):
        raise ValueError('هویت، هش یا دامنه مطالعه ناسازگار است')
    expected_cases = {(i, mode, scenario['name']) for i in range(len(plan['folds']))
                      for mode in plan['modes'] for scenario in plan['scenarios']}
    if (len(result['cases']) != len(expected_cases)
            or {(c['fold'], c['mode'], c['scenario']) for c in result['cases']} != expected_cases):
        raise ValueError('برش، اندازه یا حالت از مخرج مطالعه حذف/تکرار شده است')
    if deep:
        _verify_inventory(folder / 'inputs', plan['inputs'])
        _verify_inventory(folder / 'code', plan['code'])
        _verify_inventory(folder / 'cases', {Path(k).name: v for k, v in result['artifacts'].items()})
        source = _load_json(folder / 'inputs/source.json')
        for case in result['cases']:
            prefix = f"fold-{case['fold']:02d}-{case['mode']}"
            if case['key'] != prefix + '-' + case['scenario']:
                raise ValueError('شناسه فایل برش با دامنه تفاوت دارد')
            original = source['results'][case['fold']]['modes'][case['mode']]
            scenario = next(s for s in plan['scenarios'] if s['name'] == case['scenario'])
            config = dict(original['assumptions']['config'])
            config['initial_capital_rial'] = scenario.get('capital_rial', config['initial_capital_rial'] * scenario.get('multiplier', 1))
            if case['assumptions']['config'] != config or case['assumptions']['fees'] != plan['fees']:
                raise ValueError('تنظیمات اندازه یا هزینه با برنامه تفاوت دارد')
            frames = {name: _development_frame(_relative_file(folder, 'cases/' + case['key'] + '-' + name + '.parquet'), plan['cutoff'])
                      for name in ('equity', 'executions', 'decisions')}
            picked = _development_frame(folder / 'inputs' / (prefix + '-picks.parquet'), plan['cutoff'])
            if case['selected_opportunities'] != len(picked):
                raise ValueError('مخرج نامزدهای انتخاب‌شده با منبع تفاوت دارد')
            checked = diagnose_cash_ledger(frames['equity'], frames['executions'], frames['decisions'], case['metrics'], case['selected_opportunities'])
            if checked != case['diagnostic']:
                raise ValueError('تشخیص نقد و هزینه با شواهد محفوظ ناسازگار است')
            if scenario['name'] == 'x1':
                _same_metrics(case['metrics'], original['metrics'])
                for name, frame in frames.items():
                    expected_frame = _development_frame(folder / 'inputs' / (prefix + '-' + name + '.parquet'), plan['cutoff'])
                    pd.testing.assert_frame_equal(frame, expected_frame, check_dtype=False, rtol=1e-9, atol=.01)
    expected = {s['name']: {mode: summarize_cases([c['diagnostic'] for c in result['cases']
                if c['scenario'] == s['name'] and c['mode'] == mode]) for mode in plan['modes']} for s in plan['scenarios']}
    if expected != result['summary']:
        raise ValueError('خلاصه با تمام برش‌های مطالعه ناسازگار است')
    return dict(plan=plan, artifacts_verified=deep, replay_recomputed=False, **result)
