"""بازبین شواهد: همه داده‌ها فقط در tests و روی دیتابیس/پوشه موقت هستند."""

import copy
import json

import numpy as np
import pandas as pd
import pytest

import brs
from config import settings
from core.research_review import paired_comparison, summarize_evidence
from database.db import DatabaseManager
from ops.research_review import format_review, review_research, sha256

EXPERIMENT = 'EXP-2024-01-01-PORTFOLIO-' + 'a' * 32
MODE_NAMES = ('selected', 'stress', 'random', 'baseline', 'dip', 'dip_stress', 'dip_random', 'dip_vwap')


def dump(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding='utf-8')


@pytest.fixture
def evidence(db, tmp_path):
    root = tmp_path / 'research'
    folder = root / EXPERIMENT
    folder.mkdir(parents=True)
    code = folder / 'code'
    code.mkdir()
    (code / 'requirements.lock.txt').write_text('# test only\n', encoding='utf-8')
    folds = [dict(train_start='2022-01-01', train_end_exclusive='2022-12-20', test_start='2023-01-01',
                  test_end_exclusive='2023-04-01', embargo_sessions=4),
             dict(train_start='2022-01-01', train_end_exclusive='2023-03-20', test_start='2023-04-01',
                  test_end_exclusive='2023-07-01', embargo_sessions=4)]
    config = dict(initial_capital_rial=1000, max_loss_pct=.10, stress=False)
    prices = pd.DataFrame({'date': ['2023-01-01'], 'symbol': ['آزمون'], 'close_raw': [10.]})
    prices.to_parquet(folder / 'prices.parquet', index=False)
    protocol = dict(version='expanding-portfolio-v3', portfolio_version='portfolio-v3', config=config,
                    holdout_start=settings.RESEARCH_CONFIG['holdout_start'], folds=folds,
                    prospective_independence=False, random_seed=41,
                    inputs={},
                    source_files={'requirements.lock.txt': sha256(code / 'requirements.lock.txt')})
    for name in ('features', 'labels', 'prices'):
        prices.to_parquet(folder / f'{name}.parquet', index=False)
        protocol['inputs'][name] = sha256(folder / f'{name}.parquet')
    results = []
    for i, fold in enumerate(folds):
        modes = {}
        for name in MODE_NAMES:
            ending = 1100 if name == 'selected' else 850 if name in ('stress', 'dip_stress') else 950
            curve = pd.DataFrame({'date': [fold['test_start'], str(pd.Timestamp(fold['test_end_exclusive']) - pd.Timedelta(days=1))[:10]]})
            curve = curve.assign(equity_rial=[1000., float(ending)], positions=[0, 0], stale_positions=[0, 0])
            metrics = dict(status='complete', total_return=ending / 1000 - 1,
                           max_drawdown=max(0, 1 - ending / 1000), initial_capital_rial=1000,
                           final_equity_rial=ending, executions=0, open_positions=0,
                           stale_mark_days=0, corporate_action_events=0, actual_corporate_entitlements_verified=True)
            assumptions = dict(version='portfolio-v3', config=config | {'stress': name in ('stress', 'dip_stress')})
            modes[name] = dict(metrics=metrics, assumptions=assumptions)
            for kind in ('equity', 'executions', 'picks', 'decisions'):
                (curve if kind == 'equity' else pd.DataFrame()).to_parquet(folder / f'fold-{i:02d}-{name}-{kind}.parquet', index=False)
        results.append(dict(fold=fold, modes=modes))
    summary = {}
    for name in MODE_NAMES:
        metrics = [f['modes'][name]['metrics'] for f in results]
        values = [m['total_return'] for m in metrics]
        summary[name] = dict(folds=2, valid_folds=2, positive_folds=sum(v > 0 for v in values),
                             mean_fold_return=sum(values) / 2, worst_fold_drawdown=max(m['max_drawdown'] for m in metrics))
    payload = dict(experiment_id=EXPERIMENT, protocol=protocol, results=results, summary=summary,
                   accepted_for_signals=False, artifacts_sha256={p.name: sha256(p) for p in folder.glob('*.parquet')})
    dump(folder / 'protocol.json', protocol)
    dump(root / (EXPERIMENT + '.json'), payload)
    for name, metric in ((EXPERIMENT + '-PROTOCOL', {}), (EXPERIMENT, summary)):
        db.log_experiment(name, 'test_only', 'آزمون', json.dumps(protocol), json.dumps(metric))
    return dict(root=root, folder=folder, payload=payload, report=root / (EXPERIMENT + '.json'))


