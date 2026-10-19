"""پیش‌بینی فراوانی از گذشته و ارزیابی زمانی؛ احتمال اجرای واقعی یا مجوز معامله نیست."""

import math
from numbers import Real

import numpy as np
import pandas as pd

from core.timeutils import is_canonical_iso_date
from research.baselines import STRATEGIES, pick_top

VERSION = 'temporal-frequency-v1'
TARGET = 'positive net daily-proxy return per resolved selected opportunity; blocked entry is not a win'
MIN_TRAIN_SESSIONS = 60  # کف محاسباتی ثابت است؛ کافی‌بودن آماری را ثابت نمی‌کند.
BIN_EDGES = tuple(i / 10 for i in range(11))


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, (bool, np.bool_)) and math.isfinite(value)


def _numeric(series):
    if any(not _finite(v) and not (v is None or v is pd.NA or isinstance(v, (float, np.floating)) and np.isnan(v)) for v in series):
        raise ValueError('مقدار باید عدد محدود یا نامعلوم باشد؛ متن و بولی پذیرفته نیست')
    return pd.to_numeric(series, errors='coerce')


def opportunity_features(features, labels, *, prices=None):
    """قیمت/ارزش روز تصمیم مجاز است؛ وضعیت و بازده آینده وارد رتبه‌بندی نمی‌شوند."""
    if features.duplicated(['date', 'symbol']).any() or labels.duplicated(['date', 'symbol']).any():
        raise ValueError('کلید نماد/تاریخ تکراری است')
    forbidden = ('net_ret_', 'label_status_', 'exit_date_', 'planned_exit_', 'corporate_actions_', 'mfe_', 'mae_', 'exit_delay_')
    if any(c.startswith(forbidden) or c in ('entry_date', 'entry_executable') for c in features):
        raise ValueError('اطلاعات نتیجه نباید در ورودی رتبه‌بندی باشد')
    if 'value' in features:
        raise ValueError('ارزش روز تصمیم فقط از ورودی خام برچسب محفوظ خوانده می‌شود')
    nontrading = 0
    if prices is not None:
        if prices.duplicated(['date', 'symbol']).any():
            raise ValueError('کلید قیمت خام تکراری است')
        observed = features[['date', 'symbol']].merge(prices[['date', 'symbol', 'volume', 'value']],
            on=['date', 'symbol'], how='left', validate='one_to_one', indicator=True)
        if observed._merge.ne('both').any():
            raise ValueError('فیچر بدون قیمت خام متناظر موجود است')
        # labels فقط جلسه‌های با معامله را دارد؛ صرفاً عدم معامله مشاهده‌شده
        # با حجم و ارزش هردو صفر از جامعه تصمیم حذف و در پوشش افشا می‌شود.
        no_trade = _numeric(observed.volume).eq(0).fillna(False) & _numeric(observed.value).eq(0).fillna(False)
        if observed.loc[no_trade, ['date', 'symbol']].merge(
                labels[['date', 'symbol']], on=['date', 'symbol'], how='inner').shape[0]:
            raise ValueError('برچسب معامله با شاهد خام عدم معامله سازگار نیست')
        nontrading = int(no_trade.sum())
        features = features.iloc[np.flatnonzero(~no_trade.to_numpy())]
    merged = features.merge(labels[['date', 'symbol', 'value']], on=['date', 'symbol'], how='left',
                            validate='one_to_one', indicator=True)
    if merged['_merge'].ne('both').any():
        raise ValueError('بخشی از فیچرها برچسب/ارزش متناظر ندارد؛ حذف خاموش فرصت مجاز نیست')
    output = merged.drop(columns='_merge')
    output.attrs['observed_nontrading_feature_rows'] = nontrading
    return output


def select_opportunities(features, strategy, k):
    rule = next((s for s in STRATEGIES if s.name == strategy), None)
    if rule is None:
        raise ValueError('فقط قواعد پایه ثابت پشتیبانی می‌شوند')
    selected = pick_top(features, rule.score(features), k)
    return selected[['date', 'symbol', '_score']].reset_index(drop=True)


