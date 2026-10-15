# -*- coding: utf-8 -*-
"""
اجرای پژوهش فاز ۱-ج: ساخت برچسب‌ها (نسخه‌دار) ← ارزیابی استراتژی‌های پایه ← ثبت آزمایش و گزارش.

دستور: python brs.py research   (اختیاری: --unseal-holdout برای ارزیابی نهایی بیرون از نمونه)
"""

import hashlib
import json
import logging
import math
import os
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso, to_jalali
from database.db import DatabaseManager
from features.provenance import assert_compatible_inputs
from features.store import _code_fingerprint, _git_commit, load_feature_snapshot
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


def archive_research_code(target: Path) -> dict:
    """Keep the actual dirty-tree research source and locked dependencies, excluding runtime secrets/data."""
    files = [settings.BASE_DIR / "requirements.lock.txt", settings.BASE_DIR / 'pyproject.toml']
    for folder in ("core", "features", "database", "config", "research", "backtest"):
        files.extend(p for p in sorted((settings.BASE_DIR / folder).rglob("*")) if p.suffix in (".py", ".yaml", ".sql"))
    hashes = {}
    for source in files:
        if source.is_file():
            relative = source.relative_to(settings.BASE_DIR)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
            hashes[relative.as_posix()] = _sha256(destination)
    return hashes


def research_code_fingerprint() -> str:
    return _code_fingerprint(("core", "features", "database", "config", "research", "backtest"))


def _deduplicate_input(path: Path, objects: Path) -> str:
    """ورودی یکسان فقط یک‌بار فضا می‌گیرد؛ اگر hard link ممکن نباشد، نسخه مستقل سالم باقی می‌ماند."""
    digest = _sha256(path)
    objects.mkdir(parents=True, exist_ok=True)
    target = objects / f"{digest}.parquet"
    try:
        os.link(path, target)
    except FileExistsError:
        if _sha256(target) != digest:
            raise ValueError("آرشیو ورودی پژوهش خراب شده است؛ hash یکسان نیست")
        temporary = path.with_name(f"{path.name}-{uuid.uuid4().hex}.tmp")
        try:
            os.link(target, temporary)
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
    except OSError:
        # فایل‌سیستم بدون hard link: درستی داده مقدم بر صرفه‌جویی فضاست.
        pass
    return digest


def load_raw_prices(db: DatabaseManager, end_date: Optional[str] = None) -> pd.DataFrame:
    """قیمت خام همه سهم‌های جامعه (فقط ستون‌های Point-in-Time)."""
    ids = [s["id"] for s in db.get_universe_symbols(include_inactive=True)]
    if not ids:
        return pd.DataFrame(columns=["symbol", "date"])
    placeholders = ",".join("?" * len(ids))
    return pd.read_sql_query(
        f"""SELECT s.symbol_fa AS symbol, dp.date, dp.open_raw, dp.low_raw, dp.high_raw, dp.close_raw, dp.close_final_raw,
                   dp.ref_price_raw, dp.volume, dp.value
            FROM daily_prices dp JOIN symbols s ON s.id = dp.symbol_id
            WHERE dp.source = 'tsetmc' AND dp.symbol_id IN ({placeholders})
              AND (? IS NULL OR dp.date < ?);""",
        db.conn, params=ids + [end_date, end_date])


