# -*- coding: utf-8 -*-
"""
انبار فیچر نسخه‌دار (Feature Store)

فیچرها برای همه نمادها یک‌جا محاسبه و در data/features/<نسخه>/daily.parquet ذخیره می‌شوند، همراه با
manifest.json (فهرست فیچرها، تعداد ردیف، بازه تاریخ، زمان ساخت و «منشأ»: commit گیت، اثر انگشت کد و
وابستگی‌ها، نسخه دیتابیس، کارمزدها و اثر انگشت خود فایل). هر تغییر در منطق فیچرها باید FEATURE_SET_VERSION
را بالا ببرد تا آزمایش‌های قبلی قابل بازتولید بمانند. فایل‌ها اتمی نوشته می‌شوند (اول فایل موقت، سپس جایگزینی)
تا قطع برق یا خطا در میانه ساخت، انبار نیمه‌کاره باقی نگذارد.

چرا Parquet و نه جدول SQLite؟ تعداد فیچرها در پژوهش مدام تغییر می‌کند و ~۶۰۰ نماد × ~۴۰۰۰ روز × ده‌ها
فیچر در قالب ستونی Parquet فشرده و سریع خوانده می‌شود؛ داده خام همچنان فقط در SQLite است.
"""

import hashlib
import json
import logging
import os
import subprocess
import uuid
from pathlib import Path
from typing import Callable, List, Optional

import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from features.daily import DAILY_FEATURES, FEATURE_SET_VERSION, compute_daily_features
from features.market import MARKET_FEATURES, RELATIVE_FEATURES, add_relative_features, compute_market_features

logger = logging.getLogger(__name__)

FEATURES_ROOT = settings.DATA_DIR / "features"
ALL_FEATURES: List[str] = DAILY_FEATURES + RELATIVE_FEATURES + MARKET_FEATURES

ProgressFn = Callable[[int, int, str], None]


