"""انبار فیچر با اجرای تغییرناپذیر، ورودی بایگانی‌شده و انتشار اتمی اشاره‌گر آخرین اجرا."""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional
from uuid import uuid4

import pandas as pd

from config import settings
from core.text import normalize_symbol
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from features.daily import DAILY_FEATURES, FEATURE_SET_VERSION, compute_daily_features
from features.market import MARKET_FEATURES, RELATIVE_FEATURES, add_relative_features, compute_market_features

logger = logging.getLogger(__name__)
FEATURES_ROOT = settings.DATA_DIR / "features"
ALL_FEATURES: List[str] = DAILY_FEATURES + RELATIVE_FEATURES + MARKET_FEATURES
ProgressFn = Callable[[int, int, str], None]


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("شناسه نسخه یا اجرا نامعتبر است")
    return value


def store_dir(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> Path:
    return (root or FEATURES_ROOT) / _component(version)


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def build_feature_panel(db: DatabaseManager, symbols: Optional[List[str]] = None,
                        progress: Optional[ProgressFn] = None, _inputs=None) -> pd.DataFrame:
    """فقط سهام داخل دامنه و قیمت خام با منشأ مستقیمِ ثبت‌شده از منبع."""
    universe = {s["symbol_fa"]: s for s in db.get_universe_symbols()}
    targets = list(universe) if symbols is None else list(dict.fromkeys(normalize_symbol(s) for s in symbols))
    if any(s not in universe for s in targets):
        raise ValueError("نماد درخواست‌شده در جامعه سهام تأییدشده نیست")
    frames = []
    if _inputs is not None:
        _inputs["universe"] = [universe[s] for s in targets]
    for i, symbol in enumerate(targets, start=1):
        if progress:
            progress(i, len(targets), symbol)
        prices = db.get_daily_prices_df(symbol, verified_only=True)
        if prices.empty:
            continue
        client_types = db.get_client_types_df(symbol)
        if _inputs is not None:
            _inputs["prices"].append(prices.assign(symbol=symbol))
            if not client_types.empty:
                _inputs["client_types"].append(client_types.assign(symbol=symbol))
        feats = compute_daily_features(prices, client_types)
        feats.insert(1, "symbol", symbol)
        feats["value"] = prices["value"].to_numpy()
        if not client_types.empty:
            merged = prices[["date"]].merge(client_types[["date", "buy_i_value", "sell_i_value"]],
                                               on="date", how="left", validate="one_to_one")
            feats["real_net_value"] = (merged["buy_i_value"] - merged["sell_i_value"]).to_numpy()
        else:
            feats["real_net_value"] = float("nan")
        frames.append(feats)
    if not frames:
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES)
    panel = pd.concat(frames, ignore_index=True)
    panel = add_relative_features(panel)
    panel = panel.merge(compute_market_features(panel), on="date", how="left", validate="many_to_one")
    return panel.sort_values(["date", "symbol"]).reset_index(drop=True)[["date", "symbol"] + ALL_FEATURES]


def build_feature_store(db: DatabaseManager, symbols: Optional[List[str]] = None, root: Optional[Path] = None,
                        progress: Optional[ProgressFn] = None) -> dict:
    inputs = {"prices": [], "client_types": []}
    started = now_tehran_iso()
    # همه خواندن‌ها از یک تصویر سازگار SQLite؛ گردآورنده می‌تواند در اتصال دیگر بنویسد.
    db.conn.execute("SAVEPOINT brs_feature_snapshot")
    try:
        panel = build_feature_panel(db, symbols=symbols, progress=progress, _inputs=inputs)
        schema_version = db.schema_version()
    finally:
        db.conn.execute("RELEASE brs_feature_snapshot")
    if panel.empty:
        raise ValueError("داده خام تأییدشده برای ساخت فیچر موجود نیست؛ ابتدا update --full را اجرا کنید")
    target = store_dir(root=root)
    target.mkdir(parents=True, exist_ok=True)
    run_id = uuid4().hex
    staging, final = target / f".tmp-{run_id}", target / run_id
    staging.mkdir()
    try:
        panel.astype({f: "float32" for f in ALL_FEATURES}).to_parquet(staging / "daily.parquet", index=False)
        for key in ("prices", "client_types"):
            if inputs[key]:
                pd.concat(inputs[key], ignore_index=True).to_parquet(staging / f"input_{key}.parquet", index=False)
        (staging / "universe.json").write_text(json.dumps(inputs["universe"], ensure_ascii=False, indent=2), encoding="utf-8")
        hashes = {p.name: _sha(p) for p in staging.iterdir()}
        try:
            commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=settings.BASE_DIR,
                                             text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            commit = None
        code = {str(p.relative_to(settings.BASE_DIR)): _sha(p)
                for folder in ("core", "features", "database", "config")
                for p in sorted((settings.BASE_DIR / folder).rglob("*.py"))}
        manifest = {
            "version": FEATURE_SET_VERSION, "run_id": run_id, "created_at": now_tehran_iso(),
            "input_snapshot_started_at": started, "rows": len(panel), "symbols": int(panel["symbol"].nunique()),
            "date_min": str(panel["date"].min()), "date_max": str(panel["date"].max()), "features": ALL_FEATURES,
            "coverage": {f: round(float(panel[f].notna().mean()), 4) for f in ALL_FEATURES},
            "files_sha256": hashes, "code_sha256": code, "git_commit": commit, "database_schema": schema_version,
            "config": settings.CONFIG, "lock_sha256": _sha(settings.BASE_DIR / "requirements.lock.txt"),
            "availability": "historical_reconstruction; original_historical_availability_not_verified",
            "universe_policy": "current_active_equities; survivorship_bias_unresolved",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(staging, final)
        pointer = target / f".latest-{run_id}.json"
        pointer.write_text(json.dumps({"run_id": run_id}), encoding="utf-8")
        os.replace(pointer, target / "latest.json")
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("انبار فیچر %s، اجرای %s: %d ردیف", FEATURE_SET_VERSION, run_id, len(panel))
    return manifest


def _run_dir(version, root, run_id):
    target = store_dir(version, root)
    if run_id is None:
        pointer = target / "latest.json"
        if pointer.exists():
            run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
    return target / _component(run_id) if run_id else target


def load_manifest(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None,
                  run_id: Optional[str] = None) -> Optional[dict]:
    path = _run_dir(version, root, run_id) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_feature_store(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None,
                       run_id: Optional[str] = None, symbol: Optional[str] = None) -> pd.DataFrame:
    target = _run_dir(version, root, run_id)
    path = target / "daily.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES)
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    expected = manifest.get("files_sha256", {}).get("daily.parquet")
    if expected and _sha(path) != expected:
        raise ValueError("فایل انبار فیچر با اثر انگشت ثبت‌شده مطابقت ندارد")
    return pd.read_parquet(path, filters=[("symbol", "==", normalize_symbol(symbol))] if symbol else None)


def load_symbol_features(symbol: str, version: str = FEATURE_SET_VERSION, root: Optional[Path] = None,
                         run_id: Optional[str] = None) -> pd.DataFrame:
    return load_feature_store(version, root, run_id, symbol).sort_values("date").reset_index(drop=True)
