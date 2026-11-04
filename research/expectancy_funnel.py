"""Explain recorded forecasts, selections and non-execution; never accept a strategy."""

import argparse
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.db import DatabaseManager
from ops.report_files import private_report_path, write_private_report
from ops.research_review import _load_json, _relative_file, read_development_parquet, sha256
from research.expectancy import forecast_expectancy
from research.expectancy_run import CANDIDATES, PATTERN, _rank, registered_model_family
from research.probability import _numeric

VERSION = 'recorded-expectancy-funnel-v1'
FORECAST_STATUSES = {'forecasted', 'model_unavailable', 'unknown_features',
                     'outside_training_support', 'incoherent_model_prediction'}


def forecast_funnel(forecast, *, selection_end, max_positions):
    """Disjoint exclusions retain unknown rows; a positive forecast is not a fill."""
    columns = {'date', 'symbol', 'normal_expected_net', 'stress_expected_net', 'status'}
    if (set(forecast) != columns or not is_canonical_iso_date(selection_end)
            or type(max_positions) is not int or max_positions < 1
            or forecast.duplicated(['date', 'symbol']).any()
            or any(not is_canonical_iso_date(day) for day in forecast.date.unique())
            or not forecast.symbol.map(lambda s: isinstance(s, str) and bool(s) and normalize_symbol(s) == s).all()
            or not forecast.status.isin(FORECAST_STATUSES).all()):
        raise ValueError('Recorded forecast identity, columns, statuses or selection boundary differ')
    normal, stress = (_numeric(forecast[column]) for column in ('normal_expected_net', 'stress_expected_net'))
    known = forecast.status.eq('forecasted')
    both_finite = np.isfinite(normal) & np.isfinite(stress)
    any_finite = np.isfinite(normal) | np.isfinite(stress)
    if ((known != both_finite).any() or (any_finite & ~known).any()
            or (known & ((stress > normal + 1e-10) | (normal < -1) | (stress < -1))).any()):
        raise ValueError('Unknown forecasts cannot contain numbers; forecasted pairs must be coherent')
    both = known & normal.gt(0) & stress.gt(0)
    timely = forecast.date.lt(selection_end)
    picks = _rank(forecast, pd.concat([normal, stress], axis=1).min(axis=1, skipna=False),
                  both & timely, max_positions)
    counts = dict(recorded_opportunities=len(forecast), decision_dates=int(forecast.date.nunique()),
        forecast_status_counts={key: int(value) for key, value in forecast.status.value_counts().items()},
        forecasted_rows=int(known.sum()), unknown_forecast_rows=int((~known).sum()),
        normal_positive_rows=int((known & normal.gt(0)).sum()), stress_positive_rows=int((known & stress.gt(0)).sum()),
        both_positive_rows=int(both.sum()), nonpositive_normal_rows=int((known & normal.le(0)).sum()),
        positive_normal_nonpositive_stress_rows=int((known & normal.gt(0) & stress.le(0)).sum()),
        positive_but_too_late_rows=int((both & ~timely).sum()),
        positive_before_rank_rows=int((both & timely).sum()), selected_rows=len(picks),
        excluded_by_rank_rows=int((both & timely).sum())-len(picks),
        selected_dates=int(picks.date.nunique()),
        maximum_normal_expected_net=None if not known.any() else float(normal[known].max()),
        maximum_stress_expected_net=None if not known.any() else float(stress[known].max()),
        probability_of_profit=None, accepted_for_signals=False)
    return counts, picks


