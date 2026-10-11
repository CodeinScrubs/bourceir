# -*- coding: utf-8 -*-
"""
انبار فیچر نسخه‌دار (Feature Store)

فیچرها برای همه نمادها یک‌جا محاسبه و در data/features/<نسخه>/daily.parquet ذخیره می‌شوند، همراه با
manifest.json (فهرست فیچرها، تعداد ردیف، بازه تاریخ، زمان ساخت). هر تغییر در منطق فیچرها باید
FEATURE_SET_VERSION را بالا ببرد تا آزمایش‌های قبلی قابل بازتولید بمانند.

چرا Parquet و نه جدول SQLite؟ تعداد فیچرها در پژوهش مدام تغییر می‌کند و ~۶۰۰ نماد × ~۴۰۰۰ روز × ده‌ها
فیچر در قالب ستونی Parquet فشرده و سریع خوانده می‌شود؛ داده خام همچنان فقط در SQLite است.
"""

import json
import logging
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
    targets = symbols or [s["symbol_fa"] for s in db.get_universe_symbols()]
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


def build_feature_store(db: DatabaseManager, symbols: Optional[List[str]] = None, root: Optional[Path] = None,
                        progress: Optional[ProgressFn] = None) -> dict:
    """ساخت انبار فیچر و ذخیره در Parquet + manifest. خروجی: manifest."""
    panel = build_feature_panel(db, symbols=symbols, progress=progress)
    target = store_dir(root=root)
    target.mkdir(parents=True, exist_ok=True)
    # float32 برای فیچرها کاملاً کافی است و حجم فایل را نصف می‌کند
    compact = panel.astype({f: "float32" for f in ALL_FEATURES})
    compact.to_parquet(target / "daily.parquet", index=False, compression="zstd")
    manifest = {
        "version": FEATURE_SET_VERSION,
        "created_at": now_tehran_iso(),
        "rows": int(len(panel)),
        "symbols": int(panel["symbol"].nunique()) if len(panel) else 0,
        "date_min": str(panel["date"].min()) if len(panel) else None,
        "date_max": str(panel["date"].max()) if len(panel) else None,
        "features": ALL_FEATURES,
        "coverage": {f: round(float(panel[f].notna().mean()), 4) for f in ALL_FEATURES} if len(panel) else {},
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("انبار فیچر %s ساخته شد: %d ردیف، %d نماد.", FEATURE_SET_VERSION, manifest["rows"], manifest["symbols"])
    return manifest


def load_feature_store(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> pd.DataFrame:
    """خواندن انبار فیچر؛ اگر ساخته نشده باشد، دیتافریم خالی."""
    path = store_dir(version, root) / "daily.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES)
    return pd.read_parquet(path)


def load_manifest(version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> Optional[dict]:
    path = store_dir(version, root) / "manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def load_symbol_features(symbol: str, version: str = FEATURE_SET_VERSION, root: Optional[Path] = None) -> pd.DataFrame:
    """فیچرهای یک نماد (فقط ردیف‌های همان نماد از فایل خوانده می‌شود)."""
    path = store_dir(version, root) / "daily.parquet"
    if not path.exists():
        return pd.DataFrame(columns=["date", "symbol"] + ALL_FEATURES)
    return pd.read_parquet(path, filters=[("symbol", "==", symbol)]).sort_values("date").reset_index(drop=True)
