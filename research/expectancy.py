"""بازده شرطی روزانه؛ مدل پژوهشی با ورودی معلوم، نه احتمال یا توصیه معامله."""

import numpy as np
import pandas as pd

from core.client_quality import finite_number
from core.market_rules import lock_flags
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date
from research.probability import _finite, _numeric

VERSION = 'conditional-expectancy-v1'
CONTEXT_VERSION = 'conditional-context-v1'
PREDICTORS = ('ret_1d', 'ret_5d', 'ret_20d', 'dist_sma_20d', 'rel_ret_5d', 'buyer_power_5d',
              'real_flow_ratio_5d', 'mkt_breadth_5d', 'vol_20d', 'value_ratio_20d')
RIDGE_PENALTY = .01
MIN_SESSIONS = 60
MIN_ROWS = 200
MODEL_VALIDATION_VERSION = 'expectancy-model-contract-v1'
FORECAST_STATUSES = {'model_unavailable', 'unknown_features', 'outside_training_support',
                     'forecasted', 'incoherent_model_prediction'}


def _keys(frame):
    if (not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any()
            or not {'date', 'symbol'} <= set(frame)
            or frame.duplicated(['date', 'symbol']).any()
            or any(not is_canonical_iso_date(day) for day in frame.date.unique())
            or not frame.symbol.map(lambda s: isinstance(s, str) and bool(s.strip())
                                    and normalize_symbol(s) == s).all()):
        raise ValueError('کلید یا ستون تکراری/نامعتبر در ورودی پیش‌بینی')


def _validate_model(model, *, version, predictors):
    """Validate serialized inference state, not authenticity or calibration.

    Valid v1 coefficients and forecasts are unchanged. Inconsistent metadata
    must not silently apply coefficients to a different feature order.
    """
    if not isinstance(model, dict):
        raise ValueError('مدل باید نگاشت صریح باشد')
    required = {'version', 'predictors', 'train_end_exclusive', 'penalty', 'training_rows',
                'unresolved_outcomes', 'incomplete_feature_rows', 'fitted_rows', 'fitted_sessions',
                'min_sessions', 'min_rows', 'max_resolved_at', 'coefficients', 'status',
                'accepted_for_signals', 'probability_of_profit', 'conditions_on_resolved_training'}
    if not required <= set(model):
        raise ValueError('فراداده مدل ناقص است')
    if (model['version'] != version or model['predictors'] != list(predictors)
            or not is_canonical_iso_date(model['train_end_exclusive'])
            or not _finite(model['penalty']) or model['penalty'] != RIDGE_PENALTY
            or model['accepted_for_signals'] is not False or model['probability_of_profit'] is not None
            or model['conditions_on_resolved_training'] is not True):
        raise ValueError('نسخه، ترتیب فیچر یا قرارداد مدل ناسازگار است')
    counts = ('training_rows', 'unresolved_outcomes', 'incomplete_feature_rows',
              'fitted_rows', 'fitted_sessions', 'min_sessions', 'min_rows')
    if any(type(model[k]) is not int or model[k] < 0 for k in counts):
        raise ValueError('شمار پوشش مدل باید صحیح و نامنفی باشد')
    total, fitted = model['training_rows'], model['fitted_rows']
    if (model['min_rows'] < 3 or model['min_sessions'] < 3
            or model['unresolved_outcomes'] > total or model['incomplete_feature_rows'] > total
            or fitted > total-model['unresolved_outcomes']
            or fitted > total-model['incomplete_feature_rows']
            or fitted < max(0, total-model['unresolved_outcomes']-model['incomplete_feature_rows'])
            or model['fitted_sessions'] > fitted or bool(fitted) != bool(model['fitted_sessions'])):
        raise ValueError('شمار پوشش مدل ناسازگار است')
    resolved = model['max_resolved_at']
    if ((fitted and (not is_canonical_iso_date(resolved) or resolved >= model['train_end_exclusive']))
            or not fitted and resolved is not None):
        raise ValueError('زمان نتیجه مدل با مرز آموزش سازگار نیست')
    sufficient = fitted >= model['min_rows'] and model['fitted_sessions'] >= model['min_sessions']
    fitted_fields = {'active_columns', 'center', 'scale', 'minimum', 'maximum', 'unconditioned_mean'}
    if model['coefficients'] is None:
        if model['status'] != 'insufficient_history' or sufficient or fitted_fields & set(model):
            raise ValueError('وضعیت مدل بدون ضرایب ناسازگار است')
        return
    if model['status'] != 'fitted' or not sufficient or not fitted_fields <= set(model):
        raise ValueError('وضعیت یا آرایه‌های مدل برازش‌شده ناسازگار است')

    def vector(name, length):
        value = model[name]
        if (not isinstance(value, list) or len(value) != length
                or any(not _finite(v) for v in value)):
            raise ValueError('آرایه عددی مدل نامعتبر است')
        return np.asarray(value, dtype=float)

    n = len(predictors)
    active = model['active_columns']
    if not isinstance(active, list) or len(active) != n or any(type(v) is not bool for v in active):
        raise ValueError('ماسک فیچر فعال مدل نامعتبر است')
    center, scale = vector('center', n), vector('scale', n)
    lower, upper = vector('minimum', n), vector('maximum', n)
    if ((scale < 0).any() or not np.array_equal(scale > 1e-12, active) or (lower > upper).any()):
        raise ValueError('مقیاس یا دامنه آموزش مدل ناسازگار است')
    tolerance = np.finfo(float).eps * np.maximum(1., np.maximum(np.abs(lower), np.abs(upper))) * 32
    if ((center < lower-tolerance) | (center > upper+tolerance)).any():
        raise ValueError('مرکز مدل خارج از دامنه آموزش است')
    means = vector('unconditioned_mean', 2)
    coefficients = model['coefficients']
    if (not isinstance(coefficients, list) or len(coefficients) != sum(active)+1
            or any(not isinstance(row, list) or len(row) != 2 or any(not _finite(v) for v in row)
                   for row in coefficients)
            or (means < -1).any() or means[1] > means[0]+1e-10):
        raise ValueError('ضرایب یا پاسخ‌های مدل ناسازگار است')


