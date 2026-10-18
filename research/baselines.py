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



def _cohort_returns(picks: pd.DataFrame, calendar: pd.Index, horizon: int, k: int) -> pd.Series:
    col = f"net_ret_{horizon}d"
    executed = picks[col].notna() & np.isfinite(picks[col])
    status = f"label_status_{horizon}d"
    if status in picks:
        executed &= picks[status].eq('complete')
        known = executed | (picks[status].eq('entry_blocked') & picks[col].isna())
    else:
        known = executed | picks[col].isna()
    allocated = picks[col].where(executed, 0.0).where(known)
    grouped = allocated.groupby(picks["date"])
    # روز بی‌انتخاب = نقد؛ خروج نامعلوم = NaN. کاهش گروهی برای بازار کامل سریع‌تر از apply است.
    cohort = (grouped.sum() / k).where(grouped.count() == grouped.size())
    return cohort.reindex(calendar, fill_value=0.0)


def entry_counts(picks: pd.DataFrame, horizon: int) -> dict:
    """پرشدن ورود از بسته‌شدن معامله جداست؛ ورود با داده خراب نامعلوم می‌ماند."""
    col, status = f'net_ret_{horizon}d', f'label_status_{horizon}d'
    if {'entry_date', 'entry_executable', status} <= set(picks):
        filled = picks['entry_date'].ne('') & picks['entry_executable'].eq(True)
        known = filled | (picks['entry_date'].ne('') & picks[status].eq('entry_blocked'))
    elif status in picks:
        # در آرشیو بدون ستون ورود، خروج کامل شاهد ورود است؛ خروج سانسورشده شاهد عدم ورود نیست.
        filled = picks[status].eq('complete') & picks[col].notna() & np.isfinite(picks[col])
        known = filled | (picks[status].eq('entry_blocked') & picks[col].isna())
    else:
        # سازگاری با پنل‌های قدیمی آزمون؛ قرارداد جدید وضعیت ورود صریح دارد.
        filled = picks[col].notna() & np.isfinite(picks[col])
        known = pd.Series(True, index=picks.index)
    return dict(known_entries=int(known.sum()), filled_entries=int(filled.sum()),
                unknown_entries=int((~known).sum()),
                fill_rate=float(filled.sum() / known.sum()) if known.any() else float('nan'))


def evaluate_picks(picks: pd.DataFrame, market: pd.DataFrame, horizon: int, k: int) -> Dict[str, float]:
    """ارزیابی انتخاب‌های یک استراتژی در یک افق. market: میانگین روزانه «خرید همه سهم‌های نقدشونده»."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("تعداد انتخاب باید عدد صحیح مثبت باشد")
    col = f"net_ret_{horizon}d"
    executed = picks[col].notna() & np.isfinite(picks[col])
    status_col = f"label_status_{horizon}d"
    # سازگاری با پنل‌های آزمون قدیمی؛ خروجی labels-v2 همواره وضعیت صریح دارد.
    if status_col in picks:
        executed &= picks[status_col].eq('complete')
        known = executed | (picks[status_col].eq('entry_blocked') & picks[col].isna())
    else:
        known = executed | picks[col].isna()
    trades = picks.loc[executed, col]
    calendar = market.index.union(pd.Index(picks["date"].unique())).sort_values()
    days = len(calendar)
    cohort = _cohort_returns(picks, calendar, horizon, k)
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
        "days_without_selection": int(days - picks["date"].nunique()),
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
    status = f"label_status_{horizon}d"
    values = panel[col].copy()
    if status in panel:
        blocked = panel[status].eq('entry_blocked') & panel[col].isna()
        values = values.where(panel[status].eq('complete') & np.isfinite(values))
        values.loc[blocked] = 0.0
    grouped = values.groupby(panel["date"])
    return grouped.mean().where(grouped.count() == grouped.size())


def random_benchmark(panel: pd.DataFrame, horizon: int, k: int, trials: int, seed: int = 7) -> Dict[str, float]:
    """توزیع «میانگین هر معامله» برای K انتخاب تصادفی روزانه (همان جامعه و همان قواعد اجرا)."""
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise ValueError("تعداد آزمون تصادفی باید عدد صحیح مثبت باشد")
    rng = np.random.default_rng(seed)
    market = market_benchmark(panel, horizon)
    means = []
    for _ in range(trials):
        picks = pick_top(panel, pd.Series(rng.random(len(panel)), index=panel.index), k)
        means.append(_cohort_returns(picks, market.index, horizon, k).mean())
    means = np.array(means)
    finite = means[np.isfinite(means)]
    return {"mean": float(finite.mean()) if len(finite) else float("nan"),
            "p05": float(np.percentile(finite, 5)) if len(finite) else float("nan"),
            "p95": float(np.percentile(finite, 95)) if len(finite) else float("nan"),
            "trials": int(trials), "valid_trials": int(len(finite)), "basis":"mean_cohort"}


def run_baselines(panel: pd.DataFrame, horizons: List[int], k: int, random_trials: int,
                  strategies: Optional[List[Strategy]] = None) -> Dict[str, Dict]:
    """
    panel: فیچرها + برچسب‌ها (date، symbol، value، net_ret_hd، ...) فقط برای ردیف‌های نقدشونده دوره موردنظر.
    خروجی: {افق: {"market": ..., "random": ..., "strategies": {نام: آمار}}}
    """
    strategies = strategies or STRATEGIES
    results: Dict[str, Dict] = {}
    for h in horizons:
        market = market_benchmark(panel, h)
        all_trades = panel[f"net_ret_{h}d"].where(np.isfinite(panel[f"net_ret_{h}d"]))
        if f'label_status_{h}d' in panel:
            all_trades = all_trades.where(panel[f'label_status_{h}d'].eq('complete'))
        all_trades = all_trades.dropna()
        entry = {
            "market": {"mean_trade": float(all_trades.mean()), "mean_cohort": float(market.mean()), "win_rate": float((all_trades > 0).mean()),
                       **entry_counts(panel, h),
                       "t_cohort": tolerant_t(market, lag=h), "days": int(market.notna().sum())},
            "random": random_benchmark(panel, h, k, random_trials),
            "strategies": {},
        }
        for strategy in strategies:
            picks = pick_top(panel, strategy.score(panel), k)
            stats = evaluate_picks(picks, market, h, k)
            stats["label_fa"] = strategy.label_fa
            entry["strategies"][strategy.name] = stats
        results[str(h)] = entry
    return results