def build_label_store(db: DatabaseManager, root: Optional[Path] = None, end_date: Optional[str] = None,
                      include_holdout: bool = False) -> Dict[str, Any]:
    """برچسب‌ها را می‌سازد و اتمی در data/labels/<نسخه>/labels.parquet ذخیره می‌کند. خروجی: manifest."""
    if not include_holdout:
        seal_date = settings.RESEARCH_CONFIG["holdout_start"]
        end_date = min(end_date, seal_date) if end_date else seal_date
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute("BEGIN")   # تصویر ثابت دیتابیس هنگام خواندن
    try:
        prices = load_raw_prices(db, end_date=end_date)
        schema = db.schema_version()
    finally:
        if own_snapshot:
            db.conn.rollback()
    if prices.empty:
        raise ValueError("داده قیمت برای دوره پژوهش موجود نیست")
    horizons = settings.RESEARCH_CONFIG["horizons"]
    labels = compute_labels(prices, horizons)
    run_id = uuid.uuid4().hex
    target = (root or LABELS_ROOT) / LABEL_SET_VERSION / run_id
    target.mkdir(parents=True, exist_ok=True)
    tmp = target / "labels.parquet.tmp"
    labels.to_parquet(tmp, index=False, compression="zstd")
    prices.to_parquet(target / "prices.parquet", index=False, compression="zstd")
    manifest = {
        "run_id": run_id,
        "path": str(target),
        "exclusive_end_date": end_date,
        "raw_prices_sha256": _sha256(target / "prices.parquet"),
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
                     "rolled to first non-locked day; censored exits stay unknown; synthetic corporate-action share multiplier",
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
    panel = panel.loc[mask].reset_index(drop=True)
    if end and "entry_date" in panel:
        for status in [c for c in panel if c.startswith("label_status_")]:
            suffix = status.removeprefix("label_status_")
            crossing = (panel["entry_date"] >= end) | (panel[f"exit_date_{suffix}"] >= end)
            crossing |= panel[f"planned_exit_date_{suffix}"] >= end
            panel.loc[crossing, status] = "censored"
            for prefix in ("net_ret_", "mfe_", "mae_", "exit_delay_", "corporate_actions_"):
                for column in (f"{prefix}{suffix}", f"{prefix}{suffix}_worst"):
                    if column in panel:
                        panel.loc[crossing, column] = float("nan")
    return panel


def run_research(db: DatabaseManager, unseal_holdout: bool = False, random_trials: Optional[int] = None,
                 log_experiment: bool = True, holdout_strategy: Optional[str] = None,
                 holdout_horizon: Optional[int] = None) -> Dict[str, Any]:
    """کل چرخه پژوهش پایه. خروجی: نتایج + مسیر گزارش."""
    cfg = dict(settings.RESEARCH_CONFIG)
    selected = None
    if unseal_holdout:
        selected = next((s for s in STRATEGIES if s.name == holdout_strategy), None)
        if selected is None or holdout_horizon not in cfg["horizons"]:
            raise ValueError("برای باز کردن مهر آزمون، روش نهایی و افق آن باید از قبل صریح انتخاب شده باشند")
        for experiment in db.get_experiments():
            previous = json.loads(experiment.get("parameters_json") or "{}")
            if experiment["experiment_id"].endswith("-HOLDOUT") or (
                isinstance(previous, dict) and "holdout" in (previous.get("periods") or {})
            ):
                raise ValueError("دوره مهر و موم‌شده در سابقه آزمایش‌ها قبلاً باز شده است")
    trials = cfg["random_trials"] if random_trials is None else random_trials
    if isinstance(trials, bool) or not isinstance(trials, int) or trials < 1:
        raise ValueError("تعداد آزمون تصادفی باید عدد صحیح مثبت باشد")
    code_sha = research_code_fingerprint()
    filters = None if unseal_holdout else [("date", "<", cfg["holdout_start"])]
    features, feature_manifest = load_feature_snapshot(filters=filters)
    if features.empty:
        raise ValueError("انبار فیچر خالی است؛ ابتدا python brs.py features را اجرا کنید")
    RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
    experiment_id = f"EXP-{now_tehran_iso()[:10]}-BASELINES-{uuid.uuid4().hex}"
    seal = RESEARCH_ROOT / "holdout-unsealed.json"
    if unseal_holdout:
        try:
            # ثبت پیش از خواندن نتیجه: اجرای ناموفق هم مجوز نگاه دوباره به آزمون نهایی نمی‌سازد.
            with seal.open("x", encoding="utf-8") as stream:
                json.dump({"experiment_id": experiment_id, "created_at": now_tehran_iso(),
                           "holdout_start": cfg["holdout_start"], "research_config": cfg,
                           "selected_strategy": holdout_strategy, "selected_horizon": holdout_horizon,
                           "code_sha256": code_sha, "feature_manifest": feature_manifest}, stream,
                          ensure_ascii=False, indent=2)
        except FileExistsError as exc:
            raise ValueError("دوره مهر و موم‌شده قبلاً باز شده است؛ نتیجه آن برای انتخاب مدل استفاده نشود") from exc
    cutoff = None if unseal_holdout else cfg["holdout_start"]
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute('BEGIN')
    try:
        assert_compatible_inputs(db, feature_manifest, end_exclusive=cutoff)
        label_manifest = build_label_store(db, end_date=cutoff, include_holdout=unseal_holdout)
    finally:
        if own_snapshot:
            db.conn.rollback()
    label_dir = Path(label_manifest["path"])
    for name in ("labels.parquet", "prices.parquet"):
        _deduplicate_input(label_dir / name, RESEARCH_ROOT / "objects")
    labels = pd.read_parquet(label_dir / "labels.parquet")
    if cutoff:
        features = features.loc[features["date"] < cutoff].copy()
    artifact_dir = RESEARCH_ROOT / experiment_id
    artifact_dir.mkdir()
    source_hashes = archive_research_code(artifact_dir / "code")
    if research_code_fingerprint() != code_sha:
        raise RuntimeError("کد پژوهش هنگام اجرا تغییر کرده است؛ اجرا را با کد ثابت تکرار کنید")
    features.to_parquet(artifact_dir / "features.parquet", index=False, compression="zstd")
    # لینک سخت، نسخه‌های تغییرناپذیر ورودی را بدون کپی دوباره نگه می‌دارد؛ fallback برای فایل‌سیستم دیگر.
    import shutil
    for name in ("labels.parquet", "prices.parquet", "manifest.json"):
        try:
            os.link(label_dir / name, artifact_dir / name)
        except OSError:
            shutil.copyfile(label_dir / name, artifact_dir / name)
    periods = {"development": (cfg["dev_start"], cfg["holdout_start"])}
    if unseal_holdout:
        periods["holdout"] = (cfg["holdout_start"], None)
    results: Dict[str, Any] = {}
    for name, (start, end) in periods.items():
        panel = research_panel(labels, features, start, end, cfg["min_daily_value_rial"])
        results[name] = {
            "start": start, "end": end or str(panel["date"].max()),
            "rows": int(len(panel)), "symbols": int(panel["symbol"].nunique()),
            "horizons": run_baselines(panel, [holdout_horizon] if name == "holdout" else cfg["horizons"],
                                       cfg["top_k"], trials, strategies=[selected] if name == "holdout" else None),
        }
    feature_sha = _deduplicate_input(artifact_dir / "features.parquet", RESEARCH_ROOT / "objects")
    if research_code_fingerprint() != code_sha:
        raise RuntimeError("کد پژوهش هنگام اجرا تغییر کرده است؛ نتیجه منتشر نشد")
    stamp = now_tehran_iso()
    params = {
        "strategies": [s.name for s in STRATEGIES], "top_k": cfg["top_k"], "horizons": cfg["horizons"],
        "min_daily_value_rial": cfg["min_daily_value_rial"], "random_trials": trials,
        "periods": {k: list(v) for k, v in periods.items()},
        "labels": {"version": label_manifest["version"], "sha256": label_manifest["parquet_sha256"]},
        "features": {"version": feature_manifest.get("version"), "sha256": feature_sha,
                     "source_sha256": (feature_manifest.get("provenance") or {}).get("parquet_sha256"),
                     "git_commit": (feature_manifest.get("provenance") or {}).get("git_commit")},
        "research_provenance": {"code_sha256": code_sha, "git_commit": _git_commit(),
                                "config": cfg, "label_manifest": label_manifest,
                                "feature_manifest": feature_manifest, "artifact_dir": str(artifact_dir),
                                "source_files_sha256": source_hashes},
        "portfolio_simulated": False,
        "raw_feature_inputs_verified": True,
        "holdout_selection": {"strategy": holdout_strategy, "horizon": holdout_horizon} if unseal_holdout else None,
    }
    RESEARCH_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {"experiment_id": experiment_id, "created_at": stamp, "params": params, "results": results}
    json_path = RESEARCH_ROOT / f"{experiment_id}.json"
    json_path.write_text(json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    report_path = RESEARCH_ROOT / f"{experiment_id}.md"
    report_path.write_text(format_report(payload), encoding="utf-8")
    latest_tmp = RESEARCH_ROOT / f"latest-{uuid.uuid4().hex}.tmp"
    latest_tmp.write_text(json.dumps({"experiment_id": experiment_id}), encoding="utf-8")
    if log_experiment:
        logged = db.log_experiment(
            experiment_id=experiment_id, strategy_name="rule-baselines", symbols_tested="universe",
            parameters_json=json.dumps(_json_safe(params), ensure_ascii=False, allow_nan=False),
            metrics_json=json.dumps(_json_safe(_compact_metrics(results)), ensure_ascii=False, allow_nan=False),
            start_date=cfg["dev_start"], end_date=cfg["holdout_start"],
            dataset_version=f"{label_manifest['version']}:{label_manifest['parquet_sha256'][:12]}",
            features_version=f"{feature_manifest.get('version')}:{feature_sha[:12]}",
            model_version="rule-baselines-v2",
        )
        if logged is None:
            latest_tmp.unlink(missing_ok=True)
            raise RuntimeError("ثبت آزمایش در دیتابیس ناموفق بود؛ گزارش به عنوان آخرین اجرای موفق منتشر نشد")
    if log_experiment:
        os.replace(latest_tmp, RESEARCH_ROOT / "latest.json")
    else:
        latest_tmp.unlink(missing_ok=True)
    payload["report_path"] = str(report_path)
    return payload


def _json_safe(value):
    """JSON استاندارد: مقدار تعریف‌نشده null است، نه NaN/Infinity غیرمجاز."""
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, np.generic):
        return _json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


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
    manifest = (p.get("research_provenance") or {}).get("label_manifest") or {}
    fees = manifest.get("fees", settings.FEES)
    slip = manifest.get("slippage_bps_each_side", settings.BACKTEST_CONFIG["default_slippage_bps"])
    lines = [
        f"# گزارش پژوهش استراتژی‌های پایه — {payload['experiment_id']}",
        "",
        f"تاریخ اجرا: {to_jalali(payload['created_at'][:10], fa_digits=False)} | "
        f"هر روز {p['top_k']} سهم | حداقل ارزش معاملات روز: {p['min_daily_value_rial'] / 1e10:.0f} میلیارد تومان | "
        f"برچسب {p['labels']['version']} | فیچر {p['features']['version']}",
        "",
        f"بازده‌ها برآورد پژوهشی پس از هزینه‌اند؛ امکان اجرای واقعی تضمین نشده است. خرید فردا و فروش h جلسه بعد؛ "
        f"کارمزد خرید {fees['buy'] * 100:.4f}٪، فروش {fees['sell'] * 100:.4f}٪، لغزش هر طرف {slip / 100:.3f}٪؛ "
        "ورود در صف خرید قفل مدل نمی‌شود و خروج در صف فروش تأخیر دارد.",
        "",
        "ستون‌ها: «میانگین معامله» = سود خالص متوسط هر معامله؛ «با بدترین اجرا» = همان معامله اگر در سقف روز خریده و "
        "در کف روز فروخته شود (آزمون فشار؛ استراتژی واقعاً مقاوم در این ستون هم نباید فرو بریزد)؛ «پر شدن» = نسبت ورود قابل‌مدل‌سازی میان نتیجه‌های معلوم؛ "
        "«برتری» = اختلاف با خرید همه سهم‌های نقدشونده همان روز؛ «t» = آماره اکتشافی برتری (با توجه به "
        "تعداد آزمون‌ها، اثبات معنی‌داری یا سودآوری نیست)؛ «سال‌های مثبت» = پایداری؛ میانگین سبد، تشخیص اولیه است و پرتفوی واقعی شبیه‌سازی نشده؛ رشد سالانه، افت و شارپ اعلام نمی‌شوند.",
    ]
    for period, data in payload["results"].items():
        title = "دوره توسعه (داخل نمونه)" if period == "development" else "دوره بیرون از نمونه (مهر و موم‌شده)"
        lines += ["", f"## {title}: {data['start']} تا {data['end']} — {data['symbols']} نماد"]
        for h, entry in data["horizons"].items():
            m, r = entry["market"], entry["random"]
            lines += [
                "", f"### نگهداری {h} جلسه",
                f"- خرید همه سهم‌های نقدشونده: میانگین معامله {_pct(m['mean_trade'])}، نرخ برد {_pct(m['win_rate'], 0)}",
                f"- انتخاب تصادفی ({r['trials']} بار)، میانگین سبد با سرمایه بیکار: {_pct(r['mean'])} (بازه ۹۰٪: {_pct(r['p05'])} تا {_pct(r['p95'])})",
                "",
                "| استراتژی | میانگین معامله | با بدترین اجرا | نرخ برد | پر شدن | برتری بر بازار | t | سال‌های مثبت | میانگین سبد | انتخاب نامعلوم |",
                "| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
            ]
            for s in sorted(entry["strategies"].values(), key=lambda s: -(s["mean_cohort"] if pd.notna(s["mean_cohort"]) else -9)):
                lines.append(
                    f"| {s['label_fa']} | {_pct(s['mean_trade'])} | {_pct(s.get('mean_trade_worst'))} | "
                    f"{_pct(s['win_rate'], 0)} | {_pct(s['fill_rate'], 0)} | "
                    f"{_pct(s['mean_excess_vs_market'])} | {_num(s['t_excess'])} | {s['years_positive']}/{s['years_total']} | "
                    f"{_pct(s['mean_cohort'])} | {s['unknown_picks']} |")
            action_count = sum(s["corporate_action_trades"] for s in entry["strategies"].values())
            lines.append(f"انتخاب‌های عبورکرده از اقدام شرکتی در روش‌های فوق: {action_count}؛ حسابداری آن‌ها فرضی است.")
    lines += ["", "## محدودیت‌ها",
              "- جامعه فقط نمادهای امروز بازار است؛ اندازه و جهت اثر سوگیری بقا هنوز اندازه‌گیری نشده است.",
              "- سود نقدی و سهام جدید از نسبت قیمت مرجع تفکیک نمی‌شوند؛ تعدیل تعداد سهم یک فرض مصنوعی است، نه حسابداری واقعی.",
              "- خروج نامعلوم با بازده صفر جایگزین نشده؛ تعداد انتخاب نامعلوم گزارش می‌شود و نبود داده می‌تواند مقایسه را سوگیر کند.",
              "- نقد، اندازه سفارش، بازیافت سرمایه، تأخیر فروش و ارزش‌گذاری روزانه در پرتفوی چندنمادی هنوز شبیه‌سازی نشده‌اند.",
              "- اجرا با میانگین قیمت روز فرض شده؛ روزهایی که بخشی از روز در صف بوده‌اند هنوز خوش‌بینانه‌اند.",
              "- این‌ها قاعده‌های ساده‌اند، نه توصیه معاملاتی. نتیجه دوره بیرون از نمونه فقط برای مدل نهایی باز می‌شود."]
    return "\n".join(lines) + "\n"