def resolved_outcomes(rows, horizon, as_of):
    """نتیجه فقط پس از پایان واقعی مشاهده می‌شود؛ خروج نامعلوم و توقف، زیان صفر نیستند."""
    if not is_canonical_iso_date(as_of) or type(horizon) is not int or horizon < 1:
        raise ValueError('مرز مشاهده و افق معتبر لازم است')
    status, exit_col, net_col = f'label_status_{horizon}d', f'exit_date_{horizon}d', f'net_ret_{horizon}d'
    required = ['date', 'symbol', 'entry_date', 'entry_executable', status, exit_col, net_col,
                net_col + '_worst', f'corporate_actions_{horizon}d']
    if not set(required) <= set(rows):
        raise ValueError('ستون‌های وضعیت، اجرای ورود یا بازده خالص ناقص‌اند')
    rows = rows.reset_index(drop=True)
    result = rows[['date', 'symbol']].copy()
    for column, allow_empty in (('date', False), ('entry_date', True), (exit_col, True)):
        unique = rows[column].unique()
        if any(not (allow_empty and v == '') and not is_canonical_iso_date(v) for v in unique):
            raise ValueError('تاریخ تصمیم/ورود/خروج نامعتبر است')
    if (rows['date'].ge(as_of).any() or not rows['symbol'].map(lambda v: isinstance(v, str) and bool(v)).all()
            or not rows[status].isin(['complete', 'entry_blocked', 'invalid_data', 'censored']).all()):
        raise ValueError('هویت، وضعیت یا تاریخ تصمیم نامعتبر است')
    if not rows['entry_executable'].map(lambda v: isinstance(v, (bool, np.bool_))).all():
        raise ValueError('وضعیت اجرای ورود باید بولی باشد')
    entry, exit_at = rows['entry_date'], rows[exit_col]
    if (entry.ne('') & entry.le(rows['date'])).any():
        raise ValueError('ورود باید پس از تصمیم باشد')
    numeric = rows[[net_col, net_col + '_worst', f'corporate_actions_{horizon}d']]
    if any(not _finite(v) and not pd.isna(v) for v in numeric.to_numpy().ravel()):
        raise ValueError('بازده/شمار اقدام شرکتی باید عدد محدود یا نامعلوم باشد')
    normal, stress, actions = (pd.to_numeric(numeric[c], errors='coerce') for c in numeric)
    complete, blocked = rows[status].eq('complete'), rows[status].eq('entry_blocked')
    valid = (entry.ne('') & rows['entry_executable'] & exit_at.gt(entry) & normal.notna() & stress.notna()
             & actions.notna() & normal.ge(-1) & stress.ge(-1) & stress.le(normal + 1e-10)
             & actions.ge(0) & actions.mod(1).eq(0))
    if (complete & ~valid).any():
        raise ValueError('برچسب کامل با ورود/خروج یا بازده/فشار سازگار نیست')
    if (blocked & (entry.eq('') | rows['entry_executable'] | normal.notna() | stress.notna() | exit_at.ne(''))).any():
        raise ValueError('عدم ورود نباید اجرای پرشده یا بازده عددی داشته باشد')
    if (~complete & ~blocked & (normal.notna() | stress.notna())).any():
        raise ValueError('نتیجه نامعلوم نباید بازده عددی داشته باشد')
    observed_complete = complete & exit_at.lt(as_of)
    observed_block = blocked & entry.lt(as_of)
    result['resolved_at'] = exit_at.where(observed_complete, entry.where(observed_block, None))
    result['state'] = np.where(observed_complete, 'complete', np.where(observed_block, 'entry_blocked', 'unknown'))
    result['entry_state'] = np.where(entry.ne('') & entry.lt(as_of) & rows['entry_executable'], 'filled',
                                     np.where(observed_block, 'blocked', 'unknown'))
    result['normal_net'] = normal.where(observed_complete, np.where(observed_block, 0., np.nan))
    result['stress_net'] = stress.where(observed_complete, np.where(observed_block, 0., np.nan))
    result['corporate_actions'] = actions.where(observed_complete, np.where(observed_block, 0., np.nan))
    return result


