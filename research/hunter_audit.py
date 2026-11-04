"""Descriptive coverage of frozen picks; never a selector or a profit probability."""

from numbers import Integral

from config import settings
from core.timeutils import is_canonical_iso_date
from research.expectancy import _keys, forecast_errors
from research.probability import _numeric

VERSION = 'hunter-coverage-v1'
SCOPE = 'saved expectancy opportunity panel after its existing liquidity and nontrading filters'
GAIN_THRESHOLDS = (.03, .10)  # Fixed descriptive targets, never used for selection.


def checked_opportunities(forecast, outcomes, picks, model, *, holdout_start):
    """Validate full denominator; archive runner separately verifies provenance."""
    if (not is_canonical_iso_date(holdout_start)
            or holdout_start > settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('مرز ممیزی باید در دوره توسعه باشد')
    for frame in (forecast, outcomes, picks):
        _keys(frame)
        if frame.date.ge(holdout_start).any():
            raise ValueError('دوره مهروموم‌شده در ممیزی مجاز نیست')
    if not {'state', 'resolved_at'} <= set(outcomes):
        raise ValueError('وضعیت و زمان نتیجه لازم است')
    known_dates = outcomes.loc[outcomes.state.ne('unknown'), 'resolved_at']
    if (any(not is_canonical_iso_date(day) for day in known_dates.unique())
            or known_dates.ge(holdout_start).any()):
        raise ValueError('نتیجه پس از مرز توسعه معلوم شده است')
    forecast_errors(forecast, outcomes, model)
    if (set(picks) != {'date', 'symbol', 'rank'}
            or not picks['rank'].map(lambda v: isinstance(v, Integral) and not isinstance(v, bool) and v >= 0).all()
            or picks.duplicated(['date', 'rank']).any()
            or any(sorted(group['rank']) != list(range(len(group))) for _, group in picks.groupby('date'))):
        raise ValueError('رتبه نامزد باید صحیح، یکتا و پیوسته از صفر باشد')
    chosen = picks.merge(forecast, on=['date', 'symbol'], how='left', validate='one_to_one', indicator=True)
    if (chosen._merge.ne('both').any() or chosen.status.ne('forecasted').any()
            or chosen.normal_expected_net.le(0).any() or chosen.stress_expected_net.le(0).any()):
        raise ValueError('نامزد خارج از پنل یا دروازه مثبت روش conditional-2d است')
    if 'corporate_actions' not in outcomes:
        raise ValueError('شمار صریح عبور از اقدام شرکتی لازم است')
    actions = _numeric(outcomes.corporate_actions)
    if (actions.notna().ne(outcomes.state.ne('unknown')).any()
            or actions.lt(0).any() or actions.dropna().mod(1).ne(0).any()
            or (outcomes.state.eq('entry_blocked') & actions.ne(0)).any()):
        raise ValueError('شمار عبور از اقدام شرکتی با نتیجه ناسازگار است')
    rows = forecast.merge(outcomes, on=['date', 'symbol'], validate='one_to_one')
    rows = rows.merge(picks.assign(selected=True)[['date', 'symbol', 'selected']],
                      on=['date', 'symbol'], how='left', validate='one_to_one')
    # Absence from the exhaustive, verified picks list is observed non-selection.
    rows['selected'] = rows.selected.eq(True)
    return rows


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _capture(rows, scenario, threshold):
    selected, known = rows.selected, rows.state.ne('unknown')
    positive = rows[scenario+'_net'].gt(threshold)
    total, hits = int(positive.sum()), int((positive & selected).sum())
    inside, outside = int((selected & ~known).sum()), int((~selected & ~known).sum())
    lower, upper = _ratio(hits, total+outside), _ratio(hits+inside, total+inside)
    if total == 0:
        if inside and not outside:
            lower = upper = 1.
        elif outside and not inside:
            lower = upper = 0.
    missed = rows.loc[positive & ~selected, 'status'].replace({'forecasted': 'forecasted_but_not_selected'})
    return dict(net_return_strictly_above=threshold,
        known_positive=total, selected_known_positive=hits, missed_known_positive=total-hits,
        precision_known_only=_ratio(hits, int((selected & known).sum())),
        precision_bounds=[_ratio(hits, int(selected.sum())), _ratio(hits+inside, int(selected.sum()))],
        recall_known_only=_ratio(hits, total), recall_bounds=[lower, upper],
        missed_positive_by_forecast_status={str(k): int(v) for k, v in missed.value_counts().sort_index().items()})


def summarize_opportunities(rows):
    """Row-weighted metrics on checked rows, not shared-capital returns.

    Recall bounds range over all assignments to unknown outcomes, conditional
    on a positive existing. These are identification bounds, not confidence
    intervals. Blocked entries count as non-positive selections.
    """
    selected, known = rows.selected, rows.state.ne('unknown')
    output = dict(opportunities=len(rows), selected=int(selected.sum()),
        forecasted=int(rows.status.eq('forecasted').sum()), known_outcomes=int(known.sum()),
        unknown_outcomes=int((~known).sum()), entry_blocked=int(rows.state.eq('entry_blocked').sum()),
        selection_rate=_ratio(int(selected.sum()), len(rows)),
        forecast_coverage=_ratio(int(rows.status.eq('forecasted').sum()), len(rows)),
        selected_unknown_outcomes=int((selected & ~known).sum()),
        unselected_unknown_outcomes=int((~selected & ~known).sum()),
        corporate_action_opportunities=int(rows.corporate_actions.gt(0).sum()),
        known_corporate_action_crossings=int(rows.corporate_actions.dropna().sum()),
        selected_corporate_action_opportunities=int((selected & rows.corporate_actions.gt(0)).sum()),
        selected_known_corporate_action_crossings=int(rows.loc[selected, 'corporate_actions'].dropna().sum()),
        unknown_corporate_action_counts=int(rows.corporate_actions.isna().sum()), scenarios={})
    for scenario in ('normal', 'stress'):
        output['scenarios'][scenario] = dict(_capture(rows, scenario, 0.),
            gain_thresholds=[_capture(rows, scenario, threshold) for threshold in GAIN_THRESHOLDS])
    return output


def describe_checked_opportunities(rows):
    """Format checked rows without repeating numeric validation."""
    return dict(version=VERSION, accepted_for_signals=False, holdout_eligible=False, scope=SCOPE,
        target='positive source-label net return at horizon2; not economic total return or a price peak',
        bounds='identification bounds over unknown outcomes, conditional on a positive existing; not confidence intervals',
        weighting='one stock-decision row; selections are not portfolio fills',
        total=summarize_opportunities(rows),
        daily=[dict(date=day, **summarize_opportunities(group)) for day, group in rows.groupby('date', sort=True)])


def opportunity_audit(forecast, outcomes, picks, model, *, holdout_start):
    rows = checked_opportunities(forecast, outcomes, picks, model, holdout_start=holdout_start)
    return describe_checked_opportunities(rows)
