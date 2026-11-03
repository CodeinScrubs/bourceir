"""تشخیص فقط‌خواندنی پنل محفوظ؛ بدون برازش مدل یا انتخاب راهبرد.

هش، بایت‌ها را پیوند می‌دهد؛ اصالت و زمان تاریخی را اثبات نمی‌کند.
بازبینی آزمایش والد مستقل است. این ابزار دیتابیس را باز نمی‌کند.
"""

import argparse
import hashlib
import io
import json
import math
import re
import uuid
from numbers import Integral
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from config import settings
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from research.probability import _numeric

VERSION = 'hunter-attribution-v2'
MAX_BYTES = 512 * 1024 * 1024
STATUSES = {'forecasted', 'model_unavailable', 'unknown_features',
            'outside_training_support', 'incoherent_model_prediction'}
REQUIRED = {'fold', 'date', 'symbol', 'status', 'normal_expected_net', 'stress_expected_net',
            'state', 'resolved_at', 'normal_net', 'stress_net', 'corporate_actions', 'selected'}
THRESHOLDS = ((0., 'positive'), (.03, 'above_3pct'), (.10, 'above_10pct'))
OPTIONAL_FLAGS = {f'{scenario}_missed_{name}' for scenario in ('normal', 'stress') for _, name in THRESHOLDS}


def _dates(frame, cutoff):
    if not is_canonical_iso_date(cutoff) or cutoff > settings.RESEARCH_CONFIG['holdout_start']:
        raise ValueError('مرز باید دوره مهروموم را حفظ کند')
    if not {'date', 'resolved_at'} <= set(frame):
        raise ValueError('تاریخ تصمیم و معلوم‌شدن نتیجه لازم‌اند')
    for column in ('date', 'resolved_at'):
        for day in frame[column].unique():
            if column == 'resolved_at' and pd.isna(day):
                continue
            if not is_canonical_iso_date(day) or day >= cutoff:
                raise ValueError('تاریخ تصمیم یا نتیجه نامعتبر یا در دوره مهروموم است')


def read_panel(path, expected_sha, *, cutoff):
    """ابتدا هر دو ستون تاریخ، سپس ارقام از همان بایت‌های دارای هش خوانده می‌شوند."""
    if not isinstance(expected_sha, str) or not re.fullmatch('[a-f0-9]{64}', expected_sha):
        raise ValueError('اثر انگشت صریح SHA256 لازم است')
    with Path(path).open('rb') as stream:
        body = stream.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES or hashlib.sha256(body).hexdigest() != expected_sha:
        raise ValueError('اندازه یا SHA256 پنل ناسازگار است')
    buffer = io.BytesIO(body)
    columns = pq.read_schema(buffer).names
    if len(set(columns)) != len(columns) or not REQUIRED <= set(columns) or set(columns) - REQUIRED - OPTIONAL_FLAGS:
        raise ValueError('ستون‌های پنل شکار ناقص یا خارج از قراردادند')
    buffer.seek(0)
    _dates(pd.read_parquet(buffer, columns=['date', 'resolved_at']), cutoff)
    buffer.seek(0)
    return pd.read_parquet(buffer)


