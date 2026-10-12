# -*- coding: utf-8 -*-
"""
اجرای پژوهش فاز ۱-ج: ساخت برچسب‌ها (نسخه‌دار) ← ارزیابی استراتژی‌های پایه ← ثبت آزمایش و گزارش.

دستور: python brs.py research   (اختیاری: --unseal-holdout برای ارزیابی نهایی بیرون از نمونه)
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso, to_jalali
from database.db import DatabaseManager
from features.store import load_feature_store, load_manifest
from research.baselines import STRATEGIES, run_baselines
from research.labels import LABEL_SET_VERSION, compute_labels

logger = logging.getLogger(__name__)

LABELS_ROOT = settings.DATA_DIR / "labels"
RESEARCH_ROOT = settings.DATA_DIR / "research"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def load_raw_prices(db: DatabaseManager) -> pd.DataFrame:
    """قیمت خام همه سهم‌های جامعه (فقط ستون‌های Point-in-Time)."""
    ids = [s["id"] for s in db.get_universe_symbols()]
    if not ids:
        return pd.DataFrame(columns=["symbol", "date"])
    placeholders = ",".join("?" * len(ids))
    return pd.read_sql_query(
        f"""SELECT s.symbol_fa AS symbol, dp.date, dp.low_raw, dp.high_raw, dp.close_raw, dp.close_final_raw,
                   dp.ref_price_raw, dp.volume, dp.value
            FROM daily_prices dp JOIN symbols s ON s.id = dp.symbol_id
            WHERE dp.source = 'tsetmc' AND dp.symbol_id IN ({placeholders});""",
        db.conn, params=ids)


def build_label_store(db: DatabaseManager, root: Optional[Path] = None) -> Dict[str, Any]:
    """برچسب‌ها را می‌سازد و اتمی در data/labels/<نسخه>/labels.parquet ذخیره می‌کند. خروجی: manifest."""
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute("BEGIN")   # تصویر ثابت دیتابیس هنگام خواندن
    try:
        prices = load_raw_prices(db)
        schema = db.schema_version()
    finally:
        if own_snapshot:
            db.conn.rollback()
    horizons = settings.RESEARCH_CONFIG["horizons"]
    labels = compute_labels(prices, horizons)
    target = (root or LABELS_ROOT) / LABEL_SET_VERSION
    target.mkdir(parents=True, exist_ok=True)
    tmp = target / "labels.parquet.tmp"
    labels.to_parquet(tmp, index=False, compression="zstd")
    manifest = {
        "version": LABEL_SET_VERSION,
        "created_at": now_tehran_iso(),
        "rows": int(len(labels)),
        "symbols": int(labels["symbol"].nunique()) if len(labels) else 0,
        "date_min": str(labels["date"].min()) if len(labels) else None,
        "date_max": str(labels["date"].max()) if len(labels) else None,
        "horizons": horizons,
        "fees": dict(settings.FEES),
        "slippage_bps_each_side": settings.BACKTEST_CONFIG["default_slippage_bps"],
        "lock_min_move_pct": settings.BACKTEST_CONFIG["lock_min_move_pct"],
        "execution": "entry next market session VWAP+slippage; exit h sessions later VWAP-slippage, "
                     "rolled to first non-locked day; no entry on buy-queue/halt days; corporate actions value-preserving",
        "database_schema_version": schema,
        "parquet_sha256": _sha256(tmp),
    }
    tmp_manifest = target / "manifest.json.tmp"
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, target / "labels.parquet")
    os.replace(tmp_manifest, target / "manifest.json")
    return manifest


def research_panel(labels: pd.DataFrame, features: pd.DataFrame, start: str, end: Optional[str],
                   min_value: float) -> pd.DataFrame:
    """فیچرهای روز t + برچسب‌های بعد از t، فقط سهم‌های نقدشونده و فقط دوره خواسته‌شده."""
    panel = features.merge(labels, on=["date", "symbol"], how="inner", validate="one_to_one")
    mask = (panel["date"] >= start) & (panel["value"] >= min_value)
    if end:
        mask &= panel["date"] < end
    return panel.loc[mask].reset_index(drop=True)


def run_research(db: DatabaseManager, unseal_holdout: bool = False, random_trials: Optional[int] = None,
                 log_experiment: bool = True) -> Dict[str, Any]:
    """کل چرخه پژوهش پایه. خروجی: نتایج + مسیر گزارش."""
    cfg = settings.RESEARCH_CONFIG
    label_manifest = build_label_store(db)
    labels = pd.read_parquet(LABELS_ROOT / LABEL_SET_VERSION / "labels.parquet")
    features = load_feature_store()
    feature_manifest = load_manifest() or {}
    if features.empty:
        raise ValueError("انبار فیچر خالی است؛ ابتدا python brs.py features را اجرا کنید")
    trials = cfg["random_trials"] if random_trials is None else random_trials
    periods = {"development": (cfg["dev_start"], cfg["holdout_start"])}
    if unseal_holdout:
        periods["holdout"] = (cfg["holdout_start"], None)
    results: Dict[str, Any] = {}
    for name, (start, end) in periods.items():
        panel = research_panel(labels, features, start, end, cfg["min_daily_value_rial"])
        results[name] = {
            "start": start, "end": end or str(panel["date"].max()),
            "rows": int(len(panel)), "symbols": int(panel["symbol"].nunique()),
            "horizons": run_baselines(panel, cfg["horizons"], cfg["top_k"], trials),
        }
    feature_sha = (feature_manifest.get("provenance") or {}).get("parquet_sha256", "")
    stamp = now_tehran_iso()
    experiment_id = f"EXP-{stamp[:10]}-BASELINES-{label_manifest['parquet_sha256'][:8]}-{feature_sha[:8]}"
    if unseal_holdout:
        experiment_id += "-HOLDOUT"
    params = {
        "strategies": [s.name for s in STRATEGIES], "top_k": cfg["top_k"], "horizons": cfg["horizons"],
        "min_daily_value_rial": cfg["min_daily_value_rial"], "random_trials": trials,
        "periods": {k: list(v) for k, v in periods.items()},
        "labels": {"version": label_manifest["version"], "sha256": label_manifest["parquet_sha256"]},
        "features": {"version": feature_manifest.get("version"), "sha256": feature_sha,
                     "git_commit": (feature_manifest.get("provenance") or {}).get("git_commit")},
    }
    RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {"experiment_id": experiment_id, "created_at": stamp, "params": params, "results": results}
    json_path = RESEARCH_ROOT / f"{experiment_id}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = RESEARCH_ROOT / f"{experiment_id}.md"
    report_path.write_text(format_report(payload), encoding="utf-8")
    (RESEARCH_ROOT / "latest.json").write_text(json.dumps({"experiment_id": experiment_id}), encoding="utf-8")
    if log_experiment:
        db.log_experiment(
            experiment_id=experiment_id, strategy_name="rule-baselines", symbols_tested="universe",
            parameters_json=json.dumps(params, ensure_ascii=False),
            metrics_json=json.dumps(_compact_metrics(results), ensure_ascii=False),
            start_date=cfg["dev_start"], end_date=cfg["holdout_start"],
            dataset_version=f"{label_manifest['version']}:{label_manifest['parquet_sha256'][:12]}",
            features_version=f"{feature_manifest.get('version')}:{feature_sha[:12]}",
            model_version="rule-baselines-v1",
        )
    payload["report_path"] = str(report_path)
    return payload


def _compact_metrics(results: Dict[str, Any]) -> Dict[str, Any]:
    compact = {}
    for period, data in results.items():
        for h, entry in data["horizons"].items():
            compact[f"{period}:{h}d:market_mean"] = round(entry["market"]["mean_trade"], 5)
            compact[f"{period}:{h}d:random_p95"] = round(entry["random"]["p95"], 5)
            for name, s in entry["strategies"].items():
                compact[f"{period}:{h}d:{name}"] = {
                    "mean_trade": round(s["mean_trade"], 5), "t_excess": round(s["t_excess"], 2),
                    "fill_rate": round(s["fill_rate"], 3),
                }
    return compact


def _pct(x: Any, digits: int = 2) -> str:
    return "—" if x is None or pd.isna(x) else f"{x * 100:+.{digits}f}٪"


def _num(x: Any, digits: int = 2) -> str:
    return "—" if x is None or pd.isna(x) else f"{x:.{digits}f}"


def format_report(payload: Dict[str, Any]) -> str:
    """گزارش فارسی قابل خواندن برای کاربر (و هر هوش مصنوعی دیگر)."""
    p = payload["params"]
    lines = [
        f"# گزارش پژوهش استراتژی‌های پایه — {payload['experiment_id']}",
        "",
        f"تاریخ اجرا: {to_jalali(payload['created_at'][:10], fa_digits=False)} | "
        f"هر روز {p['top_k']} سهم | حداقل ارزش معاملات روز: {p['min_daily_value_rial'] / 1e10:.0f} میلیارد تومان | "
        f"برچسب {p['labels']['version']} | فیچر {p['features']['version']}",
        "",
        "همه بازده‌ها **خالص** هستند: خرید فردا به میانگین قیمت روز، فروش h جلسه بعد، کارمزد خرید ۰.۳۷۱۲٪ و فروش "
        "۰.۸۸٪، لغزش ۰.۱٪ در هر طرف، بدون خرید در صف خرید قفل و با گیر افتادن در صف فروش.",
        "",
        "ستون‌ها: «میانگین معامله» = سود خالص متوسط هر معامله؛ «با بدترین اجرا» = همان معامله اگر در سقف روز خریده و "
        "در کف روز فروخته شود (آزمون فشار؛ استراتژی واقعاً مقاوم در این ستون هم نباید فرو بریزد)؛ «پر شدن» = درصد انتخاب‌هایی که واقعاً قابل خرید بودند؛ "
        "«برتری» = اختلاف با خرید همه سهم‌های نقدشونده همان روز؛ «t» = معنی‌داری آماری برتری (|t| بزرگ‌تر از ۳ با توجه به "
        "تعداد زیاد آزمون‌ها قابل توجه است)؛ «سال‌های مثبت» = پایداری؛ «رشد سالانه» و «افت» = پرتفوی واقعی بدون هم‌پوشانی.",
    ]
    for period, data in payload["results"].items():
        title = "دوره توسعه (داخل نمونه)" if period == "development" else "دوره بیرون از نمونه (مهر و موم‌شده)"
        lines += ["", f"## {title}: {data['start']} تا {data['end']} — {data['symbols']} نماد"]
        for h, entry in data["horizons"].items():
            m, r = entry["market"], entry["random"]
            lines += [
                "", f"### نگهداری {h} جلسه",
                f"- خرید همه سهم‌های نقدشونده: میانگین معامله {_pct(m['mean_trade'])}، نرخ برد {_pct(m['win_rate'], 0)}",
                f"- انتخاب تصادفی ({r['trials']} بار): میانگین {_pct(r['mean'])} (بازه ۹۰٪: {_pct(r['p05'])} تا {_pct(r['p95'])})",
                "",
                "| استراتژی | میانگین معامله | با بدترین اجرا | نرخ برد | پر شدن | برتری بر بازار | t | سال‌های مثبت | رشد سالانه | بیشترین افت |",
                "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
            for s in sorted(entry["strategies"].values(), key=lambda s: -(s["mean_trade"] if pd.notna(s["mean_trade"]) else -9)):
                lines.append(
                    f"| {s['label_fa']} | {_pct(s['mean_trade'])} | {_pct(s.get('mean_trade_worst'))} | "
                    f"{_pct(s['win_rate'], 0)} | {_pct(s['fill_rate'], 0)} | "
                    f"{_pct(s['mean_excess_vs_market'])} | {_num(s['t_excess'])} | {s['years_positive']}/{s['years_total']} | "
                    f"{_pct(s['cagr'], 1)} | {_pct(s['max_drawdown'], 0)} |")
    lines += ["", "## محدودیت‌ها",
              "- جامعه فقط نمادهای امروز بازار است (سوگیری بقا: نتایج کمی خوش‌بینانه‌اند).",
              "- اجرا با میانگین قیمت روز فرض شده؛ روزهایی که بخشی از روز در صف بوده‌اند هنوز خوش‌بینانه‌اند.",
              "- این‌ها قاعده‌های ساده‌اند، نه توصیه معاملاتی. نتیجه دوره بیرون از نمونه فقط برای مدل نهایی باز می‌شود."]
    return "\n".join(lines) + "\n"
