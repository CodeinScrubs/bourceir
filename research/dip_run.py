# -*- coding: utf-8 -*-
"""
ارزیابی استراتژی «خرید در اصلاح» روی دوره توسعه (دوره بیرون از نمونه مهر و موم می‌ماند و حتی خوانده نمی‌شود).

پرسش: اگر پایان هر روز ۵ نامزد برتر (طبق یک قاعده انتخاب) را برگزینیم و فردا فقط وقتی بخریم که سهم d٪ زیر
قیمت مرجع آمد و با هدف tp٪ بفروشیم، بعد از کارمزد چقدر سود می‌ماند؟ آیا انتخاب نامزد از انتخاب تصادفی بهتر است؟

دستور: python brs.py research-dip
"""

import json
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso, to_jalali
from database.db import DatabaseManager
from features.daily import FEATURE_SET_VERSION
from features.store import load_feature_store
from research.baselines import STRATEGIES, pick_top, tolerant_t
from research.dip import DIP_VERSION, DipRule, simulate
from research.labels import market_calendar

MAIN_RULES = [  # روش کاربر: ۲٪ زیر قیمت مرجع بخر، ۳ یا ۴٪ سود، «امروز یا فردا»
    DipRule(0.02, 0.03, 1, None, True), DipRule(0.02, 0.03, 1, None, False),
    DipRule(0.02, 0.04, 1, None, True), DipRule(0.02, 0.04, 1, None, False),
]
SENSITIVITY_RULES = [DipRule(d, tp, h, None, True) for d in (0.01, 0.02, 0.03) for tp in (0.02, 0.03, 0.04)
                     for h in (1, 2) if not (d == 0.02 and h == 1 and tp in (0.03, 0.04))]
RANDOM_DRAWS = 20


def load_prices_until(db: DatabaseManager, end_date: str) -> pd.DataFrame:
    """قیمت خام جامعه تا قبل از end_date (داده بیرون از نمونه خوانده نمی‌شود)."""
    ids = [s["id"] for s in db.get_universe_symbols(include_inactive=True)]
    if not ids:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(ids))
    return pd.read_sql_query(
        f"""SELECT s.symbol_fa AS symbol, dp.date, dp.open_raw, dp.high_raw, dp.low_raw, dp.close_raw,
                   dp.close_final_raw, dp.ref_price_raw, dp.volume, dp.value
            FROM daily_prices dp JOIN symbols s ON s.id = dp.symbol_id
            WHERE dp.source = 'tsetmc' AND dp.symbol_id IN ({placeholders}) AND dp.date < ?;""",
        db.conn, params=ids + [end_date])


def evaluate(trades: pd.DataFrame, picks: pd.DataFrame, calendar_days: pd.Index, k: Optional[int],
             hold: int, regime: pd.Series) -> Dict[str, Any]:
    """آمار یک قاعده × یک روش انتخاب نامزد. k=None یعنی «همه سهم‌های نقدشونده» (وزن برابر)."""
    df = picks[["date", "symbol"]].merge(trades, on=["date", "symbol"], how="left")
    known = df["dip_status"].isin(["not_filled", "complete", "censored", "no_session"])
    filled = df["dip_status"].isin(["complete", "censored"])
    done = df["dip_status"] == "complete"
    nets = df.loc[done, "net_ret"]
    per_trade = df["net_ret"].where(done, 0.0).where(df["dip_status"] != "censored")
    grouped = per_trade.groupby(df["date"])
    denominator = grouped.size() if k is None else k
    cohort = (grouped.sum(min_count=0) / denominator).where(grouped.count() == grouped.size())
    cohort = cohort.reindex(calendar_days, fill_value=0.0)
    years = nets.groupby(df.loc[done, "date"].str[:4]).mean()
    exits = df.loc[done, "exit_kind"].fillna("")
    up = df.loc[done, "date"].map(regime) > 0.5
    return {
        "picks": int(len(df)),
        "fill_rate": float(filled.sum() / known.sum()) if known.sum() else float("nan"),
        "trades": int(done.sum()),
        "censored": int((df["dip_status"] == "censored").sum()),
        "mean_trade": float(nets.mean()) if len(nets) else float("nan"),
        "median_trade": float(nets.median()) if len(nets) else float("nan"),
        "win_rate": float((nets > 0).mean()) if len(nets) else float("nan"),
        "target_rate": float(exits.str.startswith("target").mean()) if len(exits) else float("nan"),
        "mean_days_held": float(df.loc[done, "days_held"].mean()) if len(nets) else float("nan"),
        "mean_daily_cohort": float(cohort.mean()),
        "t_cohort": tolerant_t(cohort, lag=hold + 1),
        "years_positive": int((years > 0).sum()), "years_total": int(len(years)),
        "by_year": {y: round(float(v), 5) for y, v in years.items()},
        "mean_trade_market_up": float(nets[up.to_numpy()].mean()) if up.any() else float("nan"),
        "mean_trade_market_down": float(nets[~up.to_numpy()].mean()) if (~up).any() else float("nan"),
        "trades_market_up": int(up.sum()),
    }