def store_dir(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> Path:
    return (root or FEATURES_ROOT) / version


def build_feature_panel(db: DatabaseManager, symbols: Optional[List[str]] = None,
                        progress: Optional[ProgressFn] = None) -> pd.DataFrame:
    """محاسبه فیچرهای روزانه همه نمادهای داخل دامنه + فیچرهای بازار و نسبی."""
    targets = symbols or [s["symbol_fa"] for s in db.get_universe_symbols(include_inactive=True)]
    frames = []
    for i, symbol in enumerate(targets, start=1):
        if progress:
            progress(i, len(targets), symbol)
        prices = db.get_daily_prices_df(symbol)
        if prices.empty:
            continue
        client_types = db.get_client_types_df(symbol)
        feats = compute_daily_features(prices, client_types)
        feats.insert(1, "symbol", symbol)
        # ستون‌های کمکی برای فیچر بازار (بعداً حذف می‌شوند)
        feats["value"] = prices["value"].to_numpy()
        if not client_types.empty:
            merged = prices[["date"]].merge(client_types[["date", "buy_i_value", "sell_i_value"]], on="date", how="left")
            feats["real_net_value"] = (merged["buy_i_value"] - merged["sell_i_value"]).to_numpy()
        else:
            feats["real_net_value"] = float("nan")
        frames.append(feats)
    if not frames:
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES)

    panel = pd.concat(frames, ignore_index=True)
    panel = add_relative_features(panel)
    panel = panel.merge(compute_market_features(panel), on="date", how="left")
    panel = panel.drop(columns=["value", "real_net_value"])
    return panel.sort_values(["date", "symbol"]).reset_index(drop=True)[["date", "symbol"] + ALL_FEATURES]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _code_fingerprint() -> str:
    """اثر انگشت یکجای کدهایی که روی مقدار فیچرها اثر دارند (برای بازتولید دقیق یک آزمایش)."""
    digest = hashlib.sha256()
    for folder in ("core", "features", "database", "config"):
        for path in sorted((settings.BASE_DIR / folder).rglob("*")):
            if path.suffix in (".py", ".yaml", ".sql"):
                digest.update(str(path.relative_to(settings.BASE_DIR).as_posix()).encode("utf-8"))
                digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def _git_commit() -> Optional[str]:
    """commit فعلی گیت؛ اگر تغییر ثبت‌نشده وجود داشته باشد با پسوند -dirty."""
    try:
        run = dict(cwd=settings.BASE_DIR, text=True, stderr=subprocess.DEVNULL, timeout=10)
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], **run).strip()
        dirty = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], **run).strip()
        return commit + ("-dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return None


def _provenance(db: DatabaseManager, parquet: Path) -> dict:
    lock_file = settings.BASE_DIR / "requirements.lock.txt"
    return {
        "git_commit": _git_commit(),
        "code_sha256": _code_fingerprint(),
        "requirements_lock_sha256": _sha256(lock_file) if lock_file.exists() else None,
        "database_schema_version": db.schema_version(),
        "fees": dict(settings.FEES),
        "lock_min_move_pct": settings.BACKTEST_CONFIG["lock_min_move_pct"],
        "parquet_sha256": _sha256(parquet),
        # محدودیت‌های شناخته‌شده که هر پژوهشی روی این داده باید در نظر بگیرد
        "known_limitations": [
            "universe = current active TSE/IFB equities (survivorship bias: delisted symbols missing)",
            "history reconstructed from TSETMC's current archive, not as originally published in real time",
        ],
    }


def build_feature_store(db: DatabaseManager, symbols: Optional[List[str]] = None, root: Optional[Path] = None,
                        progress: Optional[ProgressFn] = None) -> dict:
    """ساخت انبار فیچر و ذخیره اتمی در Parquet + manifest. خروجی: manifest."""
    # همه خواندن‌ها از یک تصویر ثابت دیتابیس (اگر هم‌زمان پایش زنده یا به‌روزرسانی در حال نوشتن باشد)
    own_snapshot = not db.conn.in_transaction
    if own_snapshot:
        db.conn.execute("BEGIN")
    try:
        panel = build_feature_panel(db, symbols=symbols, progress=progress)
    finally:
        if own_snapshot:
            db.conn.rollback()
    target = store_dir(root=root)
    target.mkdir(parents=True, exist_ok=True)
    # float32 برای فیچرها کاملاً کافی است و حجم فایل را نصف می‌کند
    compact = panel.astype({f: "float32" for f in ALL_FEATURES})
    tmp_parquet = target / f"daily-{uuid.uuid4().hex}.parquet.tmp"
    compact.to_parquet(tmp_parquet, index=False, compression="zstd")
    manifest = {
        "version": FEATURE_SET_VERSION,
        "created_at": now_tehran_iso(),
        "rows": int(len(panel)),
        "symbols": int(panel["symbol"].nunique()) if len(panel) else 0,
        "date_min": str(panel["date"].min()) if len(panel) else None,
        "date_max": str(panel["date"].max()) if len(panel) else None,
        "features": ALL_FEATURES,
        "coverage": {f: round(float(panel[f].notna().mean()), 4) for f in ALL_FEATURES} if len(panel) else {},
        "provenance": _provenance(db, tmp_parquet),
    }
    tmp_manifest = target / f"manifest-{uuid.uuid4().hex}.json.tmp"
    tmp_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_parquet, target / "daily.parquet")
    os.replace(tmp_manifest, target / "manifest.json")
    logger.info("انبار فیچر %s ساخته شد: %d ردیف، %d نماد.", FEATURE_SET_VERSION, manifest["rows"], manifest["symbols"])
    return manifest


def load_feature_snapshot(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> tuple:
    """خروجی و manifest یکسان با کنترل hash؛ تغییر هم‌زمان فایل، نتیجه مخلوط تولید نمی‌کند."""
    path = store_dir(version, root) / "daily.parquet"
    manifest = load_manifest(version, root)
    if not path.exists() or not manifest:
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES), {}
    digest = (manifest.get("provenance") or {}).get("parquet_sha256")
    if not digest or _sha256(path) != digest:
        raise ValueError("اثر انگشت انبار فیچر با manifest یکسان نیست؛ ساخت فیچر را دوباره اجرا کنید")
    frame = pd.read_parquet(path)
    if _sha256(path) != digest or load_manifest(version, root) != manifest:
        raise ValueError("انبار فیچر هنگام خواندن تغییر کرد؛ پژوهش را دوباره اجرا کنید")
    return frame, manifest


def load_feature_store(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> pd.DataFrame:
    return load_feature_snapshot(version, root)[0]


def load_manifest(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> Optional[dict]:
    path = store_dir(version, root) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_symbol_features(symbol: str, version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> pd.DataFrame:
    """فیچرهای یک نماد (فقط ردیف‌های همان نماد از فایل خوانده می‌شود)."""
    path = store_dir(version, root) / "daily.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES)
    return pd.read_parquet(path, filters=[("symbol", "==", symbol)]).sort_values("date").reset_index(drop=True)