def validate_expectancy_model(model):
    """The original ten-feature family retains its exact serialized contract."""
    _validate_model(model, version=VERSION, predictors=PREDICTORS)


def _features(frame, boundary, *, training):
    _keys(frame)
    if not is_canonical_iso_date(boundary) or not {'date', 'symbol', *PREDICTORS} <= set(frame):
        raise ValueError('مرز، هویت و ستون‌های مدل کامل نیستند')
    if (frame.date.ge(boundary) if training else frame.date.lt(boundary)).any():
        raise ValueError('هویت یا مرز زمانی مدل ناسازگار است')
    forbidden = ('adj_close', 'adjustment_factor', 'entry_date', 'entry_executable')
    if any(c in forbidden or c.startswith(('net_ret_', 'label_status_', 'exit_date_', 'planned_exit_',
                                          'exit_delay_', 'corporate_actions_', 'mfe_', 'mae_')) for c in frame):
        raise ValueError('اطلاعات نتیجه یا تعدیل آینده در ورودی مدل مجاز نیست')
    return np.column_stack([_numeric(frame[c]).to_numpy(dtype=float, na_value=np.nan) for c in PREDICTORS])


def fit_expectancy(features, outcomes, *, train_end_exclusive, min_sessions=MIN_SESSIONS,
                   min_rows=MIN_ROWS, model_family='linear-v1'):
    """دو پاسخ خالص با وزن برابر تاریخ؛ مجهول‌ها در پوشش می‌مانند و در حل جایگزین نمی‌شوند."""
    if model_family == 'context-v1':
        from research.context_expectancy import fit_context_expectancy

        return fit_context_expectancy(features, outcomes, train_end_exclusive=train_end_exclusive,
                                      min_sessions=min_sessions, min_rows=min_rows)
    if model_family != 'linear-v1':
        raise ValueError('خانواده مدل بازده شرطی نامعتبر است')
    if type(min_sessions) is not int or min_sessions < 3 or type(min_rows) is not int or min_rows < 3:
        raise ValueError('کف محاسباتی باید صحیح و حداقل سه باشد')
    x = _features(features, train_end_exclusive, training=True)
    model = _fit_matrix(features, outcomes, x, train_end_exclusive=train_end_exclusive,
                        min_sessions=min_sessions, min_rows=min_rows, version=VERSION, predictors=PREDICTORS)
    validate_expectancy_model(model)
    return model