def run_dip_research(db: DatabaseManager, log_experiment: bool = True,
                     rules: Optional[List[DipRule]] = None) -> Dict[str, Any]:
    cfg = settings.RESEARCH_CONFIG
    cutoff, start, k = cfg["holdout_start"], cfg["dev_start"], cfg["top_k"]
    prices = load_prices_until(db, cutoff)
    if prices.empty:
        raise ValueError("داده قیمت موجود نیست؛ ابتدا به‌روزرسانی داده‌ها را اجرا کنید")
    calendar = market_calendar(prices)
    features = load_feature_store()
    if features.empty:
        raise ValueError("انبار فیچر خالی است؛ ابتدا python brs.py features را اجرا کنید")
    features = features.loc[(features["date"] >= start) & (features["date"] < cutoff)]
    liquid = prices.loc[(prices["date"] >= start) & (prices["value"] >= cfg["min_daily_value_rial"]), ["date", "symbol", "value"]]
    panel = features.merge(liquid, on=["date", "symbol"], how="inner")
    regime = panel.groupby("date")["mkt_breadth_5d"].first()
    calendar_days = pd.Index(sorted(panel["date"].unique()))

    candidates: Dict[str, pd.DataFrame] = {"all_liquid": panel[["date", "symbol"]]}
    labels_fa = {"all_liquid": "همه سهم‌های نقدشونده (بدون انتخاب)"}
    for strategy in STRATEGIES:
        candidates[strategy.name] = pick_top(panel, strategy.score(panel), k)[["date", "symbol"]]
        labels_fa[strategy.name] = strategy.label_fa
    rng = np.random.default_rng(11)
    for draw in range(RANDOM_DRAWS):
        candidates[f"random_{draw}"] = pick_top(panel, pd.Series(rng.random(len(panel)), index=panel.index), k)[["date", "symbol"]]

    rules = rules or (MAIN_RULES + SENSITIVITY_RULES)
    results: Dict[str, Any] = {}
    for rule in rules:
        trades = simulate(prices, calendar, rule, decisions=panel[["date", "symbol"]])
        trades = trades.loc[trades["dip_status"] != "skipped"]
        entry: Dict[str, Any] = {}
        for name, picks in candidates.items():
            if name.startswith("random_"):
                continue
            stats = evaluate(trades, picks, calendar_days, None if name == "all_liquid" else k, rule.hold, regime)
            stats["label_fa"] = labels_fa[name]
            entry[name] = stats
        draws = [evaluate(trades, candidates[f"random_{d}"], calendar_days, k, rule.hold, regime)
                 for d in range(RANDOM_DRAWS)]
        entry["random"] = {
            "label_fa": f"انتخاب تصادفی ({RANDOM_DRAWS} بار)",
            "mean_trade": float(np.nanmean([d["mean_trade"] for d in draws])),
            "mean_daily_cohort": float(np.mean([d["mean_daily_cohort"] for d in draws])),
            "mean_trade_p95": float(np.nanpercentile([d["mean_trade"] for d in draws], 95)),
            "fill_rate": float(np.nanmean([d["fill_rate"] for d in draws])),
            "win_rate": float(np.nanmean([d["win_rate"] for d in draws])),
            "target_rate": float(np.nanmean([d["target_rate"] for d in draws])),
        }
        results[rule.name] = {"rule": rule.__dict__, "candidates": entry}

    experiment_id = f"EXP-{now_tehran_iso()[:10]}-DIP-{uuid.uuid4().hex[:12]}"
    params = {"version": DIP_VERSION, "top_k": k, "period": [start, cutoff], "random_draws": RANDOM_DRAWS,
              "min_daily_value_rial": cfg["min_daily_value_rial"], "fees": dict(settings.FEES),
              "rules": [r.name for r in rules], "holdout": "sealed (prices after holdout_start not loaded)"}
    payload = {"experiment_id": experiment_id, "created_at": now_tehran_iso(), "params": params, "results": results}
    folder = settings.DATA_DIR / "research"
    folder.mkdir(parents=True, exist_ok=True)
    clean = json.loads(json.dumps(payload, default=float).replace("NaN", "null"))
    (folder / f"{experiment_id}.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    report = format_dip_report(payload)
    (folder / f"{experiment_id}.md").write_text(report, encoding="utf-8")
    (folder / "latest-dip.json").write_text(json.dumps({"experiment_id": experiment_id}), encoding="utf-8")
    if log_experiment:
        main = results.get(MAIN_RULES[0].name, {}).get("candidates", {})
        logged = db.log_experiment(
            experiment_id=experiment_id, strategy_name="dip-buy", symbols_tested="universe",
            parameters_json=json.dumps(params, ensure_ascii=False),
            metrics_json=json.dumps({n: round(s.get("mean_trade", float("nan")), 5) for n, s in main.items()},
                                    ensure_ascii=False).replace("NaN", "null"),
            start_date=start, end_date=cutoff, dataset_version=f"prices<{cutoff}",
            features_version=FEATURE_SET_VERSION, model_version=DIP_VERSION)
        if logged is None:
            raise RuntimeError("ثبت آزمایش در دیتابیس ناموفق بود")
    payload["report"] = report
    return payload


def _pct(x: Any, digits: int = 2) -> str:
    return "—" if x is None or pd.isna(x) else f"{x * 100:+.{digits}f}٪"


def format_dip_report(payload: Dict[str, Any]) -> str:
    p, res = payload["params"], payload["results"]
    lines = [
        f"# آزمون روش «خرید در اصلاح» — {payload['experiment_id']}",
        "",
        f"تاریخ اجرا: {to_jalali(payload['created_at'][:10], fa_digits=False)} | دوره: {p['period'][0]} تا {p['period'][1]} "
        f"(دوره ۱۴۰۴ به بعد مهر و موم و خوانده‌نشده) | هر روز {p['top_k']} نامزد | فقط سهم‌های با ارزش معاملات ≥ "
        f"{p['min_daily_value_rial'] / 1e10:.0f} میلیارد تومان",
        "",
        "**روش:** پایان روز نامزدها انتخاب می‌شوند. فردا سفارش خرید در «قیمت مرجع منهای d٪» گذاشته می‌شود؛ اگر سهم به آن "
        "رسید خرید (اگر پایین‌تر باز شد، به قیمت گشایش). فروش با سفارش در «قیمت خرید به‌علاوه هدف»؛ اگر تا پایان روز بعد "
        "(یا دو روز بعد) نرسید، فروش به میانگین قیمت همان روز. کارمزد خرید ۰.۳۷۱۲٪ و فروش ۰.۸۸٪ کسر شده است.",
        "",
        "**محافظه‌کار / خوش‌بینانه:** داده روزانه نمی‌گوید سقف روز قبل از کف آمده یا بعد از آن. «محافظه‌کار» فرض می‌کند "
        "هدف در همان روز خرید (بعد از افت) نمی‌رسد و لمس دقیق قیمت سفارش کافی نیست؛ «خوش‌بینانه» برعکس. واقعیت بین این دو است.",
        "",
        "ستون‌ها: «خرید شد» = درصد نامزدهایی که به قیمت سفارش رسیدند؛ «میانگین معامله» = سود خالص هر خرید؛ «هدف» = درصد "
        "معامله‌هایی که به هدف رسیدند؛ «روزانه» = بازده متوسط روزانه سرمایه (نامزد خریدنشده = سرمایه بیکار)؛ "
        "«بازار مثبت/منفی» = میانگین معامله وقتی پهنای ۵ روزه بازار بالای ۵۰٪ / زیر ۵۰٪ بوده؛ «سال‌های مثبت» = پایداری.",
    ]

    def table(rule_name: str, title: str) -> None:
        if rule_name not in res:
            return
        entry = res[rule_name]["candidates"]
        lines.extend(["", f"### {title}", "",
                      "| نامزدها | خرید شد | میانگین معامله | نرخ برد | هدف | روزانه | بازار مثبت | بازار منفی | سال‌های مثبت |",
                      "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
        order = sorted((n for n in entry if n != "random"), key=lambda n: -(entry[n]["mean_trade"]
                                                                           if pd.notna(entry[n]["mean_trade"]) else -9))
        for name in order + ["random"]:
            s = entry[name]
            years = f"{s['years_positive']}/{s['years_total']}" if "years_total" in s else "—"
            lines.append(
                f"| {s['label_fa']} | {_pct(s['fill_rate'], 0)} | {_pct(s['mean_trade'])} | {_pct(s['win_rate'], 0)} | "
                f"{_pct(s['target_rate'], 0)} | {_pct(s['mean_daily_cohort'], 3)} | {_pct(s.get('mean_trade_market_up'))} | "
                f"{_pct(s.get('mean_trade_market_down'))} | {years} |")
        random_p95 = entry["random"].get("mean_trade_p95")
        lines.append(f"\nمرز شانس: ۹۵٪ انتخاب‌های تصادفی میانگین معامله‌ای کمتر از {_pct(random_p95)} داشتند.")

    for rule in MAIN_RULES:
        mode = "محافظه‌کار" if rule.conservative else "خوش‌بینانه"
        table(rule.name, f"روش شما: خرید در −{rule.dip:.0%}، هدف +{rule.take_profit:.0%}، تا فردا ({mode})")

    grid = [n for n in res if n not in {r.name for r in MAIN_RULES}]
    if grid:
        lines.extend(["", "### حساسیت به پارامترها (محافظه‌کار؛ میانگین معامله خالص)", "",
                      "| افت خرید | هدف | نگهداری | همه نقدشونده | صف خرید | قدرت خریدار | مومنتوم ۵ روزه | تصادفی |",
                      "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for name in grid:
            r, c = res[name]["rule"], res[name]["candidates"]
            lines.append(f"| −{r['dip']:.0%} | +{r['take_profit']:.0%} | {r['hold']} | {_pct(c['all_liquid']['mean_trade'])} | "
                         f"{_pct(c['buy_queue']['mean_trade'])} | {_pct(c['buyer_power']['mean_trade'])} | "
                         f"{_pct(c['momentum_5d']['mean_trade'])} | {_pct(c['random']['mean_trade'])} |")
    lines.extend(["", "## محدودیت‌ها",
                  "- ترتیب حرکت قیمت در طول روز از داده روزانه معلوم نیست؛ دو حالت محافظه‌کار و خوش‌بینانه گزارش شده است. "
                  "دیده‌بان لحظه‌ای (از مهر ۱۴۰۵) این ابهام را در آینده حل می‌کند.",
                  "- جایگاه سفارش در صف و پر شدن جزئی مدل نشده است؛ لمس قیمت لزوماً یعنی خرید ما نیست.",
                  "- جامعه فقط نمادهای امروز بازار است (سوگیری بقا).",
                  "- چندین پارامتر و روش آزموده شده؛ بهترین عدد جدول ممکن است شانسی باشد. معیار، پایداری در سال‌ها و هر دو حالت است.",
                  "- این گزارش توصیه معاملاتی نیست."])
    return "\n".join(lines) + "\n"