def checked_panel(frame, *, cutoff):
    if not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any() or not REQUIRED <= set(frame):
        raise ValueError('ستون‌های کامل و یکتا لازم‌اند')
    if set(frame) - REQUIRED - OPTIONAL_FLAGS:
        raise ValueError('ستون پنل خارج از قرارداد است')
    _dates(frame, cutoff)  # تاریخ مهروموم پیش از ارقام مالی رد می‌شود.
    data = frame.copy().reset_index(drop=True)
    if data.empty or data.duplicated(['date', 'symbol']).any():
        raise ValueError('پنل غیرخالی و یکتای نماد–تاریخ لازم است')
    if (not data.symbol.map(lambda v: isinstance(v, str) and bool(v) and normalize_symbol(v) == v).all()
            or not data.fold.map(lambda v: isinstance(v, Integral) and not isinstance(v, (bool, np.bool_)) and v >= 0).all()
            or data.groupby('date').fold.nunique().gt(1).any()):
        raise ValueError('هویت نماد یا برش نامعتبر است')
    if not data.selected.map(lambda v: isinstance(v, (bool, np.bool_))).all():
        raise ValueError('وضعیت انتخاب باید بولی صریح باشد')
    if not data.status.isin(STATUSES).all() or not data.state.isin(['complete', 'entry_blocked', 'unknown']).all():
        raise ValueError('وضعیت پیش‌بینی یا نتیجه نامعتبر است')
    forecasted, known = data.status.eq('forecasted'), data.state.ne('unknown')
    for pair, available in ((('normal_expected_net', 'stress_expected_net'), forecasted),
                            (('normal_net', 'stress_net'), known)):
        a, b = pair
        data[a], data[b] = _numeric(data[a]), _numeric(data[b])
        if (not data[a].notna().equals(available) or not data[b].notna().equals(available)
                or data[a].lt(-1).any() or data[b].lt(-1).any() or data[b].gt(data[a] + 1e-10).any()):
            raise ValueError('ارقام با وضعیت پیش‌بینی یا نتیجه سازگار نیستند')
    data['corporate_actions'] = _numeric(data.corporate_actions)
    if (not data.corporate_actions.notna().equals(known) or data.corporate_actions.lt(0).any()
            or data.corporate_actions.dropna().mod(1).ne(0).any()
            or not data.resolved_at.notna().equals(known)
            or data.loc[known, 'resolved_at'].le(data.loc[known, 'date']).any()):
        raise ValueError('زمان نتیجه یا شمار اقدام شرکتی با وضعیت ناسازگار است')
    blocked = data.state.eq('entry_blocked')
    if data.loc[blocked, ['normal_net', 'stress_net', 'corporate_actions']].ne(0).any().any():
        raise ValueError('ورود مسدود باید نتیجه صفر مشاهده‌شده و عبور شرکتی صفر داشته باشد')
    eligible = forecasted & data.normal_expected_net.gt(0) & data.stress_expected_net.gt(0)
    if (data.selected & ~eligible).any():
        raise ValueError('انتخاب با دروازه مثبت‌بودن برآورد محفوظ ناسازگار است')
    for scenario in ('normal', 'stress'):
        for threshold, name in THRESHOLDS:
            column = f'{scenario}_missed_{name}'
            if column in data:
                # The parent's flags are tri-state: unknown outcome -> unknown
                # missed opportunity. NaN > threshold is False in pandas; that
                # numerical comparison must not turn an unresolved row into a
                # known negative. Compare values after an explicit dtype cast.
                expected = (~data.selected & data[scenario + '_net'].gt(threshold)).astype(
                    'boolean').where(known, pd.NA)
                if (not data[column].notna().equals(known)
                        or not data.loc[known, column].map(lambda v: isinstance(v, (bool, np.bool_))).all()
                        or not data[column].astype('boolean').equals(expected)):
                    raise ValueError('پرچم فرصت ازدست‌رفته با ارقام سطر سازگار نیست')
    return data.sort_values(['fold', 'date', 'symbol']).reset_index(drop=True)


def _mean(values):
    result = math.fsum(float(value) / len(values) for value in values) if len(values) else None
    return result if result is None or math.isfinite(result) else None


def _scenario(data, scenario):
    actual, predicted = data[scenario + '_net'], data[scenario + '_expected_net']
    known = actual.notna()
    paired = known & predicted.notna()
    errors = actual[paired] - predicted[paired]
    daily = pd.DataFrame({'date': data.loc[paired, 'date'], 'error': errors,
                          'actual': actual[paired], 'predicted': predicted[paired]})
    means = daily.groupby('date')[['error', 'actual', 'predicted']].mean()
    return dict(rows=len(data), known=int(known.sum()), unknown=int((~known).sum()),
        complete_proxy_trades=int(data.state.eq('complete').sum()), blocked_entries=int(data.state.eq('entry_blocked').sum()),
        known_positive=int(actual.gt(0).sum()), known_negative=int(actual.lt(0).sum()), known_zero=int(actual.eq(0).sum()),
        known_above_3pct=int(actual.gt(.03).sum()), known_above_10pct=int(actual.gt(.10).sum()),
        positive_fraction_known=float(actual.gt(0).sum() / known.sum()) if known.any() else None,
        positive_fraction_identification_bounds=[float(actual.gt(0).sum() / len(data)),
            float((actual.gt(0).sum() + (~known).sum()) / len(data))] if len(data) else None,
        selected=int(data.selected.sum()), selected_known_positive=int((data.selected & actual.gt(0)).sum()),
        mean_known_net=_mean(actual[known]), all_row_mean_net=_mean(actual) if known.all() else None,
        mean_positive_net=_mean(actual[actual.gt(0)]), mean_negative_net=_mean(actual[actual.lt(0)]),
        paired_rows=int(paired.sum()), paired_sessions=len(means),
        paired_mean_prediction=_mean(predicted[paired]), paired_mean_observed=_mean(actual[paired]),
        paired_bias_observed_minus_prediction=_mean(errors),
        equal_date_bias_observed_minus_prediction=_mean(means.error),
        equal_date_mean_observed=_mean(means.actual), equal_date_mean_prediction=_mean(means.predicted),
        error_conditions_on_resolved=True)