def fit_frequency(outcomes, scenario, *, min_sessions=MIN_TRAIN_SESSIONS):
    """برآورد روی نتایج معلوم شرط دارد؛ کرانِ نامعلوم، فاصله اطمینان نیست."""
    if scenario not in ('normal', 'stress') or type(min_sessions) is not int or min_sessions < 1:
        raise ValueError('سناریو یا حداقل سابقه نامعتبر است')
    values = outcomes[scenario + '_net']
    known = values.notna()
    wins = int(values.gt(0).sum())
    total, n = len(values), int(known.sum())
    sessions = int(outcomes.loc[known, 'date'].nunique())
    supported = n > 0 and sessions >= min_sessions
    gains, losses = values[values > 0], -values[values < 0]
    gain, loss = (float(s.mean()) if len(s) else None for s in (gains, losses))
    return dict(opportunities=total, resolved=n, unresolved=total-n, resolved_sessions=sessions,
                wins=wins, losses=int(values.lt(0).sum()), zero_returns=int(values.eq(0).sum()),
                blocked_entries=int(outcomes['state'].eq('entry_blocked').sum()),
                corporate_action_opportunities=int(outcomes['corporate_actions'].gt(0).sum()),
                probability=wins / n if supported else None,
                empirical_success_fraction_lower=wins / total if total else None,
                empirical_success_fraction_upper=(wins + total - n) / total if total else None,
                mean_net_return=float(values[known].mean()) if supported else None,
                average_gain=gain, average_loss=loss,
                payoff_break_even_probability=loss / (gain + loss) if gain is not None and loss is not None else None,
                max_resolved_at=max(outcomes.loc[known, 'resolved_at'], default=None),
                probability_conditions_on_resolved_labels=True, min_train_sessions=min_sessions,
                sample_floor_is_not_statistical_sufficiency=True,
                status='estimated' if supported else 'insufficient_resolved_history')


def forecast_opportunities(selected, models, base_models, *, fold_index, horizon, strategy, train_end_exclusive):
    """فقط انتخاب‌های روز تصمیم و مدل آموزش‌دیده؛ این تابع نتیجه آزمون را دریافت نمی‌کند."""
    if (not is_canonical_iso_date(train_end_exclusive) or selected.date.lt(train_end_exclusive).any()
            or any(m['max_resolved_at'] is not None and m['max_resolved_at'] >= train_end_exclusive
                   for source in (models, base_models) for m in source.values())):
        raise ValueError('مدل یا فرصت با مرز زمانی آموزش سازگار نیست')
    output = selected.copy()
    output['fold'] = fold_index
    output['horizon'] = horizon
    output['strategy'] = strategy
    output['train_end_exclusive'] = train_end_exclusive
    for scenario in ('normal', 'stress'):
        model, base = models[scenario], base_models[scenario]
        for name, source in (('probability', model['probability']), ('base_probability', base['probability']),
                             ('expected_net', model['mean_net_return'])):
            output[scenario + '_' + name] = source
    return output


def _bounds(values):
    return dict(lower=float(values[0]), upper=float(values[1]))