def update_report(db, evidence):
    p, folder = evidence['payload'], evidence['folder']
    p['artifacts_sha256'] = {path.name: sha256(path) for path in folder.glob('*.parquet')}
    dump(evidence['report'], p)
    dump(folder / 'protocol.json', p['protocol'])
    with db.conn:
        db.conn.execute('UPDATE experiments SET parameters_json=? WHERE experiment_id IN (?,?)',
                        (json.dumps(p['protocol']), EXPERIMENT, EXPERIMENT + '-PROTOCOL'))
        db.conn.execute('UPDATE experiments SET metrics_json=? WHERE experiment_id=?', (json.dumps(p['summary']), EXPERIMENT))


def test_curve_recomputed_stress_failure_not_promoted_to_profitability(db, evidence):
    result = review_research(db, EXPERIMENT, root=evidence['root'])
    selected = result['candidates']['selected']
    assert selected['normal']['all_fold_mean'] == pytest.approx(.1)
    assert selected['stress']['all_fold_mean'] == pytest.approx(-.15)
    assert selected['stress']['loss_floor_breached_folds'] == 2
    assert 'nonpositive_stress_mean' in selected['blockers']
    assert 'simulated_loss_floor_breached' in selected['blockers']
    assert not result['accepted_for_signals'] and not selected['eligible_for_profitability_claim']
    assert result['portfolio_cagr'] is None and result['verified']['curve_metrics_recomputed']
    assert result['candidates']['dip']['comparisons']['selected_past_baseline']['mean_known_difference'] == pytest.approx(-.15)
    assert result['candidates']['dip']['comparisons']['stressed_selected_past_baseline']['mean_known_difference'] == 0
    assert 'میانگین حساب‌های مستقل' in format_review(result)
    assert not result['verified']['external_authenticity_verified']


def test_positive_everywhere_still_requires_independent_evidence(db, evidence):
    p = evidence['payload']
    for i, result in enumerate(p['results']):
        for name, record in result['modes'].items():
            curve_path = evidence['folder'] / f'fold-{i:02d}-{name}-equity.parquet'
            curve = pd.read_parquet(curve_path)
            curve.loc[1, 'equity_rial'] = 1200
            curve.to_parquet(curve_path, index=False)
            record['metrics'].update(total_return=.2, max_drawdown=0, final_equity_rial=1200)
    for summary in p['summary'].values():
        summary.update(mean_fold_return=.2, positive_folds=2, worst_fold_drawdown=0)
    update_report(db, evidence)
    report = review_research(db, EXPERIMENT, root=evidence['root'])
    assert not report['accepted_for_signals']
    assert all(not c['eligible_for_profitability_claim'] for c in report['candidates'].values())
    assert 'independent_future_test_missing' in report['candidates']['selected']['blockers']


def test_unknown_folds_not_zero_not_dropped_and_pairs_use_common_known_scope(db, evidence):
    p = evidence['payload']
    rec = p['results'][1]['modes']['selected']
    rec['metrics'].update(status='invalid_data', total_return=None, max_drawdown=None, final_equity_rial=None)
    path = evidence['folder'] / 'fold-01-selected-equity.parquet'
    curve = pd.read_parquet(path)
    curve.loc[1, 'equity_rial'] = np.nan
    curve.to_parquet(path, index=False)
    p['summary']['selected'].update(valid_folds=1, positive_folds=1)
    update_report(db, evidence)
    report = review_research(db, EXPERIMENT, root=evidence['root'])
    normal = report['candidates']['selected']['normal']
    pair = report['candidates']['selected']['comparisons']['normal_baseline']
    assert normal['known_folds'] == normal['unknown_folds'] == 1 and normal['all_fold_mean'] is None
    assert pair['paired_known_folds'] == pair['excluded_unknown_pairs'] == 1
    assert pair['rows'][1]['difference'] is None and pair['statistical_significance'] is None


@pytest.mark.parametrize('name', ['fold-00-selected-equity.parquet', 'code/requirements.lock.txt'])
def test_modified_artifact_or_code_fails_closed(db, evidence, name):
    path = evidence['folder'] / name
    path.write_bytes(path.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='اثر انگشت'):
        review_research(db, EXPERIMENT, root=evidence['root'])