def _ranking(data, scenario):
    pred, net = scenario + '_expected_net', scenario + '_net'
    forecasted = data.loc[data.status.eq('forecasted')].copy()
    # دهک هر تاریخ فقط از پیش‌بینی محفوظ همان تاریخ به دست می‌آید.
    # رتبه میانگین، مقادیر برابر را جدا نمی‌کند؛ نتیجه در رتبه مصرف نمی‌شود.
    forecasted['decile'] = np.ceil(forecasted.groupby('date')[pred].rank(method='average', pct=True) * 10).astype(int)
    daily = []
    for day, group in forecasted.groupby('date', sort=True):
        pairs = group.loc[group[net].notna()]
        ic = None
        if len(pairs) >= 3 and pairs[pred].nunique() > 1 and pairs[net].nunique() > 1:
            ic = float(pairs[pred].rank().corr(pairs[net].rank()))
        daily.append(dict(date=day, forecasts=len(group), resolved=len(pairs), unknown=len(group)-len(pairs),
                          spearman_on_resolved=ic, all_forecast_outcomes_known=len(pairs) == len(group)))
    valid = [r['spearman_on_resolved'] for r in daily if r['spearman_on_resolved'] is not None]
    complete = [r['spearman_on_resolved'] for r in daily
                if r['spearman_on_resolved'] is not None and r['all_forecast_outcomes_known']]
    return dict(forecast_sessions=len(daily), defined_ic_sessions=len(valid), undefined_ic_sessions=len(daily)-len(valid),
        sessions_with_unknown_outcomes=sum(r['unknown'] > 0 for r in daily),
        mean_daily_spearman_on_resolved=float(np.mean(valid)) if valid else None,
        complete_outcome_defined_ic_sessions=len(complete),
        mean_daily_spearman_on_complete_dates=float(np.mean(complete)) if complete else None,
        deciles=[dict(decile=i, **_scenario(forecasted.loc[forecasted.decile.eq(i)], scenario)) for i in range(1, 11)],
        daily=daily, ties_kept_together=True, p_value=None, confidence_interval=None)


def diagnose_panel(frame, *, cutoff):
    data = checked_panel(frame, cutoff=cutoff)
    forecasted = data.status.eq('forecasted')
    gates = pd.Series('not_forecasted', index=data.index)
    gates.loc[forecasted & data.normal_expected_net.le(0)] = 'normal_nonpositive'
    gates.loc[forecasted & data.normal_expected_net.gt(0) & data.stress_expected_net.le(0)] = 'stress_only_rejection'
    passed = forecasted & data.normal_expected_net.gt(0) & data.stress_expected_net.gt(0)
    gates.loc[passed & ~data.selected] = 'positive_gate_not_selected'
    gates.loc[data.selected] = 'selected'
    names = ['not_forecasted', 'normal_nonpositive', 'stress_only_rejection', 'positive_gate_not_selected', 'selected']
    def group_summary(group):
        return {scenario: _scenario(group, scenario) for scenario in ('normal', 'stress')}
    return dict(version=VERSION, cutoff_exclusive=cutoff, opportunities=len(data),
        min_date=data.date.min(), max_date=data.date.max(),
        forecast_status_counts={str(k): int(v) for k, v in data.status.value_counts().items()},
        outcome_status_counts={str(k): int(v) for k, v in data.state.value_counts().items()},
        summary=group_summary(data),
        gates={name: group_summary(data.loc[gates.eq(name)]) for name in names},
        corporate_action_strata={
            'known_zero_crossings': group_summary(data.loc[data.corporate_actions.eq(0)]),
            'known_crossings': group_summary(data.loc[data.corporate_actions.gt(0)]),
            'unknown_crossings': group_summary(data.loc[data.corporate_actions.isna()])},
        folds=[dict(fold=int(fold), summary=group_summary(group),
                    ranking={scenario: _ranking(group, scenario) for scenario in ('normal', 'stress')})
               for fold, group in data.groupby('fold', sort=True)],
        accepted_for_signals=False, holdout_eligible=False, profitability_proven=False,
        parent_experiment_reverified=False, external_authenticity_verified=False, new_model_fitted=False,
        historical_universe_completeness_verified=False,
        limitations=[
            'Retrospective attribution of the supplied panel, not independent validation or a new strategy.',
            'Rows and ranks are not portfolio fills, cash returns, or whole-historical-market coverage.',
            'Conditional means and rank correlations exclude unknown values only with explicit counts.',
            'Rank deciles preserve ties; small or tied daily panels can have empty deciles.',
            'Known zero synthetic crossings do not verify real corporate entitlements.',
            'Positive gate nonselection cannot be attributed to a rank cap without replaying the selector.',
            'No probability calibration, statistical significance, or model acceptance is inferred.',
            'Identification bounds describe unresolved outcomes; they are not confidence intervals.',
            'The all-row mean is withheld if any outcome in that group is unknown.',
        ])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--panel', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    args = parser.parse_args(argv)
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    panel = read_panel(args.panel, args.sha256, cutoff=cutoff)
    result = diagnose_panel(panel, cutoff=cutoff)
    result.update(input_sha256=args.sha256, created_at=now_tehran_iso(),
                  diagnostic_code_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest())
    # پوشه خصوصی تازه؛ ورودی و گزارش پیشین بازنویسی نمی‌شوند.
    folder = settings.DATA_DIR / 'private' / 'hunter-attribution' / uuid.uuid4().hex
    folder.mkdir(parents=True, exist_ok=False)
    target = folder / 'diagnostics.json'
    temporary = folder / 'diagnostics.tmp'
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    temporary.replace(target)
    print(target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
