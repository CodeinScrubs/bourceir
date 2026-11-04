# -*- coding: utf-8 -*-
"""
استراتژی‌های پایه و ارزیابی علمی آن‌ها (فاز ۱-ج)

پرسش: آیا قاعده‌های ساده و رایج (مومنتوم، برگشت، ورود پول حقیقی، صف خرید، ...) بعد از کارمزد و لغزش و با اجرای
واقع‌بینانه سود خالص می‌دهند؟ و آیا بهتر از «انتخاب تصادفی» هستند؟ هر مدل یادگیری ماشین بعدی باید از بهترین
همین‌ها بهتر باشد، وگرنه ارزشی ندارد.

قواعد ارزیابی:
- انتخاب در پایان روز t فقط با فیچرهای روز t (انبار فیچر) و ارزش معاملات روز t (نقدشوندگی).
- K نماد با بیشترین امتیاز (تساوی: ارزش معاملات بیشتر). اگر ورود ممکن نبود (صف خرید/توقف)، آن سهم سرمایه
  بیکار می‌ماند (بازده صفر) — «انتخاب خوب ولی غیرقابل خرید» امتیاز نمی‌گیرد.
- معیار مقایسه هر روز: میانگین همه سهم‌های نقدشونده قابل خرید («خرید همه بازار») و توزیع انتخاب تصادفی.
- معنی‌داری: آماره t با خطای استاندارد نیوی-وست (هم‌پوشانی دوره‌های نگهداری چندروزه).
- دوره نگهداری بیرون از نمونه (holdout) مهر و موم است و فقط با دستور صریح باز می‌شود.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np
import pandas as pd

from core.timeutils import is_canonical_iso_date

ScoreFn = Callable[[pd.DataFrame], pd.Series]


@dataclass(frozen=True)
class Strategy:
    name: str
    label_fa: str
    score: ScoreFn


STRATEGIES: List[Strategy] = [
    Strategy("momentum_5d", "مومنتوم ۵ روزه (بیشترین رشد هفته اخیر)", lambda f: f["ret_5d"]),
    Strategy("momentum_20d", "مومنتوم ۲۰ روزه (بیشترین رشد ماه اخیر)", lambda f: f["ret_20d"]),
    Strategy("reversal_5d", "برگشت کوتاه‌مدت (بیشترین افت هفته اخیر)", lambda f: -f["ret_5d"]),
    Strategy("real_inflow_5d", "ورود پول حقیقی ۵ روزه", lambda f: f["real_flow_ratio_5d"]),
    Strategy("buyer_power", "قدرت خریدار حقیقی امروز", lambda f: f["buyer_power"]),
    Strategy("volume_breakout", "جهش ارزش معاملات با روز مثبت", lambda f: f["value_ratio_20d"].where(f["ret_1d"] > 0)),
    Strategy("near_20d_high", "نزدیک سقف ۲۰ روزه (شکست مقاومت)", lambda f: f["dist_high_20d"]),
    Strategy("buy_queue", "صف خرید قفل امروز (خرید فردا)",
             lambda f: f["lock_up_streak"].where(f["locked_up"] == 1)),
]

SCORE_COVERAGE_VERSION = 'baseline-score-coverage-v1'


def _numeric_score_input(series):
    """Return finite numeric values; booleans and nonfinite cells are unknown."""
    if pd.api.types.is_bool_dtype(series.dtype):
        return pd.Series(np.nan, index=series.index, dtype=float)
    if not pd.api.types.is_numeric_dtype(series.dtype):
        series = series.mask(series.map(lambda v: isinstance(v, (bool, np.bool_, str, bytes))))
    values = pd.to_numeric(series, errors='coerce').to_numpy(dtype=float, na_value=np.nan, copy=True)
    values[~np.isfinite(values)] = np.nan
    return pd.Series(values, index=series.index)


def decision_score_inputs(panel: pd.DataFrame) -> pd.DataFrame:
    """Keep subsequent outcomes out of baseline scoring even with custom rules."""
    forbidden = ('net_ret_', 'label_status_', 'exit_date_', 'planned_exit_',
                 'corporate_actions_', 'mfe_', 'mae_', 'exit_delay_')
    columns = [c for c in panel if isinstance(c, str) and
               (c.startswith(forbidden) or c in ('entry_date', 'entry_executable', 'resolved_at'))]
    return panel.drop(columns=columns)


def _score_states(panel, strategy, score=None):
    score = strategy.score(panel) if score is None else score
    if not isinstance(score, pd.Series) or not score.index.equals(panel.index):
        raise ValueError('امتیاز باید با ترتیب ورودی یکسان باشد')
    numeric = _numeric_score_input(score)
    known_score = numeric.notna()
    known_nonmatch = pd.Series(False, index=panel.index)
    if strategy.name == 'volume_breakout':
        ret, value_ratio = (_numeric_score_input(panel[name]) for name in ('ret_1d', 'value_ratio_20d'))
        known_nonmatch = ret.notna() & ret.le(0)
        known_score = ret.notna() & ret.gt(0) & value_ratio.notna() & numeric.notna()
    elif strategy.name == 'buy_queue':
        locked, streak = (_numeric_score_input(panel[name]) for name in ('locked_up', 'lock_up_streak'))
        known_nonmatch = locked.eq(0)
        known_score = locked.eq(1) & streak.notna() & numeric.notna()
    unknown = ~(known_score | known_nonmatch)
    return numeric, known_score, known_nonmatch, unknown


def eligible_strategy_score(panel: pd.DataFrame, strategy: Strategy, score: Optional[pd.Series] = None) -> pd.Series:
    """Only rank finite scores with verified inputs for the rule's condition."""
    numeric, known_score, _, _ = _score_states(panel, strategy, score)
    return numeric.where(known_score)