def test_rehashed_curve_with_forged_metrics_is_detected(db, evidence):
    path = evidence['folder'] / 'fold-00-selected-equity.parquet'
    curve = pd.read_parquet(path)
    curve.loc[1, 'equity_rial'] = 500
    curve.to_parquet(path, index=False)
    update_report(db, evidence)
    with pytest.raises(ValueError, match='منحنی محفوظ'):
        review_research(db, EXPERIMENT, root=evidence['root'])


@pytest.mark.parametrize('attack', ['registry_missing', 'registry_mismatch', 'protocol_mismatch', 'summary_mismatch',
                                  'missing_file', 'extra_file', 'missing_mode', 'fold_removed', 'holdout', 'traversal',
                                  'overlap', 'missing_input', 'changed_loss_limit', 'fractional_positions'])
def test_evidence_and_protocol_inconsistencies_are_rejected(db, evidence, attack):
    p, root, folder = evidence['payload'], evidence['root'], evidence['folder']
    if attack == 'registry_missing':
        with db.conn:
            db.conn.execute('DELETE FROM experiments WHERE experiment_id=?', (EXPERIMENT + '-PROTOCOL',))
    elif attack == 'registry_mismatch':
        with db.conn:
            db.conn.execute('UPDATE experiments SET metrics_json=? WHERE experiment_id=?', ('{}', EXPERIMENT))
    elif attack == 'protocol_mismatch':
        dump(folder / 'protocol.json', {})
    elif attack == 'summary_mismatch':
        p['summary']['selected']['mean_fold_return'] = .9
        update_report(db, evidence)
    elif attack == 'missing_file':
        (folder / 'fold-00-selected-equity.parquet').unlink()
    elif attack == 'extra_file':
        pd.DataFrame().to_parquet(folder / 'omitted.parquet')
    elif attack == 'missing_mode':
        del p['results'][0]['modes']['stress']
        update_report(db, evidence)
    elif attack == 'fold_removed':
        p['results'].pop()
        update_report(db, evidence)
    elif attack == 'holdout':
        p['protocol']['folds'][-1]['test_end_exclusive'] = '2026-01-01'
        update_report(db, evidence)
    elif attack == 'traversal':
        p['protocol']['source_files'] = {'../outside.py': 'a' * 64}
        update_report(db, evidence)
    elif attack == 'overlap':
        p['protocol']['folds'][1]['test_start'] = '2023-03-25'
        update_report(db, evidence)
    elif attack == 'missing_input':
        del p['protocol']['inputs']['labels']
        update_report(db, evidence)
    elif attack == 'changed_loss_limit':
        p['results'][0]['modes']['stress']['assumptions']['config']['max_loss_pct'] = .9
        update_report(db, evidence)
    elif attack == 'fractional_positions':
        path = folder / 'fold-00-selected-equity.parquet'
        curve = pd.read_parquet(path).astype({'positions': float})
        curve.loc[1, 'positions'] = .5
        curve.to_parquet(path, index=False)
        update_report(db, evidence)
    with pytest.raises(ValueError):
        review_research(db, EXPERIMENT, root=root)


def test_claim_of_acceptance_cannot_pass_this_development_reader(db, evidence):
    evidence['payload']['accepted_for_signals'] = True
    update_report(db, evidence)
    with pytest.raises(ValueError, match='فاقد مجوز'):
        review_research(db, EXPERIMENT, root=evidence['root'])


def test_input_id_and_json_duplicate_keys_rejected(db, evidence):
    with pytest.raises(ValueError):
        review_research(db, '../anything', root=evidence['root'])
    evidence['report'].write_text('{"protocol":{},"protocol":{}}', encoding='utf-8')
    with pytest.raises(ValueError, match='تکراری'):
        review_research(db, EXPERIMENT, root=evidence['root'])