def entry_funnel(picks, executions, decisions, days):
    """Associate next-session buys or explicit entry rejections; missing logs stay unknown."""
    if (not days or days != sorted(set(days)) or any(not is_canonical_iso_date(day) for day in days)
            or not {'date', 'symbol', 'rank'} <= set(picks) or picks.duplicated(['date', 'symbol']).any()):
        raise ValueError('Entry attribution requires the fixed session calendar and unique picks')
    positions = {day: index for index, day in enumerate(days)}
    expected = {}
    for pick in picks.itertuples(index=False):
        if pick.date not in positions or positions[pick.date] + 1 >= len(days):
            raise ValueError('Selected decision has no next session in the recorded fold')
        expected[(days[positions[pick.date] + 1], pick.symbol)] = (pick.date, pick.symbol)
    bought = set()
    if not executions.empty:
        if not {'date', 'symbol', 'side'} <= set(executions) or not executions.side.isin(['buy', 'sell']).all():
            raise ValueError('Execution identity and side are required')
        for row in executions.loc[executions.side.eq('buy')].itertuples(index=False):
            key = (row.date, row.symbol)
            if key not in expected or expected[key] in bought:
                raise ValueError('Buy execution does not match one unique next-session selection')
            bought.add(expected[key])
    blocked = {}
    if not decisions.empty:
        if not {'date', 'symbol', 'status'} <= set(decisions):
            raise ValueError('Decision identity and reason are required')
        if 'decision_date' in decisions:
            for row in decisions.loc[decisions.decision_date.notna()].itertuples(index=False):
                key, selected = (row.date, row.symbol), (row.decision_date, row.symbol)
                if expected.get(key) != selected or selected in bought or selected in blocked or not isinstance(row.status, str) or not row.status:
                    raise ValueError('Entry rejection conflicts with selection, timing or execution')
                blocked[selected] = row.status
    reasons = pd.Series(list(blocked.values()), dtype=object).value_counts().to_dict()
    unresolved = len(picks)-len(bought)-len(blocked)
    return dict(selected_rows=len(picks), buy_execution_rows=len(bought),
        explicit_nonexecution_rows=len(blocked),
        explicit_nonexecution_reasons={key: int(value) for key, value in reasons.items()},
        unattributed_selected_rows=unresolved, attribution_complete=unresolved == 0,
        live_fills_verified=False, accepted_for_signals=False)