def _fit_matrix(features, outcomes, x, *, train_end_exclusive, min_sessions, min_rows, version, predictors):
    """Shared fixed Ridge arithmetic; public family wrappers validate their own recipes."""
    _keys(outcomes)
    if not {'state', 'resolved_at', 'normal_net', 'stress_net'} <= set(outcomes):
        raise ValueError('ستون‌های نتیجه آموزش کامل نیستند')
    joined = features[['date', 'symbol']].merge(outcomes, how='left', on=['date', 'symbol'], validate='one_to_one', indicator=True)
    if joined._merge.ne('both').any() or len(joined) != len(outcomes):
        raise ValueError('مخرج ورودی و نتیجه آموزش برابر نیست')
    y = np.column_stack([_numeric(joined[c]).to_numpy(dtype=float, na_value=np.nan) for c in ('normal_net', 'stress_net')])
    known = np.isfinite(y).all(axis=1)
    if (not joined.state.isin(['complete', 'entry_blocked', 'unknown']).all()
            or (joined.state.eq('unknown').to_numpy() != ~known).any()
            or (np.isfinite(y).any(axis=1) != known).any()
            or (joined.state.eq('entry_blocked').to_numpy() & (y != 0).any(axis=1)).any()
            or joined.loc[~known, 'resolved_at'].notna().any()):
        raise ValueError('نامعلومی نتیجه با ارقام آموزشی سازگار نیست')
    if (known & ((y[:, 1] > y[:, 0] + 1e-10) | (y < -1).any(axis=1))).any():
        raise ValueError('بازده فشار/عادی نامعتبر است')
    if (any(not is_canonical_iso_date(day) or day >= train_end_exclusive
            for day in joined.loc[known, 'resolved_at'])
            or joined.loc[known, 'resolved_at'].le(joined.loc[known, 'date']).any()):
        raise ValueError('خروج آموزش پس از مرز مشاهده شده است')
    usable = known & np.isfinite(x).all(axis=1)
    sessions = joined.loc[usable, 'date'].nunique()
    model = dict(version=version, train_end_exclusive=train_end_exclusive, predictors=list(predictors),
        penalty=RIDGE_PENALTY, training_rows=len(joined), unresolved_outcomes=int((~known).sum()),
        incomplete_feature_rows=int((~np.isfinite(x).all(axis=1)).sum()), fitted_rows=int(usable.sum()),
        fitted_sessions=int(sessions), min_sessions=min_sessions, min_rows=min_rows,
        max_resolved_at=max(joined.loc[usable, 'resolved_at'], default=None),
        coefficients=None, status='insufficient_history', accepted_for_signals=False,
        probability_of_profit=None, conditions_on_resolved_training=True)
    if usable.sum() < min_rows or sessions < min_sessions:
        return model
    a, b = x[usable], y[usable]
    counts = joined.loc[usable].groupby('date').symbol.transform('size').to_numpy()
    weights = 1. / (sessions * counts)
    center = np.sum(a * weights[:, None], axis=0)
    try:
        with np.errstate(over='raise', invalid='raise'):
            scale = np.sqrt(np.sum((a - center) ** 2 * weights[:, None], axis=0))
    except FloatingPointError as exc:
        raise ValueError('مقیاس عددی آموزش قابل محاسبه نیست') from exc
    active = scale > 1e-12
    design = np.column_stack([np.ones(len(a)), (a[:, active] - center[active]) / scale[active]])
    penalty = np.eye(design.shape[1]) * RIDGE_PENALTY
    penalty[0, 0] = 0.
    beta = np.linalg.solve(design.T @ (weights[:, None] * design) + penalty, design.T @ (weights[:, None] * b))
    if not np.isfinite(beta).all():
        raise ValueError('حل مدل متناهی نیست')
    model.update(status='fitted', active_columns=active.tolist(), center=center.tolist(), scale=scale.tolist(),
        minimum=a.min(axis=0).tolist(), maximum=a.max(axis=0).tolist(), coefficients=beta.tolist(),
        unconditioned_mean=np.sum(b * weights[:, None], axis=0).tolist())
    return model


