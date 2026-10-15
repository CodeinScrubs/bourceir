"""Registered development-only walk-forward replay from verified, pinned baseline inputs."""

import json
import os
import re
import uuid
from dataclasses import asdict, replace

import numpy as np
import pandas as pd

from backtest.portfolio import PORTFOLIO_VERSION, PortfolioConfig, replay_portfolio
from config import settings
from core.timeutils import now_tehran_iso, to_jalali
from database.workbench import WorkbenchRepository
from research.baselines import STRATEGIES, _cohort_returns, pick_top
from research.run import _json_safe, _sha256, archive_research_code, research_code_fingerprint
from research.walkforward import time_folds

WALKFORWARD_VERSION = "expanding-portfolio-v1"


def verified_baseline_inputs(root, experiment_id=None):
    if experiment_id is None:
        experiment_id = json.loads((root / "latest.json").read_text(encoding="utf-8"))["experiment_id"]
    if not re.fullmatch(r"EXP-\d{4}-\d{2}-\d{2}-BASELINES-[a-f0-9]{32}", experiment_id):
        raise ValueError("شناسه پژوهش پایه معتبر نیست")
    payload = json.loads((root / f"{experiment_id}.json").read_text(encoding="utf-8"))
    if payload["params"].get("holdout_selection") or "holdout" in payload["results"]:
        raise ValueError("پژوهش دارای دوره نهایی در انتخاب روش استفاده نمی‌شود")
    folder = root / experiment_id
    label_manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    digests = dict(features=payload["params"]["features"]["sha256"],
                   labels=label_manifest["parquet_sha256"], prices=label_manifest["raw_prices_sha256"])
    cutoff = settings.RESEARCH_CONFIG["holdout_start"]
    boundary = label_manifest.get("exclusive_end_date")
    if not boundary or boundary > cutoff:
        raise ValueError("منبع پژوهش فقط به دوره توسعه محدود نشده است")
    frames = {}
    for name, expected in digests.items():
        path = folder / f"{name}.parquet"
        if _sha256(path) != expected:
            raise ValueError("اثر انگشت ورودی پژوهش پایه ناسازگار است")
        frames[name] = pd.read_parquet(path)
        if not frames[name].empty and (frames[name]["date"] >= cutoff).any():
            raise ValueError("ورودی پژوهش شامل دوره نهایی است")
        if _sha256(path) != expected:
            raise ValueError("ورودی پژوهش هنگام خواندن تغییر کرد")
    return experiment_id, frames, digests


def select_from_past(panel, fold, horizons, k):
    """Rank each fixed baseline only on labels known before the embargo boundary.

    Ranks are formed before purging outcomes; an unknown future exit cannot improve historical
    candidate ranks by removing that stock. Unknown picks remain unknown in the score series.
    """
    past = panel.loc[(panel["date"] >= fold.train_start) & (panel["date"] < fold.train_end_exclusive)].copy()
    calendar = pd.Index(sorted(past["date"].unique()))
    candidates, best = [], None
    for horizon in horizons:
        status, exit_col, net_col = f"label_status_{horizon}d", f"exit_date_{horizon}d", f"net_ret_{horizon}d"
        known_exit = (past[status] == "complete") & (past[exit_col] != "") & (past[exit_col] < fold.train_end_exclusive)
        known_block = (past[status] == "entry_blocked") & (past["entry_date"] != "") & (past["entry_date"] < fold.train_end_exclusive)
        evaluated = past.copy()
        evaluated.loc[~(known_exit | known_block), status] = "censored"
        evaluated.loc[~known_exit, net_col] = np.nan
        for strategy in STRATEGIES:
            picks = pick_top(evaluated, strategy.score(evaluated), k)
            returns = _cohort_returns(picks, calendar, horizon, k)
            unknown = float(returns.isna().mean()) if len(returns) else 1.0
            mean = float(returns.mean()) if len(returns) else np.nan
            acceptable = np.isfinite(mean) and unknown <= 0.05 and len(returns) >= 100
            candidate = dict(strategy=strategy.name, horizon=horizon, mean_cohort=mean,
                             unknown_fraction=unknown, scored_days=len(returns), selectable=bool(acceptable))
            candidates.append(candidate)
            if acceptable and mean > 0 and (best is None or mean > best["mean_cohort"]):
                best = candidate
    return best, candidates


