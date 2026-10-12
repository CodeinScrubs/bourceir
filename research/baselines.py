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
    x = series.dropna().to_numpy(dtype=float)
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


def pick_top(panel: pd.DataFrame, score: pd.Series, k: int) -> pd.DataFrame:
    """K سهم برتر هر روز بر اساس امتیاز (فقط ردیف‌های دارای امتیاز)."""
    df = panel.assign(_score=score).dropna(subset=["_score"])
    df = df.sort_values(["date", "_score", "value"], ascending=[True, False, False])
    return df[df.groupby("date").cumcount() < k]


def _portfolio_stats(cohort: pd.Series, horizon: int) -> Dict[str, float]:
    """
    پرتفوی واقعی بدون هم‌پوشانی: هر h روز یک سبد K سهمی؛ برای حذف شانس روز شروع، همه h حالت شروع محاسبه و
    میانه (CAGR) و بدترین افت سرمایه گزارش می‌شود.
    """
    periods_per_year = 240.0 / horizon
    cagr, mdd, sharpe = [], [], []
    for offset in range(horizon):
        r = cohort.iloc[offset::horizon].fillna(0.0)
        if len(r) < 10:
            continue
        equity = (1.0 + r).cumprod()
        years = len(r) / periods_per_year
        cagr.append(equity.iloc[-1] ** (1.0 / years) - 1.0 if equity.iloc[-1] > 0 else -1.0)
        peak = np.maximum.accumulate(np.concatenate([[1.0], equity.to_numpy()]))
        mdd.append(float(np.max(1.0 - np.concatenate([[1.0], equity.to_numpy()]) / peak)))
        sharpe.append(float(r.mean() / r.std() * np.sqrt(periods_per_year)) if r.std() > 0 else float("nan"))
    if not cagr:
        return {"cagr": float("nan"), "max_drawdown": float("nan"), "sharpe": float("nan")}
    return {"cagr": float(np.median(cagr)), "max_drawdown": float(np.max(mdd)), "sharpe": float(np.nanmedian(sharpe))}


def evaluate_picks(picks: pd.DataFrame, market: pd.DataFrame, horizon: int, k: int) -> Dict[str, float]:
    """ارزیابی انتخاب‌های یک استراتژی در یک افق. market: میانگین روزانه «خرید همه سهم‌های نقدشونده»."""
    col = f"net_ret_{horizon}d"
    executed = picks[col].notna()
    trades = picks.loc[executed, col]
    days = picks["date"].nunique()
    # بازده سبد هر روز: جمع بازده سهم‌های خریداری‌شده ÷ K (سهم خریدنشده = سرمایه بیکار)
    cohort = picks.groupby("date")[col].apply(lambda r: r.fillna(0.0).sum() / k)
    per_day_mean = picks.groupby("date")[col].mean()
    excess = (per_day_mean - market.reindex(per_day_mean.index)).dropna()
    gains, losses = trades[trades > 0].sum(), -trades[trades < 0].sum()
    by_year = trades.groupby(picks.loc[executed, "date"].str[:4]).mean()
    stats = {
        "days": int(days),
        "picks": int(len(picks)),
        "trades": int(executed.sum()),
        "fill_rate": float(executed.mean()) if len(picks) else float("nan"),
        "mean_trade": float(trades.mean()) if len(trades) else float("nan"),
        "mean_trade_worst": float(picks.loc[executed, f"{col}_worst"].mean())
        if f"{col}_worst" in picks and len(trades) else float("nan"),
        "median_trade": float(trades.median()) if len(trades) else float("nan"),
        "win_rate": float((trades > 0).mean()) if len(trades) else float("nan"),
        "profit_factor": float(gains / losses) if losses > 0 else float("nan"),
        "mean_excess_vs_market": float(excess.mean()) if len(excess) else float("nan"),
        "t_excess": newey_west_t(excess, lag=horizon),
        "mean_cohort": float(cohort.mean()) if len(cohort) else float("nan"),
        "t_cohort": newey_west_t(cohort, lag=horizon),
        "years_positive": int((by_year > 0).sum()),
        "years_total": int(len(by_year)),
        "by_year": {y: round(float(v), 5) for y, v in by_year.items()},
    }
    stats.update(_portfolio_stats(cohort, horizon))
    return stats


def market_benchmark(panel: pd.DataFrame, horizon: int) -> pd.Series:
    """میانگین بازده خالص همه سهم‌های نقدشونده قابل خرید در هر روز («خرید همه بازار»)."""
    return panel.groupby("date")[f"net_ret_{horizon}d"].mean()


def random_benchmark(panel: pd.DataFrame, horizon: int, k: int, trials: int, seed: int = 7) -> Dict[str, float]:
    """توزیع «میانگین هر معامله» برای K انتخاب تصادفی روزانه (همان جامعه و همان قواعد اجرا)."""
    rng = np.random.default_rng(seed)
    col = f"net_ret_{horizon}d"
    means = []
    for _ in range(trials):
        picks = pick_top(panel, pd.Series(rng.random(len(panel)), index=panel.index), k)
        means.append(picks[col].mean())
    means = np.array(means)
    return {"mean": float(means.mean()), "p05": float(np.percentile(means, 5)),
            "p95": float(np.percentile(means, 95)), "trials": int(trials)}


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
        all_trades = panel[f"net_ret_{h}d"].dropna()
        entry = {
            "market": {"mean_trade": float(all_trades.mean()), "win_rate": float((all_trades > 0).mean()),
                       "fill_rate": float(panel[f"net_ret_{h}d"].notna().mean()),
                       "t_cohort": newey_west_t(market, lag=h), "days": int(market.notna().sum())},
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