def score_coverage(panel: pd.DataFrame, strategy: Strategy, score: Optional[pd.Series] = None) -> dict:
    """Describe unscorable observed rows without asserting a complete market ranking.

    For conditional rules, a known failed condition is different from missing
    inputs. This is coverage of the supplied panel, not historical membership.
    """
    if 'date' not in panel:
        raise ValueError('تاریخ برای پوشش امتیاز لازم است')
    numeric, known_score, known_nonmatch, unknown = _score_states(panel, strategy, score)
    by_day = pd.DataFrame(dict(date=panel.date, known_score=known_score.astype(int),
                               unknown=unknown.astype(int))).groupby('date').sum()
    return dict(version=SCORE_COVERAGE_VERSION, observed_rows=len(panel),
        known_score_rows=int(known_score.sum()), known_nonmatching_rows=int(known_nonmatch.sum()),
        unknown_score_rows=int(unknown.sum()),
        observed_days=int(len(by_day)), days_with_unknown_score=int(by_day.unknown.gt(0).sum()),
        days_with_known_score_and_unknown=int((by_day.known_score.gt(0) & by_day.unknown.gt(0)).sum()),
        finite_score_with_unknown_inputs=int((numeric.notna() & unknown).sum()),
        ranking_scope='finite scores in observed eligible rows; not a verified complete historical universe')


def newey_west_t(series: pd.Series, lag: int) -> float:
    """آماره t میانگین با خطای استاندارد نیوی-وست (برای بازده‌های هم‌پوشان)."""
    if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
        raise ValueError("وقفه آماری باید عدد صحیح نامنفی باشد")
    # حذف فاصله‌های مجهول تقویم، هم‌بستگی زمانی کاذب می‌سازد؛ آماره در این حالت اعلام نمی‌شود.
    if series.isna().any() or not np.isfinite(series.to_numpy(dtype=float)).all():
        return float("nan")
    x = series.to_numpy(dtype=float)
    n = len(x)
    if n < max(10, lag + 2):
        return float("nan")
    d = x - x.mean()
    var = d @ d / n
    for k in range(1, lag + 1):
        w = 1.0 - k / (lag + 1.0)
        var += 2.0 * w * (d[k:] @ d[:-k]) / n
    if var <= 0:
        return float("nan")
    return float(x.mean() / np.sqrt(var / n))


def tolerant_t(series: pd.Series, lag: int) -> float:
    """
    سازگاری نام قدیمی: روز نامعلوم حذف یا صفر نمی‌شود و فاصله تقویمی فشرده نمی‌شود.
    بدون مدل استنباط معتبر برای داده گمشده، آماره مجهول می‌ماند؛ آمار توصیفی جدا گزارش می‌شود.
    """
    return newey_west_t(series, lag)