def _picks(panel, strategy, k, random_seed=None):
    if random_seed is None:
        rule = next(s for s in STRATEGIES if s.name == strategy)
        score = rule.score(panel)
    else:
        score = pd.Series(np.random.default_rng(random_seed).random(len(panel)), index=panel.index)
    picked = pick_top(panel, score, k)[["date", "symbol"]].copy()
    picked["rank"] = picked.groupby("date").cumcount()
    return picked


def run_portfolio_research(db, *, source_id=None, initial_capital_rial=None, min_train_sessions=504,
                           test_sessions=252, horizons=(2, 3), random_seed=41):
    root = settings.DATA_DIR / "research"
    source_id, inputs, digests = verified_baseline_inputs(root, source_id)
    features, labels, prices = (inputs[key] for key in ("features", "labels", "prices"))
    cfg = dict(settings.RESEARCH_CONFIG)
    horizons = tuple(horizons)
    if not horizons or any(isinstance(h, bool) or not isinstance(h, int) or h not in cfg["horizons"] for h in horizons):
        raise ValueError("افق‌های موجود پژوهش پایه لازم‌اند")
    panel = features.merge(labels, on=["date", "symbol"], validate="one_to_one")
    panel = panel.loc[(panel["date"] >= cfg["dev_start"]) & (panel["date"] < cfg["holdout_start"])
                      & (panel["value"] >= cfg["min_daily_value_rial"])].reset_index(drop=True)
    from research.labels import market_calendar
    calendar = market_calendar(prices)
    dev_calendar = calendar[calendar >= cfg["dev_start"]]
    folds = time_folds(dev_calendar, min_train_sessions=min_train_sessions, test_sessions=test_sessions,
                       embargo_sessions=max(horizons) + 1, end_exclusive=cfg["holdout_start"])
    profile = WorkbenchRepository(db).profile()
    capital = initial_capital_rial if initial_capital_rial is not None else (
        profile["capital_rial"] if profile else settings.BACKTEST_CONFIG["initial_capital"])
    max_loss = profile["max_loss_rial"] / profile["capital_rial"] if profile else 0.10
    base = PortfolioConfig(initial_capital_rial=float(capital), max_positions=cfg["top_k"], max_loss_pct=max_loss,
                           participation=min(0.05, settings.BACKTEST_CONFIG["max_volume_participation_pct"]),
                           slippage_bps=settings.BACKTEST_CONFIG["default_slippage_bps"])
    code_sha = research_code_fingerprint()
    experiment_id = f"EXP-{now_tehran_iso()[:10]}-PORTFOLIO-{uuid.uuid4().hex}"
    artifacts = root / experiment_id
    artifacts.mkdir(parents=True)
    from research.run import _deduplicate_input
    for name, frame in inputs.items():
        path = artifacts / f"{name}.parquet"
        frame.to_parquet(path, index=False, compression="zstd")
        _deduplicate_input(path, root / "objects")
    protocol = dict(version=WALKFORWARD_VERSION, portfolio_version=PORTFOLIO_VERSION, source_id=source_id, inputs=digests,
                    config=asdict(base), horizons=list(horizons), random_seed=random_seed, folds=[asdict(f) for f in folds],
                    selection="highest positive past cohort mean, otherwise idle cash; fixed eight existing baselines",
                    purge="actual exit before train boundary; unknown rank rows retained", holdout_start=cfg["holdout_start"],
                    prospective_independence=False, code_sha256=code_sha, source_files=archive_research_code(artifacts / "code"))
    registered = db.log_experiment(experiment_id=experiment_id + "-PROTOCOL", strategy_name="portfolio-protocol",
        symbols_tested="universe", parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json="{}", start_date=cfg["dev_start"], end_date=cfg["holdout_start"],
        dataset_version=source_id, features_version=features.attrs.get("version", "pinned-baseline"), model_version=WALKFORWARD_VERSION)
    if registered is None:
        raise RuntimeError("ثبت پروتکل پیش از ارزیابی ناموفق بود")
    (artifacts / "protocol.json").write_text(json.dumps(_json_safe(protocol), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    results, accumulated = [], {mode: [] for mode in ("selected", "stress", "random", "baseline")}
    capitals = {mode: float(capital) for mode in accumulated}
    for index, fold in enumerate(folds):
        selected, training = select_from_past(panel, fold, horizons, cfg["top_k"])
        test = panel.loc[(panel["date"] >= fold.test_start) & (panel["date"] < fold.test_end_exclusive)]
        selections = _picks(test, selected["strategy"], cfg["top_k"]) if selected else pd.DataFrame(columns=["date", "symbol", "rank"])
        # Independent test accounts; never silently liquidate/reuse cash from an open previous-fold holding.
        horizon = selected["horizon"] if selected else horizons[0]
        result = dict(fold=asdict(fold), selected=selected, training=training, modes={})
        prior = prices.loc[prices["date"] < fold.test_start].groupby("symbol", sort=False).tail(1)
        test_prices = prices.loc[(prices["date"] >= fold.test_start) & (prices["date"] < fold.test_end_exclusive)]
        replay_prices = pd.concat([prior, test_prices], ignore_index=True)
        test_calendar = calendar[(calendar >= fold.test_start) & (calendar < fold.test_end_exclusive)]
        # Stop new entries a horizon before the fold boundary; locked/partial exits remain visibly open.
        selection_end = test_calendar[max(0, len(test_calendar) - horizon - 1)]
        selections = selections.loc[selections["date"] < selection_end]
        mode_picks = dict(selected=selections, stress=selections,
                          random=_picks(test.loc[test.date < selection_end], None, cfg["top_k"], random_seed + index),
                          baseline=_picks(test.loc[test.date < selection_end], "buyer_power", cfg["top_k"]))
        for mode, picked in mode_picks.items():
            configuration = replace(base, initial_capital_rial=capitals[mode], horizon=horizon, stress=mode == "stress")
            replay = replay_portfolio(replay_prices, picked, calendar, configuration,
                                      start=fold.test_start, end=fold.test_end_exclusive)
            prefix = f"fold-{index:02d}-{mode}"
            for name, frame in (("equity", replay.equity), ("executions", replay.executions), ("decisions", replay.decisions), ("picks", picked)):
                frame.to_parquet(artifacts / f"{prefix}-{name}.parquet", index=False, compression="zstd")
            result["modes"][mode] = dict(metrics=replay.metrics, assumptions=replay.assumptions)
            accumulated[mode].append(replay.metrics)
            # Folds are independent accounts for honest reporting; never compound across open holdings.
        results.append(result)
    if research_code_fingerprint() != code_sha:
        raise RuntimeError("کد پژوهش هنگام اجرا تغییر کرده است؛ نتیجه منتشر نشد")
    summaries = {}
    for mode, metrics in accumulated.items():
        returns = [m["total_return"] for m in metrics if m["total_return"] is not None]
        summaries[mode] = dict(folds=len(metrics), valid_folds=len(returns),
            all_folds_financially_known=len(returns) == len(metrics), mean_of_valid_folds_only=True,
            positive_folds=sum(r > 0 for r in returns), mean_fold_return=float(np.mean(returns)) if returns else None,
            worst_fold_drawdown=max((m["max_drawdown"] for m in metrics if m["max_drawdown"] is not None), default=None),
            folds_with_open_positions=sum(m["open_positions"] > 0 for m in metrics),
            corporate_action_events=sum(m["corporate_action_events"] for m in metrics))
    payload = dict(experiment_id=experiment_id, created_at=now_tehran_iso(), protocol=protocol,
                   results=results, summary=summaries, accepted_for_signals=False,
                   acceptance_reasons=["development periods previously reviewed; independent prospective test needed",
                                       "actual corporate entitlements and intraday order fills unverified",
                                       "folds restart with cash; open holdings are reported, not compounded"],
                   financial_assessment="research_only")
    hashes = {p.name: _sha256(p) for p in artifacts.glob("*.parquet")}
    payload["artifacts_sha256"] = hashes
    report_path = root / f"{experiment_id}.md"
    report_path.write_text(format_portfolio_report(payload), encoding="utf-8")
    (root / f"{experiment_id}.json").write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    logged = db.log_experiment(experiment_id=experiment_id, strategy_name="portfolio-walkforward", symbols_tested="universe",
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(summaries), ensure_ascii=False, allow_nan=False),
        start_date=cfg["dev_start"], end_date=cfg["holdout_start"], dataset_version=source_id,
        features_version="pinned-baseline", model_version=WALKFORWARD_VERSION)
    if logged is None:
        raise RuntimeError("ثبت پژوهش ناموفق بود؛ آخرین نتیجه موفق تغییر نکرد")
    temporary = root / f"latest-portfolio-{uuid.uuid4().hex}.tmp"
    temporary.write_text(json.dumps(dict(experiment_id=experiment_id)), encoding="utf-8")
    os.replace(temporary, root / "latest-portfolio.json")
    payload["report_path"] = str(report_path)
    return payload


def format_portfolio_report(payload):
    lines = [f"# پژوهش سرمایه مشترک و ارزیابی زمانی — {payload['experiment_id']}", "",
             f"زمان: {to_jalali(payload['created_at'], with_time=True)} | فقط دوره توسعه؛ دوره نهایی باز نشده است.", "",
             "در هر برش، انتخاب روش فقط از نتایج معلوم پیش از فاصله زمانی انجام شده است؛ در نبود روش با میانگین مثبت، سرمایه نقد می‌ماند.",
             "حساب هر برش مستقل از برش بعد است. قیمت روزانه جانشین اجرای واقعی سفارش است؛ پرشدن صف ثابت نشده است.", "",
             "| حالت | برش معتبر | برش مثبت | میانگین بازده برش‌های معتبر | بیشترین افت برش معتبر | برش دارای پوزیشن باز |",
             "| --- | ---: | ---: | ---: | ---: | ---: |"]
    labels = dict(selected="انتخاب با گذشته", stress="خرید سقف / فروش کف", random="انتخاب تصادفی", baseline="قدرت خریدار پایه")
    for mode, value in payload["summary"].items():
        def pct(number):
            return "نامعلوم" if number is None else f"{number:+.2%}"
        lines.append(f"| {labels[mode]} | {value['valid_folds']}/{value['folds']} | {value['positive_folds']} | "
                     f"{pct(value['mean_fold_return'])} | {pct(value['worst_fold_drawdown'])} | {value['folds_with_open_positions']} |")
    lines += ["", "این پژوهش مجوز سیگنال نیست. برش‌های گذشته قبلاً دیده شده‌اند؛ سود نقدی/حق‌تقدم واقعی و اجرای صف تأیید نشده‌اند.",
              "افت سبد از ارزش خالص فروش برآوردی به قیمت پایانی محاسبه می‌شود؛ توقف نماد آخرین قیمت قدیمی را حفظ می‌کند.",
              "پس از رسیدن به سقف زیان، خرید جدید متوقف می‌شود؛ امکان فروش دارایی موجود یا محدود ماندن زیان تضمین نمی‌شود.",
              "سرمایه مشترک در هر برش رعایت شده، اما زنجیره CAGR سالانه ساخته نشده است؛ پوزیشن باز در مرز برش پنهان نشده است."]
    lines.append("برش نامعتبر وارد میانگین نمی‌شود و صفر هم فرض نمی‌شود؛ تا تمام برش‌های مقایسه معلوم نباشند، رتبه مالی قطعی اعلام نمی‌شود.")
    return "\n".join(lines) + "\n"