def forecast_expectancy(features, model):
    if isinstance(model, dict) and model.get('version') == CONTEXT_VERSION:
        from research.context_expectancy import forecast_context_expectancy

        return forecast_context_expectancy(features, model)
    validate_expectancy_model(model)
    x = _features(features, model['train_end_exclusive'], training=False)
    return _forecast_matrix(features, model, x)


def _forecast_matrix(features, model, x):
    """Inference after the selected family has validated both model and raw feature inputs."""
    out = features[['date', 'symbol']].copy()
    for column in ('normal_expected_net', 'stress_expected_net'):
        out[column] = np.nan
    out['status'] = 'model_unavailable'
    if model['coefficients'] is None:
        return out
    if model['max_resolved_at'] >= model['train_end_exclusive']:
        raise ValueError('زمان آخرین نتیجه مدل با مرز آموزش سازگار نیست')
    active = np.asarray(model['active_columns'], dtype=bool)
    lower, upper = np.asarray(model['minimum']), np.asarray(model['maximum'])
    complete = np.isfinite(x).all(axis=1)
    tolerance = np.finfo(float).eps * np.maximum(1., np.maximum(np.abs(lower), np.abs(upper))) * 32
    supported = complete & ((x >= lower-tolerance) & (x <= upper+tolerance)).all(axis=1)
    out['status'] = np.where(~complete, 'unknown_features', np.where(~supported, 'outside_training_support', 'forecasted'))
    if supported.any():
        center, scale = np.asarray(model['center']), np.asarray(model['scale'])
        design = np.column_stack([np.ones(supported.sum()), (x[supported][:, active] - center[active]) / scale[active]])
        prediction = design @ np.asarray(model['coefficients'])
        admissible = np.isfinite(prediction).all(axis=1) & (prediction >= -1).all(axis=1) & (prediction[:, 1] <= prediction[:, 0] + 1e-10)
        positions = np.flatnonzero(supported)
        out.iloc[positions[admissible], out.columns.get_indexer(['normal_expected_net', 'stress_expected_net'])] = prediction[admissible]
        out.iloc[positions[~admissible], out.columns.get_loc('status')] = 'incoherent_model_prediction'
    return out


def fixed_hypothesis_masks(features, prices):
    """مرزهای خنثی از پیش ثابت؛ فاقد صنعت جاری یا نتیجه فردا."""
    data = {c: _numeric(features[c]) for c in
            ('mkt_breadth_5d', 'dist_sma_20d', 'buyer_power_5d', 'rel_ret_5d', 'ret_3d', 'close_position')}
    # پرچم صفر daily-v7 به‌تنهایی نبود قفل را هنگام قیمت ناقص اثبات نمی‌کند.
    fields = ['open_raw', 'low_raw', 'high_raw', 'close_raw', 'ref_price_raw', 'volume']
    raw = features[['date', 'symbol']].merge(prices[['date', 'symbol', *fields]],
        on=['date', 'symbol'], how='left', validate='one_to_one')
    opening, low, high, last, ref, volume = (finite_number(raw, c) for c in fields)
    valid = (low > 0) & (high >= low) & last.between(low, high) & opening.between(low, high) & (ref > 0) & (volume > 0)
    not_locked = valid.to_numpy() & ~lock_flags(raw).locked_up.to_numpy()
    common = (data['mkt_breadth_5d'].ge(.5) & data['dist_sma_20d'].gt(0)
              & data['buyer_power_5d'].gt(1) & not_locked)
    return dict(relative_trend=common & data['rel_ret_5d'].gt(0),
                orderly_pullback=common & data['ret_3d'].lt(0) & data['close_position'].ge(.5))