def pick_top(panel: pd.DataFrame, score: pd.Series, k: int) -> pd.DataFrame:
    """K سهم برتر هر روز بر اساس امتیاز (فقط ردیف‌های دارای امتیاز)."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("تعداد انتخاب باید عدد صحیح مثبت باشد")
    df = panel.assign(_score=score).dropna(subset=["_score"])
    df = df.loc[np.isfinite(df["_score"])].copy()
    columns = ["date", "_score", "value"] + (["symbol"] if "symbol" in df else [])
    df = df.sort_values(columns, ascending=[True, False, False] + ([True] if "symbol" in df else []))
    return df[df.groupby("date").cumcount() < k]



def _entry_masks(picks: pd.DataFrame, horizon: int):
    """Return consistent known filled and blocked entries, not actual fills."""
    col, status = f'net_ret_{horizon}d', f'label_status_{horizon}d'
    absent = pd.Series(False, index=picks.index)
    if {'entry_date', 'entry_executable', status} <= set(picks):
        valid_dates = [day for day in picks['entry_date'].dropna().unique() if is_canonical_iso_date(day)]
        valid_date = picks['entry_date'].isin(valid_dates)
        if 'date' in picks:
            valid_decisions = [day for day in picks['date'].dropna().unique() if is_canonical_iso_date(day)]
            chronological = picks['entry_date'].astype('string').gt(picks['date'].astype('string')).fillna(False)
            valid_date &= picks['date'].isin(valid_decisions) & chronological
        flag = picks['entry_executable']
        flag_true = flag.map(lambda v: isinstance(v, (bool, np.bool_)) and bool(v))
        flag_false = flag.map(lambda v: isinstance(v, (bool, np.bool_)) and not bool(v))
        finite_return = picks[col].notna() & np.isfinite(picks[col])
        filled_status = ((picks[status].eq('complete') & finite_return) |
                         (picks[status].isin(['censored', 'invalid_data']) & picks[col].isna()))
        filled = valid_date & flag_true & filled_status
        blocked = valid_date & flag_false & picks[status].eq('entry_blocked') & picks[col].isna()
    elif status in picks and not ({'entry_date', 'entry_executable'} & set(picks)):
        # در آرشیو بدون ستون ورود، خروج کامل شاهد ورود است؛ خروج سانسورشده شاهد عدم ورود نیست.
        filled = picks[status].eq('complete') & picks[col].notna() & np.isfinite(picks[col])
        blocked = picks[status].eq('entry_blocked') & picks[col].isna()
    else:
        # Missing status or only part of the entry evidence is not a known entry.
        filled = blocked = absent
    exit_col = f'exit_date_{horizon}d'
    if exit_col in picks:
        blocked &= picks[exit_col].isna() | picks[exit_col].eq('')
    return filled.fillna(False), blocked.fillna(False)


def _outcome_masks(picks: pd.DataFrame, horizon: int):
    """Use the same entry evidence for returns and entry diagnostics."""
    filled, blocked = _entry_masks(picks, horizon)
    status = f'label_status_{horizon}d'
    if status not in picks:
        return filled, blocked
    complete = filled & picks[status].eq('complete')
    exit_col = f'exit_date_{horizon}d'
    if exit_col in picks:
        valid_dates = [day for day in picks[exit_col].dropna().unique() if is_canonical_iso_date(day)]
        complete &= picks[exit_col].isin(valid_dates)
        preceding = 'entry_date' if 'entry_date' in picks else 'date' if 'date' in picks else None
        if preceding:
            valid_previous = [day for day in picks[preceding].dropna().unique() if is_canonical_iso_date(day)]
            chronological = picks[exit_col].astype('string').gt(picks[preceding].astype('string')).fillna(False)
            complete &= picks[preceding].isin(valid_previous) & chronological
    return complete.fillna(False), blocked


def _cohort_returns(picks: pd.DataFrame, calendar: pd.Index, horizon: int, k: int, *, missing_panel_days=None) -> pd.Series:
    col = f"net_ret_{horizon}d"
    executed, blocked = _outcome_masks(picks, horizon)
    allocated = picks[col].where(executed, 0.0).where(executed | blocked)
    grouped = allocated.groupby(picks["date"])
    # روز بی‌انتخاب = نقد؛ خروج نامعلوم = NaN. کاهش گروهی برای بازار کامل سریع‌تر از apply است.
    cohort = (grouped.sum() / k).where(grouped.count() == grouped.size())
    cohort = cohort.reindex(calendar, fill_value=0.0)
    if missing_panel_days is not None:
        cohort.loc[cohort.index.isin(missing_panel_days)] = np.nan
    return cohort


def entry_counts(picks: pd.DataFrame, horizon: int) -> dict:
    """پرشدن ورود از بسته‌شدن معامله جداست؛ ورود با داده خراب نامعلوم می‌ماند."""
    filled, blocked = _entry_masks(picks, horizon)
    known = filled | blocked
    return dict(known_entries=int(known.sum()), filled_entries=int(filled.sum()),
                unknown_entries=int((~known).sum()),
                fill_rate=float(filled.sum() / known.sum()) if known.any() else float('nan'))


def evaluate_picks(picks: pd.DataFrame, market: pd.DataFrame, horizon: int, k: int, *, missing_panel_days=None) -> Dict[str, float]:
    """ارزیابی انتخاب‌های یک استراتژی در یک افق. market: میانگین روزانه «خرید همه سهم‌های نقدشونده»."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("تعداد انتخاب باید عدد صحیح مثبت باشد")
    col = f"net_ret_{horizon}d"
    executed, blocked = _outcome_masks(picks, horizon)
    known = executed | blocked
    trades = picks.loc[executed, col]
    calendar = market.index.union(pd.Index(picks["date"].unique())).sort_values()
    days = len(calendar)
    missing = pd.Index([] if missing_panel_days is None else missing_panel_days)
    if not missing.is_unique or not missing.isin(calendar).all() or missing.isin(picks['date']).any():
        raise ValueError('روزهای فاقد پنل با تقویم یا انتخاب‌ها ناسازگارند')
    cohort = _cohort_returns(picks, calendar, horizon, k, missing_panel_days=missing)
    excess = cohort - market.reindex(calendar)
    gains, losses = trades[trades > 0].sum(), -trades[trades < 0].sum()
    by_year = trades.groupby(picks.loc[executed, "date"].str[:4]).mean()
    entries = entry_counts(picks, horizon)
    stats = {
        "days": int(days),
        "picks": int(len(picks)),
        "trades": int(executed.sum()),
        **entries,
        "unknown_picks": int((~known).sum()),
        "unknown_cohorts": int(cohort.isna().sum()),
        "days_without_selection": int(days - len(missing) - picks["date"].nunique()),
        "missing_panel_days": int(len(missing)),
        "corporate_action_trades": int((picks.get(f"corporate_actions_{horizon}d", pd.Series(dtype=float)) > 0).sum()),
        "mean_trade": float(trades.mean()) if len(trades) else float("nan"),
        "mean_trade_worst": float(picks.loc[executed, f"{col}_worst"].mean())
        if f"{col}_worst" in picks and len(trades) else float("nan"),
        "median_trade": float(trades.median()) if len(trades) else float("nan"),
        "win_rate": float((trades > 0).mean()) if len(trades) else float("nan"),
        "profit_factor": float(gains / losses) if losses > 0 else float("nan"),
        "mean_excess_vs_market": float(excess.mean()) if len(excess) else float("nan"),
        "t_excess": tolerant_t(excess, lag=horizon),
        "mean_cohort": float(cohort.mean()) if len(cohort) else float("nan"),
        "t_cohort": tolerant_t(cohort, lag=horizon),
        "years_positive": int((by_year > 0).sum()),
        "years_total": int(len(by_year)),
        "by_year": {y: round(float(v), 5) for y, v in by_year.items()},
    }
    # بدون پرتفوی چندنمادی، موجودی نقد و mark-to-market، این سه شاخص قابل استناد نیستند.
    stats.update(cagr=float("nan"), max_drawdown=float("nan"), sharpe=float("nan"), portfolio_simulated=False)
    return stats