def inspect_archive(db, source_id, *, root=None):
    """Recompute forecasts/picks and reconcile recorded entry evidence, without outcome tuning."""
    if not isinstance(source_id, str) or not re.fullmatch(PATTERN, source_id):
        raise ValueError('An explicit EXPECTANCY experiment identity is required')
    root = Path(root or settings.DATA_DIR/'research').resolve()
    folder, source = root/source_id, root/(source_id+'.json')
    source_hash, payload = sha256(source), _load_json(source)
    protocol = payload['protocol']
    model_family, _, _ = registered_model_family(protocol)
    if (payload['experiment_id'] != source_id
            or payload['accepted_for_signals'] is not False or payload['holdout_eligible'] is not False
            or protocol['candidate_modes'] != list(CANDIDATES)
            or not is_canonical_iso_date(protocol['holdout_start'])
            or protocol['holdout_start'] > settings.RESEARCH_CONFIG['holdout_start']
            or not protocol['folds'] or len(protocol['folds']) != len(payload['results'])
            or _load_json(folder/'protocol.json') != protocol):
        raise ValueError('Recorded experiment contract or development scope differs')
    for fold in protocol['folds']:
        dates = [fold[key] for key in ('train_start', 'train_end_exclusive', 'test_start', 'test_end_exclusive')]
        if any(not is_canonical_iso_date(day) for day in dates) or not dates[0] < dates[1] < dates[2] < dates[3] <= protocol['holdout_start']:
            raise ValueError('Reject invalid or sealed folds before any numerical forecast is read')
    for name, metrics in ((source_id, payload['summary']), (source_id+'-PROTOCOL', {})):
        row = db.conn.execute('SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?',
                              (name, 'completed')).fetchone()
        if row is None or json.loads(row['parameters_json']) != protocol or json.loads(row['metrics_json']) != metrics:
            raise ValueError('Original experiment registration differs')
    if payload['artifacts_sha256'].get('features.parquet') != protocol['inputs']['features']:
        raise ValueError('Feature bytes differ from the registered source input')
    consumed = {'../'+source.name: source_hash, 'protocol.json': sha256(folder/'protocol.json')}
    model_code = ['research/expectancy.py', 'research/expectancy_run.py']
    if model_family == 'context-v1':
        model_code.append('research/context_expectancy.py')
    for name in model_code:
        code = _relative_file(folder/'code', name)
        if (sha256(code) != protocol['source_files'][name]
                or code.read_bytes() != (settings.BASE_DIR/name).read_bytes().replace(b'\r\n', b'\n')):
            raise ValueError('Forecast contract changed; do not silently reinterpret the old model')
        consumed['code/'+name] = sha256(code)

    def table(name, *, require_date=True):
        file = _relative_file(folder, name)
        expected = payload['artifacts_sha256'][name]
        frame = read_development_parquet(file, expected, protocol['holdout_start'], require_date=require_date)
        consumed[name] = expected
        return frame

    features = table('features.parquet')
    rows = []
    for index, (fold, record) in enumerate(zip(protocol['folds'], payload['results'], strict=True)):
        if record['fold'] != fold:
            raise ValueError('Recorded result fold differs')
        name = f'fold-{index:02d}-model.json'
        model_file = _relative_file(folder, name)
        if sha256(model_file) != payload['models_sha256'][name]:
            raise ValueError('Recorded model bytes differ')
        consumed[name] = sha256(model_file)
        model = _load_json(model_file)
        if model['train_end_exclusive'] != fold['train_end_exclusive']:
            raise ValueError('Saved model training boundary differs')
        forecast = table(f'fold-{index:02d}-forecasts.parquet')
        if forecast.date.lt(fold['test_start']).any() or forecast.date.ge(fold['test_end_exclusive']).any():
            raise ValueError('Forecast identity exceeds its recorded test fold')
        own = features.merge(forecast[['date', 'symbol']], on=['date', 'symbol'], how='inner', validate='one_to_one')
        pd.testing.assert_frame_equal(forecast.reset_index(drop=True), forecast_expectancy(own, model).reset_index(drop=True), check_dtype=False)
        days = table(f'fold-{index:02d}-idle_cash-equity.parquet').date.tolist()
        if not days or any(not fold['test_start'] <= day < fold['test_end_exclusive'] for day in days):
            raise ValueError('Recorded session calendar exceeds its fold')
        funnel, selected = forecast_funnel(forecast, selection_end=days[max(0, len(days)-3)],
                                          max_positions=protocol['config']['max_positions'])
        modes = {}
        for mode in ('conditional-2d', 'conditional-1d', 'conditional-2d-stress', 'conditional-1d-stress'):
            picks = table(f'fold-{index:02d}-{mode}-picks.parquet', require_date=False)
            pd.testing.assert_frame_equal(picks.reset_index(drop=True), selected.reset_index(drop=True), check_dtype=False)
            executions = table(f'fold-{index:02d}-{mode}-executions.parquet', require_date=False)
            decisions = table(f'fold-{index:02d}-{mode}-decisions.parquet', require_date=False)
            modes[mode] = entry_funnel(picks, executions, decisions, days)
            if len(executions) != record['modes'][mode]['metrics']['executions']:
                raise ValueError('Recorded execution count differs from its table')
        training_mean = model.get('unconditioned_mean')
        rows.append(dict(fold=index, period=fold, forecasts=funnel, entries=modes,
            fitted_training_rows=model['fitted_rows'], unresolved_training_outcomes=model['unresolved_outcomes'],
            stress_training_mean=None if training_mean is None else training_mean[1]))
    for name, expected in consumed.items():
        file = source if name.startswith('../') else _relative_file(folder, name)
        if sha256(file) != expected:
            raise ValueError('Source changed during the forecast audit')
    return dict(version=VERSION, source_id=source_id, source_sha256=source_hash,
        audit_code_sha256=sha256(Path(__file__)),
        consumed_files_sha256=consumed, rows=rows, forecast_rows_recomputed=True,
        conditional_picks_recomputed=True, model_retrained=False, execution_replayed=False,
        recorded_opportunity_population_verified=False, historical_source_authenticity_verified=False,
        source_archive_required=True, holdout_opened=False, probability_of_profit=None,
        accepted_for_signals=False, economic_profit_verified=False,
        limitations=['Describes recorded opportunities, not a newly fitted or independently tested strategy',
            'Forecast/pick reconstruction is not a complete financial or bootstrap replay',
            'Missing entry attribution remains unknown; recorded fills do not prove live fills'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args(argv)
    target = private_report_path(args.output, forbidden_paths=(settings.DB_PATH,))
    if target.exists() or not settings.DB_PATH.exists():
        parser.error('Use a new private output and an existing source database')
    with DatabaseManager(read_only=True) as db:
        result = inspect_archive(db, args.source)
    result['generated_at'] = now_tehran_iso()
    write_private_report(target, result)
    print(json.dumps(dict(report=str(target), folds=len(result['rows']),
        selected_rows=sum(row['forecasts']['selected_rows'] for row in result['rows']),
        accepted_for_signals=False), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