def forecast_errors(forecast, outcomes, model):
    if isinstance(model, dict) and model.get('version') == CONTEXT_VERSION:
        from research.context_expectancy import validate_context_model

        validate_context_model(model)
    else:
        validate_expectancy_model(model)
    _keys(forecast)
    _keys(outcomes)
    required = {'state', 'resolved_at', 'normal_net', 'stress_net'}
    if (not required <= set(outcomes)
            or not {'status', 'normal_expected_net', 'stress_expected_net'} <= set(forecast)
            or set(forecast) & set(outcomes) != {'date', 'symbol'}):
        raise ValueError('ستون‌های مستقل پیش‌بینی و نتیجه کامل نیستند')
    rows = forecast.merge(outcomes, on=['date', 'symbol'], how='left', validate='one_to_one', indicator=True)
    if rows._merge.ne('both').any() or len(outcomes) != len(rows):
        raise ValueError('مخرج پیش‌بینی و نتیجه برابر نیست؛ ردیف نامعلوم باید صریح باشد')
    rows = rows.drop(columns='_merge')
    for column in ('normal_net', 'stress_net', 'normal_expected_net', 'stress_expected_net'):
        rows[column] = _numeric(rows[column])
    known = rows.normal_net.notna() & rows.stress_net.notna()
    predicted = rows.normal_expected_net.notna() & rows.stress_expected_net.notna()
    if (not rows.state.isin(['complete', 'entry_blocked', 'unknown']).all()
            or not rows.status.isin(FORECAST_STATUSES).all()
            or rows.state.ne('unknown').ne(known).any()
            or rows.status.eq('forecasted').ne(predicted).any()
            or rows.normal_net.notna().ne(rows.stress_net.notna()).any()
            or rows.normal_expected_net.notna().ne(rows.stress_expected_net.notna()).any()
            or (rows.state.eq('entry_blocked') & (rows.normal_net.ne(0) | rows.stress_net.ne(0))).any()
            or rows.loc[~known, 'resolved_at'].notna().any()
            or rows.date.lt(model['train_end_exclusive']).any()):
        raise ValueError('وضعیت پیش‌بینی یا نتیجه با پوشش سازگار نیست')
    if (any(not is_canonical_iso_date(d) for d in rows.loc[known, 'resolved_at'])
            or rows.loc[known, 'resolved_at'].le(rows.loc[known, 'date']).any()):
        raise ValueError('زمان معلوم‌شدن نتیجه باید بعد از تصمیم باشد')
    for normal, stress in (('normal_net', 'stress_net'), ('normal_expected_net', 'stress_expected_net')):
        if (rows[normal].lt(-1).any() or rows[stress].lt(-1).any()
                or rows[stress].gt(rows[normal]+1e-10).any()):
            raise ValueError('بازده عادی/فشار ناسازگار است')
    output = dict(opportunities=len(rows), forecasts=int(rows.status.eq('forecasted').sum()), scenarios={})
    for i, scenario in enumerate(('normal', 'stress')):
        pred, actual = rows[scenario + '_expected_net'], rows[scenario + '_net']
        valid = pred.notna() & actual.notna()
        baseline = model.get('unconditioned_mean', [None, None])[i]
        output['scenarios'][scenario] = dict(resolved_forecasts=int(valid.sum()), unknown_forecast_outcomes=int((pred.notna() & actual.isna()).sum()),
            mse=float(((pred[valid] - actual[valid]) ** 2).mean()) if valid.any() else None,
            baseline_mse=float(((baseline - actual[valid]) ** 2).mean()) if valid.any() and baseline is not None else None,
            mean_prediction=float(pred[valid].mean()) if valid.any() else None,
            mean_observed_net=float(actual[valid].mean()) if valid.any() else None,
            error_conditions_on_resolved=True)
        # معیار اصلی خطا مانند تابع آموزش به هر تاریخ وزن برابر می دهد.
        daily = rows.loc[valid, ['date']].assign(
            model_squared_error=(pred[valid] - actual[valid]) ** 2,
            baseline_squared_error=(baseline - actual[valid]) ** 2 if baseline is not None else np.nan)
        means = daily.groupby('date')[['model_squared_error', 'baseline_squared_error']].mean()
        output['scenarios'][scenario].update(resolved_forecast_sessions=len(means),
            equal_date_mse=float(means.model_squared_error.mean()) if len(means) else None,
            equal_date_baseline_mse=float(means.baseline_squared_error.mean()) if len(means) and baseline is not None else None)
    return output
