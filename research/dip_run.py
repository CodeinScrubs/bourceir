# -*- coding: utf-8 -*-
"""
ارزیابی استراتژی «خرید در اصلاح» روی دوره توسعه؛ ردیف‌های قیمت و فیچر از holdout وارد ارزیابی نمی‌شوند.

پرسش: اگر پایان هر روز ۵ نامزد برتر (طبق یک قاعده انتخاب) را برگزینیم و فردا فقط وقتی بخریم که سهم d٪ زیر
قیمت مرجع آمد و با هدف tp٪ بفروشیم، بعد از کارمزد چقدر سود می‌ماند؟ آیا انتخاب نامزد از انتخاب تصادفی بهتر است؟

دستور: python brs.py research-dip
"""

import json
import math
import os
import uuid
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso, to_jalali
from database.db import DatabaseManager
from features.daily import FEATURE_SET_VERSION
from features.provenance import assert_compatible_inputs
from features.store import _git_commit, load_feature_snapshot
from research.baselines import STRATEGIES, pick_top, tolerant_t
from research.dip import DIP_VERSION, THROUGH_MARGIN, TIME_EXIT_PRICE_BASIS, DipRule, simulate
from research.labels import market_calendar
from research.run import _deduplicate_input, _json_safe, _sha256, archive_research_code, research_code_fingerprint

MAIN_RULES = [  # روش کاربر: ۲٪ زیر قیمت مرجع بخر، ۳ یا ۴٪ سود، «امروز یا فردا»
    DipRule(0.02, 0.03, 1, None, True), DipRule(0.02, 0.03, 1, None, False),
    DipRule(0.02, 0.04, 1, None, True), DipRule(0.02, 0.04, 1, None, False),
]
SENSITIVITY_RULES = [DipRule(d, tp, h, None, True) for d in (0.01, 0.02, 0.03) for tp in (0.02, 0.03, 0.04)
                     for h in (1, 2) if not (d == 0.02 and h == 1 and tp in (0.03, 0.04))]
RANDOM_DRAWS = 20
DIP_SUMMARY_VERSION = 'dip-summary-v1'


def _finite_stat(values, statistic, *args):
    values = np.asarray(values, dtype=float)
    return float(statistic(values, *args)) if len(values) and np.isfinite(values).all() else float("nan")


def summarize_random_draws(draws):
    """قرعه نامعلوم حذف نمی‌شود؛ آمار شرطیِ قرعه‌های معلوم جدا و با مخرج صریح است."""
    result = dict(label_fa=f'انتخاب تصادفی ({len(draws)} بار)', coverage={}, known_draw_diagnostics={})
    for key in ('mean_trade', 'mean_daily_cohort', 'fill_rate', 'win_rate', 'target_rate'):
        values = np.asarray([draw[key] for draw in draws], dtype=float)
        known = values[np.isfinite(values)]
        result['coverage'][key] = dict(known=len(known), unknown=len(values) - len(known), total=len(values))
        result[key] = _finite_stat(values, np.mean)
        result['known_draw_diagnostics'][key] = _finite_stat(known, np.mean)
        if key == 'mean_trade':
            result['mean_trade_p95'] = _finite_stat(values, np.percentile, 95)
            result['known_draw_diagnostics']['mean_trade_p95'] = _finite_stat(known, np.percentile, 95)
    return result


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
    df = picks[["date", "symbol"]].merge(trades, on=["date", "symbol"], how="left", validate="one_to_one")
    known = df["dip_status"].isin(["not_filled", "complete", "censored", "no_session"])
    filled = df["dip_status"].isin(["complete", "censored"])
    if "entry_filled" in df:
        known = df["entry_filled"].notna()
        filled = df["entry_filled"] == 1
    done = df["dip_status"] == "complete"
    nets = df.loc[done, "net_ret"]
    idle = df["dip_status"].isin(["not_filled", "no_session"])
    per_trade = df["net_ret"].where(done).mask(idle, 0.0)
    grouped = per_trade.groupby(df["date"])
    denominator = grouped.size() if k is None else k
    cohort = (grouped.sum(min_count=0) / denominator).where(grouped.count() == grouped.size())
    cohort = cohort.reindex(calendar_days, fill_value=0.0)
    years = nets.groupby(df.loc[done, "date"].str[:4]).mean()
    exits = df.loc[done, "exit_kind"].fillna("")
    regime_values = df.loc[done, "date"].map(regime)
    up, down = regime_values > 0.5, regime_values <= 0.5
    return {
        "picks": int(len(df)),
        "fill_rate": float(filled.sum() / known.sum()) if known.sum() else float("nan"),
        "trades": int(done.sum()),
        "censored": int((df["dip_status"] == "censored").sum()),
        "invalid_or_missing": int((df["dip_status"].isna() | (df["dip_status"] == "invalid")).sum()),
        "cohort_known_days": int(cohort.notna().sum()), "cohort_unknown_days": int(cohort.isna().sum()),
        "portfolio_simulated": False,
        "mean_trade": float(nets.mean()) if len(nets) else float("nan"),
        "median_trade": float(nets.median()) if len(nets) else float("nan"),
        "win_rate": float((nets > 0).mean()) if len(nets) else float("nan"),
        "target_rate": float(exits.str.startswith("target").mean()) if len(exits) else float("nan"),
        "mean_days_held": float(df.loc[done, "days_held"].mean()) if len(nets) else float("nan"),
        "mean_daily_cohort": _finite_stat(cohort, np.mean),
        "known_mean_daily_cohort": float(cohort.mean()),
        "t_cohort": tolerant_t(cohort, lag=hold + 1),
        "years_positive": int((years > 0).sum()), "years_total": int(len(years)),
        "by_year": {y: round(float(v), 5) for y, v in years.items()},
        "mean_trade_market_up": float(nets[up.to_numpy()].mean()) if up.any() else float("nan"),
        "mean_trade_market_down": float(nets[down.to_numpy()].mean()) if down.any() else float("nan"),
        "trades_market_up": int(up.sum()),
        "trades_market_down": int(down.sum()), "trades_market_unknown": int(regime_values.isna().sum()),
    }