def score_forecasts(ledger, scenario):
    """مخرج تمام فرصت‌ها محفوظ است؛ امتیاز معلوم با کرانِ نتایج نامعلوم گزارش می‌شود."""
    if scenario not in ('normal', 'stress'):
        raise ValueError('سناریوی امتیازدهی نامعتبر است')
    p = _numeric(ledger[scenario + '_probability'])
    base = _numeric(ledger[scenario + '_base_probability'])
    net = _numeric(ledger[scenario + '_net'])
    if (not ledger['state'].isin(['complete', 'entry_blocked', 'unknown']).all()
            or not ledger['entry_state'].isin(['filled', 'blocked', 'unknown']).all()
            or (ledger.state.eq('unknown') & net.notna()).any()
            or (ledger.state.ne('unknown') & net.isna()).any()
            or (ledger.state.eq('entry_blocked') & (net.ne(0) | ledger.entry_state.ne('blocked'))).any()
            or (ledger.state.eq('complete') & ledger.entry_state.ne('filled')).any()):
        raise ValueError('وضعیت ورود/خروج با نتیجه محفوظ ناسازگار است')
    for probabilities in (p, base):
        if ((probabilities.notna()) & (~np.isfinite(probabilities) | (probabilities < 0) | (probabilities > 1))).any():
            raise ValueError('احتمال باید نامعلوم یا بین صفر و یک باشد')
    if (net.notna() & (~np.isfinite(net) | (net < -1))).any():
        raise ValueError('بازده نامعتبر است')
    known = net.notna()
    forecasted = p.notna()
    scored = forecasted & known
    paired = scored & base.notna()
    event = net.gt(0).astype(float).where(known)
    errors = (p - event) ** 2
    base_errors = (base - event) ** 2
    unscored = forecasted & ~known
    lower = errors[scored].sum() + np.minimum(p[unscored] ** 2, (1 - p[unscored]) ** 2).sum()
    upper = errors[scored].sum() + np.maximum(p[unscored] ** 2, (1 - p[unscored]) ** 2).sum()
    total, n = len(ledger), int(known.sum())
    wins = int(net.gt(0).sum())
    brier = float(errors[scored].mean()) if scored.any() else None
    pair_brier = float(errors[paired].mean()) if paired.any() else None
    base_brier = float(base_errors[paired].mean()) if paired.any() else None
    bins = []
    for index, (lo, hi) in enumerate(zip(BIN_EDGES[:-1], BIN_EDGES[1:])):
        mask = forecasted & p.ge(lo) & (p.le(hi) if index == len(BIN_EDGES)-2 else p.lt(hi))
        sample, observed = int(mask.sum()), int((mask & known).sum())
        successes = int((mask & net.gt(0)).sum())
        bins.append(dict(lower=lo, upper=hi, forecasts=sample, resolved=observed, unknown=sample-observed,
                         mean_probability=float(p[mask].mean()) if sample else None,
                         observed_frequency=successes / observed if observed else None,
                         frequency_bounds=_bounds((successes / sample, (successes + sample-observed) / sample)) if sample else None))
    available_pair = forecasted & base.notna()
    daily = pd.DataFrame(dict(date=ledger.date, paired_forecasts=available_pair, paired_scored=paired,
                              mean_brier_advantage=(base_errors-errors).where(paired))).groupby('date', sort=True).agg(
                                  paired_forecasts=('paired_forecasts', 'sum'), paired_scored=('paired_scored', 'sum'),
                                  mean_brier_advantage=('mean_brier_advantage', 'mean')).reset_index()
    daily['mean_brier_advantage'] = daily['mean_brier_advantage'].where(
        daily.paired_forecasts.gt(0) & daily.paired_forecasts.eq(daily.paired_scored))
    daily_rows = daily.astype(object).where(daily.notna(), None).to_dict('records')
    gains, losses = net[net > 0], -net[net < 0]
    entry_filled = int(ledger['entry_state'].eq('filled').sum())
    entry_known = int(ledger['entry_state'].isin(['filled', 'blocked']).sum())
    completed = int(ledger['state'].eq('complete').sum())
    return dict(opportunities=total, resolved=n, unresolved=total-n, wins=wins,
                blocked_entries=int(ledger['state'].eq('entry_blocked').sum()),
                proxy_filled_entries=entry_filled, unknown_entries=total-entry_known,
                proxy_entry_fill_rate=entry_filled / entry_known if entry_known else None,
                completed_proxy_trades=completed, profit_frequency_completed_trades=wins / completed if completed else None,
                losses=int(net.lt(0).sum()), zero_returns=int(net.eq(0).sum()),
                forecasts=int(forecasted.sum()), no_forecast=int((~forecasted).sum()),
                mean_forecast_probability=float(p[forecasted].mean()) if forecasted.any() else None,
                scored_forecasts=int(scored.sum()), unknown_forecast_outcomes=int(unscored.sum()),
                empirical_success_frequency_resolved=wins / n if n else None,
                empirical_success_fraction_bounds=_bounds((wins / total, (wins + total - n) / total)) if total else None,
                mean_known_net_return=float(net[known].mean()) if n else None,
                all_opportunity_mean_net_return=float(net.mean()) if n and n == total else None,
                average_gain=float(gains.mean()) if len(gains) else None,
                average_loss=float(losses.mean()) if len(losses) else None,
                brier_resolved=brier, paired_brier=pair_brier, paired_base_brier=base_brier,
                paired_scored_forecasts=int(paired.sum()),
                brier_skill=1 - pair_brier / base_brier if base_brier is not None and base_brier > 0 else None,
                brier_all_forecast_bounds=_bounds((lower / forecasted.sum(), upper / forecasted.sum())) if forecasted.any() else None,
                calibration=bins, daily_brier_advantage=daily_rows,
                unknown_bounds_are_confidence_intervals=False,
                probability_of_real_trade_profit=None, accepted_for_signals=False)