def test_cli_review_read_only_and_output_private(db, evidence, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    before = db.conn.execute('SELECT COUNT(*) FROM experiments').fetchone()[0]
    out = tmp_path / 'private/review.json'
    assert brs.main(['research-review', '--experiment', EXPERIMENT, '--json', str(out)]) == 0
    assert not json.loads(out.read_text(encoding='utf-8'))['accepted_for_signals']
    assert db.conn.execute('SELECT COUNT(*) FROM experiments').fetchone()[0] == before
    public = tmp_path / 'public.json'
    assert brs.main(['research-review', '--experiment', EXPERIMENT, '--json', str(public)]) == 1
    assert not public.exists()
    assert 'بازبینی شواهد' in capsys.readouterr().out
    with DatabaseManager(db.db_path, read_only=True) as readonly:
        assert review_research(readonly, EXPERIMENT, root=evidence['root'])['verified']['registered_protocol_and_result']


def test_paired_unknowns_and_zero_distinct_from_loss():
    rows = [{'modes': {'a': {'metrics': {'total_return': a}}, 'b': {'metrics': {'total_return': b}}}}
            for a, b in ((0, 0), (.2, .1), (None, .5), (-.3, -.2))]
    compared = paired_comparison(rows, 'a', 'b')
    assert compared['paired_known_folds'] == 3 and compared['excluded_unknown_pairs'] == 1
    assert compared['tied_pairs'] == compared['positive_pairs'] == compared['negative_pairs'] == 1


def test_flow_candidates_have_same_fold_stressed_baseline_comparisons(evidence):
    p = copy.deepcopy(evidence['payload'])
    p['protocol']['version'] = 'flow-portfolio-v3'
    for result in p['results']:
        old = result['modes']
        result['modes'] = {'past_baseline': old['baseline'], 'past_baseline_stress': old['stress'],
                           'retail_acceleration-3d': old['selected'], 'retail_acceleration-3d-stress': old['stress'],
                           'retail_acceleration-3d-random': old['random']}
        for mode in result['modes'].values():
            mode['metrics'].update(max_loss_from_initial=0, loss_floor_breached=False)
    report = summarize_evidence(p, p['results'])
    comparison = report['candidates']['retail_acceleration-3d']['comparisons']['stressed_baseline']
    assert comparison['mean_known_difference'] == 0 and comparison['paired_known_folds'] == 2


def test_flow_archive_end_to_end_checks_all_filters_and_horizons(db, evidence):
    p, folder, root = evidence['payload'], evidence['folder'], evidence['root']
    flow_id = EXPERIMENT.replace('-PORTFOLIO-', '-FLOW-')
    new_folder = root / flow_id
    folder.rename(new_folder)
    p['experiment_id'] = flow_id
    p['protocol'].update(version='flow-portfolio-v3', horizons=[2, 3])
    source = pd.read_parquet(new_folder / 'prices.parquet')
    for path in new_folder.glob('*.parquet'):
        path.unlink()
    p['protocol']['inputs'] = {}
    for name in ('prices', 'clients', 'baseline_features', 'baseline_labels'):
        source.to_parquet(new_folder / f'{name}.parquet', index=False)
        p['protocol']['inputs'][name] = sha256(new_folder / f'{name}.parquet')
    mapping = dict(past_baseline='baseline', past_baseline_stress='stress')
    mapping.update({f'{name}-{h}d{suffix}': mode for name in ('retail_acceleration', 'volume_breakout', 'daily_pressure')
                    for h in (2, 3) for suffix, mode in (('', 'selected'), ('-stress', 'stress'), ('-random', 'random'))})
    for i, fold in enumerate(p['results']):
        old = fold['modes']
        fold['modes'] = {name: copy.deepcopy(old[mode]) for name, mode in mapping.items()}
        for name, mode in mapping.items():
            for kind in ('equity', 'executions', 'picks', 'decisions'):
                # فایل‌های حالت قبلی فقط در پوشه آزمون موقت از حافظه بازسازی می‌شوند.
                if kind == 'equity':
                    ending = old[mode]['metrics']['final_equity_rial']
                    frame = pd.DataFrame(dict(date=[fold['fold']['test_start']], equity_rial=[float(ending)],
                                              positions=[0], stale_positions=[0]))
                else:
                    frame = pd.DataFrame()
                frame.to_parquet(new_folder / f'fold-{i:02d}-{name}-{kind}.parquet', index=False)
    p['summary'] = {name: copy.deepcopy(p['summary'][mode]) for name, mode in mapping.items()}
    p['artifacts_sha256'] = {path.name: sha256(path) for path in new_folder.glob('*.parquet')}
    dump(new_folder / 'protocol.json', p['protocol'])
    dump(root / (flow_id + '.json'), p)
    for name, metrics in ((flow_id + '-PROTOCOL', {}), (flow_id, p['summary'])):
        db.log_experiment(name, 'test_only', 'آزمون', json.dumps(p['protocol']), json.dumps(metrics))
    report = review_research(db, flow_id, root=root)
    assert len(report['summaries']) == 20 and len(report['candidates']) == 6
    assert all(c['comparisons']['stressed_baseline']['paired_known_folds'] == 2 for c in report['candidates'].values())
    assert report['verified']['artifact_count'] == 164