def run_dip_research(db: DatabaseManager, log_experiment: bool = True,
                     rules: Optional[List[DipRule]] = None) -> Dict[str, Any]:
    cfg = settings.RESEARCH_CONFIG
    code_sha = research_code_fingerprint()
    cutoff, start, k = cfg["holdout_start"], cfg["dev_start"], cfg["top_k"]
    features, feature_manifest = load_feature_snapshot(filters=[("date", ">=", start), ("date", "<", cutoff)])
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute("BEGIN")
    try:
        assert_compatible_inputs(db, feature_manifest, end_exclusive=cutoff)
        prices = load_prices_until(db, cutoff)
        schema_version = db.schema_version()
    finally:
        if own_snapshot:
            db.conn.rollback()
    if prices.empty:
        raise ValueError("داده قیمت موجود نیست؛ ابتدا به‌روزرسانی داده‌ها را اجرا کنید")
    calendar = market_calendar(prices)
    if features.empty:
        raise ValueError("انبار فیچر خالی است؛ ابتدا python brs.py features را اجرا کنید")
    features = features.loc[(features["date"] >= start) & (features["date"] < cutoff)]
    liquid = prices.loc[(prices["date"] >= start) & (prices["value"] >= cfg["min_daily_value_rial"]), ["date", "symbol", "value"]]
    panel = features.merge(liquid, on=["date", "symbol"], how="inner", validate="one_to_one")
    if panel.empty:
        raise ValueError("در دوره توسعه نامزد دارای داده و نقدشوندگی کافی وجود ندارد")
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

    rules = list(rules if rules is not None else MAIN_RULES + SENSITIVITY_RULES)
    if not rules or len({r.name for r in rules}) != len(rules):
        raise ValueError("قواعد باید غیرخالی و دارای نام یکتا باشند")
    experiment_id = f"EXP-{now_tehran_iso()[:10]}-DIP-{uuid.uuid4().hex}"
    folder = settings.DATA_DIR / "research"
    artifacts = folder / experiment_id
    artifacts.mkdir(parents=True)
    source_hashes = archive_research_code(artifacts / "code")
    if research_code_fingerprint() != code_sha:
        raise RuntimeError("کد پژوهش هنگام اجرا تغییر کرده است؛ اجرا را با کد ثابت تکرار کنید")
    pinned_inputs = {}
    selected = pd.concat([p.assign(selector=name) for name, p in candidates.items()], ignore_index=True)
    for name, frame in (("prices", prices), ("features", features), ("candidates", selected),
                        ("calendar", pd.DataFrame({"date": calendar}))):
        path = artifacts / f"{name}.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        pinned_inputs[name] = {"file": path.name, "sha256": _deduplicate_input(path, folder / "objects"), "rows": len(frame)}
    results: Dict[str, Any] = {}
    outcome_files = {}
    for rule_index, rule in enumerate(rules):
        trades = simulate(prices, calendar, rule, decisions=panel[["date", "symbol"]])
        trades = trades.loc[trades["dip_status"] != "skipped"]
        outcome_path = artifacts / f"trades-{rule_index:03d}.parquet"
        trades.to_parquet(outcome_path, index=False, compression="zstd")
        outcome_files[rule.name] = {"file": outcome_path.name, "sha256": _sha256(outcome_path), "rows": len(trades)}
        entry: Dict[str, Any] = {}
        for name, picks in candidates.items():
            if name.startswith("random_"):
                continue
            stats = evaluate(trades, picks, calendar_days, None if name == "all_liquid" else k, rule.hold, regime)
            stats["label_fa"] = labels_fa[name]
            entry[name] = stats
        draws = [evaluate(trades, candidates[f"random_{d}"], calendar_days, k, rule.hold, regime)
                 for d in range(RANDOM_DRAWS)]
        entry["random"] = summarize_random_draws(draws)
        results[rule.name] = {"rule": rule.__dict__, "candidates": entry}

    if research_code_fingerprint() != code_sha:
        raise RuntimeError("کد پژوهش هنگام اجرا تغییر کرده است؛ نتیجه منتشر نشد")
    params = {"version": DIP_VERSION, "summary_version": DIP_SUMMARY_VERSION,
              "top_k": k, "period": [start, cutoff], "random_draws": RANDOM_DRAWS,
              "min_daily_value_rial": cfg["min_daily_value_rial"], "fees": dict(settings.FEES),
              "rules": [r.name for r in rules], "rule_definitions": {r.name: r.__dict__ for r in rules},
              "slippage_bps_time_exit": settings.BACKTEST_CONFIG["default_slippage_bps"],
              "time_exit_price_basis": TIME_EXIT_PRICE_BASIS,
              "lock_min_move_pct": settings.BACKTEST_CONFIG["lock_min_move_pct"], "through_margin": THROUGH_MARGIN,
              "random_seed": 11, "inputs": pinned_inputs, "outcomes": outcome_files,
              "feature_source_manifest": feature_manifest,
              "code_sha256": code_sha,
              "source_files_sha256": source_hashes, "git_commit": _git_commit(),
              "database_schema_version": schema_version, "corporate_action_model": "synthetic_reference_share_multiplier",
              "portfolio_simulated": False, "holdout": "sealed for this run; prices and feature rows restricted before cutoff"}
    payload = {"experiment_id": experiment_id, "created_at": now_tehran_iso(), "params": params, "results": results,
               "accepted_for_signals": False}
    clean = _json_safe(payload)
    (folder / f"{experiment_id}.json").write_text(json.dumps(clean, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    (artifacts / "manifest.json").write_text(json.dumps(_json_safe(params), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report = format_dip_report(payload)
    report_path = folder / f"{experiment_id}.md"
    report_path.write_text(report, encoding="utf-8")
    if log_experiment:
        main = results.get(MAIN_RULES[0].name, {}).get("candidates", {})
        logged = db.log_experiment(
            experiment_id=experiment_id, strategy_name="dip-buy", symbols_tested="universe",
            parameters_json=json.dumps(_json_safe(params), ensure_ascii=False, allow_nan=False),
            metrics_json=json.dumps(_json_safe({n: round(s.get("mean_trade", float("nan")), 5) for n, s in main.items()}),
                                    ensure_ascii=False, allow_nan=False),
            start_date=start, end_date=cutoff, dataset_version=f"prices<{cutoff}",
            features_version=FEATURE_SET_VERSION, model_version=DIP_VERSION)
        if logged is None:
            raise RuntimeError("ثبت آزمایش در دیتابیس ناموفق بود")
        pointer_tmp = folder / f"latest-dip-{uuid.uuid4().hex}.json.tmp"
        pointer_tmp.write_text(json.dumps({"experiment_id": experiment_id}), encoding="utf-8")
        os.replace(pointer_tmp, folder / "latest-dip.json")
    payload["report"] = report
    payload["report_path"] = str(report_path)
    return payload


def _pct(x: Any, digits: int = 2) -> str:
    return "—" if x is None or not math.isfinite(x) else f"{x * 100:+.{digits}f}٪"


def format_dip_report(payload: Dict[str, Any]) -> str:
    p, res = payload["params"], payload["results"]
    lines = [
        f"# آزمون روش «خرید در اصلاح» — {payload['experiment_id']}",
        "",
        f"تاریخ اجرا: {to_jalali(payload['created_at'][:10], fa_digits=False)} | دوره: {p['period'][0]} تا {p['period'][1]} "
        f"(این اجرا فقط ردیف‌های پیش از مرز holdout را ارزیابی می‌کند) | هر روز {p['top_k']} نامزد | فقط سهم‌های با ارزش معاملات ≥ "
        f"{p['min_daily_value_rial'] / 1e10:.0f} میلیارد تومان",
        "",
        "**روش:** پایان روز نامزدها انتخاب می‌شوند. فردا سفارش خرید در «قیمت مرجع منهای d٪» گذاشته می‌شود؛ اگر سهم به آن "
        "رسید خرید (اگر پایین‌تر باز شد، به قیمت گشایش). فروش با سفارش در «قیمت خرید به‌علاوه هدف»؛ اگر تا پایان روز بعد "
        "(یا دو روز بعد) نرسید، خروج با قیمت آخرین معامله همان روز منهای لغزش مدل می‌شود؛ میانگین کل روز پس از انتظار تا پایان روز قابل ادعا نیست. کارمزد خرید ۰.۳۷۱۲٪ و فروش ۰.۸۸٪ کسر شده است.",
        "",
        "**محافظه‌کار / خوش‌بینانه:** داده روزانه نمی‌گوید سقف روز قبل از کف آمده یا بعد از آن. «محافظه‌کار» فرض می‌کند "
        "هدف در همان روز خرید (بعد از افت) نمی‌رسد و لمس دقیق قیمت سفارش کافی نیست؛ «خوش‌بینانه» برعکس. این دو فرض، کران تضمینی واقعیت نیستند.",
        "",
        "ستون‌ها: «خرید شد» = درصد نامزدهایی که به قیمت سفارش رسیدند؛ «میانگین معامله» = سود خالص هر خرید؛ «هدف» = درصد "
        "معامله‌هایی که به هدف رسیدند؛ «روزانه» = میانگین گروه تصمیم‌ها (سرمایه بیکار لحاظ شده؛ بازده سبد نیست)؛ "
        "«بازار مثبت/منفی» = میانگین معامله وقتی پهنای ۵ روزه بازار بالای ۵۰٪ / زیر ۵۰٪ بوده؛ «سال‌های مثبت» = پایداری.",
        "«روزانه» فقط با معلوم بودن همه گروه‌های تصمیم گزارش می‌شود. میانگین روزهای معلوم به‌تنهایی بازده کل دوره نیست. "
        "قرعه نامعلوم از کنترل تصادفی حذف نمی‌شود؛ آمار شرطی فقط در known_draw_diagnostics و با شمار قرعه‌های معلوم ذخیره است.",
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
        if pd.notna(random_p95):
            lines.append(f"\nصدک ۹۵ میانگین معامله در قرعه‌های تصادفی همین اجرا: {_pct(random_p95)}؛ این احتمال سود آینده نیست.")
        else:
            lines.append('\nصدک کنترل تصادفی نامعلوم است؛ قرعه نامعلوم از مخرج حذف نشد.')
        coverage = entry['random'].get('coverage', {})
        for metric in ('mean_trade', 'mean_daily_cohort'):
            if metric in coverage:
                counts = coverage[metric]
                lines.append(f"کنترل تصادفی {metric}: {counts['known']}/{counts['total']} قرعه معلوم؛ {counts['unknown']} نامعلوم.")
        lines.extend(['', '| نامزدها | روز معلوم | روز نامعلوم | خروج سانسورشده | نتیجه نامعتبر/گمشده |',
                      '|---|---:|---:|---:|---:|'])
        for name in order:
            item = entry[name]
            lines.append(f"| {item['label_fa']} | {item['cohort_known_days']} | {item['cohort_unknown_days']} | "
                         f"{item['censored']} | {item['invalid_or_missing']} |")

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
                  "اسنپ‌شات لحظه‌ای به‌تنهایی ترتیب همه معاملات و پر شدن سفارش ما را ثابت نمی‌کند.",
                  "- جایگاه سفارش در صف و پر شدن جزئی مدل نشده است؛ لمس قیمت لزوماً یعنی خرید ما نیست.",
                  "- جامعه فقط نمادهای امروز بازار است (سوگیری بقا).",
                  "- چندین پارامتر و روش آزموده شده؛ بهترین عدد جدول ممکن است شانسی باشد. معیار، پایداری در سال‌ها و هر دو حالت است.",
                  "- سرمایه مشترک، هم‌پوشانی پوزیشن‌ها و محدودیت حجم سفارش شبیه‌سازی نشده‌اند؛ بازده گروه، سود سرمایه واقعی نیست.",
                  "- تعدیل اقدام شرکتی فقط ضریب مصنوعی تعداد سهم است؛ وصول سود، حق‌تقدم و زمان قابلیت فروش مدل نشده‌اند.",
                  "- این گزارش توصیه معاملاتی نیست."])
    return "\n".join(lines) + "\n"