def market_benchmark(panel: pd.DataFrame, horizon: int) -> pd.Series:
    """میانگین بازده خالص همه سهم‌های نقدشونده قابل خرید در هر روز («خرید همه بازار»)."""
    col = f"net_ret_{horizon}d"
    values = panel[col].copy()
    executed, blocked = _outcome_masks(panel, horizon)
    values = values.where(executed)
    values.loc[blocked] = 0.0
    grouped = values.groupby(panel["date"])
    return grouped.mean().where(grouped.count() == grouped.size())


def _baseline_calendar(panel: pd.DataFrame, calendar):
    observed = list(panel['date'].unique())
    if any(not is_canonical_iso_date(day) for day in observed):
        raise ValueError('تاریخ پنل با تقویم سازگار نیست')
    if calendar is None:
        days = pd.Index(sorted(observed))
        basis = 'observed_panel_only'
    else:
        supplied = list(calendar)
        if (not supplied or any(not is_canonical_iso_date(day) for day in supplied)
                or supplied != sorted(set(supplied)) or not set(observed).issubset(supplied)):
            raise ValueError('تقویم باید معتبر، یکتا، مرتب و شامل تاریخ‌های پنل باشد')
        days = pd.Index(supplied)
        basis = 'provided_calendar_not_verified_here'
    return days, days[~days.isin(observed)], basis


def random_benchmark(panel: pd.DataFrame, horizon: int, k: int, trials: int, seed: int = 7, *,
                     calendar=None) -> Dict[str, float]:
    """توزیع «میانگین هر معامله» برای K انتخاب تصادفی روزانه (همان جامعه و همان قواعد اجرا)."""
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise ValueError("تعداد آزمون تصادفی باید عدد صحیح مثبت باشد")
    rng = np.random.default_rng(seed)
    days, missing, _ = _baseline_calendar(panel, calendar)
    market = market_benchmark(panel, horizon).reindex(days)
    means, unknown_days = [], []
    for _ in range(trials):
        picks = pick_top(panel, pd.Series(rng.random(len(panel)), index=panel.index), k)
        cohort = _cohort_returns(picks, market.index, horizon, k, missing_panel_days=missing)
        means.append(cohort.mean())
        unknown_days.append(int(cohort.isna().sum()))
    means = np.array(means)
    finite = means[np.isfinite(means)]
    return {"mean": float(finite.mean()) if len(finite) else float("nan"),
            "p05": float(np.percentile(finite, 5)) if len(finite) else float("nan"),
            "p95": float(np.percentile(finite, 95)) if len(finite) else float("nan"),
            "trials": int(trials), "valid_trials": int(len(finite)), "basis":"mean_cohort",
            "minimum_unknown_days": min(unknown_days), "maximum_unknown_days": max(unknown_days),
            "conditional_on_known_days": any(unknown_days)}


def run_baselines(panel: pd.DataFrame, horizons: List[int], k: int, random_trials: int,
                  strategies: Optional[List[Strategy]] = None, *, calendar=None) -> Dict[str, Dict]:
    """
    panel: فیچرها + برچسب‌ها (date، symbol، value، net_ret_hd، ...) فقط برای ردیف‌های نقدشونده دوره موردنظر.
    خروجی: {افق: {"market": ..., "random": ..., "strategies": {نام: آمار}}}
    """
    strategies = strategies or STRATEGIES
    days, missing, calendar_basis = _baseline_calendar(panel, calendar)
    results: Dict[str, Dict] = {}
    for h in horizons:
        market = market_benchmark(panel, h).reindex(days)
        executed, _ = _outcome_masks(panel, h)
        all_trades = panel.loc[executed, f'net_ret_{h}d']
        entry = {
            "market": {"mean_trade": float(all_trades.mean()), "mean_cohort": float(market.mean()), "win_rate": float((all_trades > 0).mean()),
                       **entry_counts(panel, h),
                       "t_cohort": tolerant_t(market, lag=h), "days": int(market.notna().sum()),
                       "calendar_days": len(days), "unknown_days": int(market.isna().sum()),
                       "no_panel_days": len(missing), "calendar_basis": calendar_basis,
                       "conditional_on_known_days": bool(market.isna().any())},
            "random": random_benchmark(panel, h, k, random_trials, calendar=days if len(days) else None),
            "strategies": {},
        }
        for strategy in strategies:
            decision_inputs = decision_score_inputs(panel)
            score = strategy.score(decision_inputs)
            picks = pick_top(panel, eligible_strategy_score(decision_inputs, strategy, score), k)
            stats = evaluate_picks(picks, market, h, k, missing_panel_days=missing)
            stats["label_fa"] = strategy.label_fa
            stats['score_coverage'] = score_coverage(panel, strategy, score)
            entry["strategies"][strategy.name] = stats
        results[str(h)] = entry
    return results
