"""شواهد فقط‌خواندنی دوره توسعه برای بررسی ادعاها؛ بدون باز کردن holdout.

خروجی، گزارش ممیزی است؛ آزمایش معاملاتی جدید یا سفارش تولید نمی‌شود.
"""

import argparse
import hashlib
import json
import math
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from config import settings
from core import market_rules
from database.db import DatabaseManager
from research.baselines import STRATEGIES, pick_top
from research.dip import simulate
from research.dip_run import MAIN_RULES, load_prices_until
from research.labels import market_calendar


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def sql_evidence(db):
    cfg = settings.RESEARCH_CONFIG
    limits = (cfg["dev_start"], cfg["holdout_start"])
    daily = dict(db.conn.execute("""
        SELECT COUNT(*) AS rows, MIN(date) AS date_min, MAX(date) AS date_max,
          SUM(ref_price_raw IS NULL OR ref_price_raw <= 0) AS unknown_reference,
          SUM(open_raw IS NULL) AS unknown_open,
          SUM(close_raw IS NULL) AS unknown_last,
          SUM(close_final_raw IS NULL OR close_final_raw <= 0) AS unknown_official_close
        FROM daily_prices WHERE source = 'tsetmc' AND date >= ? AND date < ?;
    """, limits).fetchone())
    flow = dict(db.conn.execute("""
        WITH raw AS (
          SELECT dp.symbol_id, dp.date, dp.value AS turnover,
                 ct.buy_i_value - ct.sell_i_value AS net
          FROM daily_prices dp
          LEFT JOIN client_types_daily ct ON ct.symbol_id = dp.symbol_id AND ct.date = dp.date
          WHERE dp.source = 'tsetmc' AND dp.date < ?
        ), rolling AS (
          SELECT date,
                 COUNT(*) OVER w AS window_days,
                 COUNT(net) OVER w AS known_days,
                 SUM(net) OVER w AS net_sum,
                 SUM(turnover) OVER w AS turnover_sum,
                 SUM(CASE WHEN net IS NOT NULL THEN turnover END) OVER w AS matched_sum
          FROM raw WINDOW w AS (PARTITION BY symbol_id ORDER BY date ROWS 4 PRECEDING)
        )
        SELECT COUNT(*) AS affected_five_session_windows,
               AVG(ABS(net_sum / turnover_sum - net_sum / matched_sum)) AS mean_abs_feature_difference,
               MAX(ABS(net_sum / turnover_sum - net_sum / matched_sum)) AS max_abs_feature_difference
        FROM rolling WHERE date >= ? AND window_days = 5 AND known_days BETWEEN 3 AND 4
             AND turnover_sum > 0 AND matched_sum > 0;
    """, (limits[1], limits[0])).fetchone())
    missing = [dict(row) for row in db.conn.execute("""
        SELECT s.symbol_fa, dp.date, dp.ref_price_raw FROM daily_prices dp
        JOIN symbols s ON s.id = dp.symbol_id
        WHERE dp.source = 'tsetmc' AND dp.date >= ? AND dp.date < ?
          AND (dp.ref_price_raw IS NULL OR dp.ref_price_raw <= 0)
        ORDER BY dp.date, s.symbol_fa LIMIT 12;
    """, limits).fetchall()]
    return {"daily_prices": daily, "flow_windows": flow, "unknown_reference_examples": missing}


def saved_baseline_evidence(root):
    folder = root / "data" / "research"
    latest = json.loads((folder / "latest.json").read_text(encoding="utf-8"))["experiment_id"]
    artifact = folder / latest
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    cutoff = settings.RESEARCH_CONFIG["holdout_start"]
    assert manifest["exclusive_end_date"] <= cutoff
    assert manifest["date_max"] < cutoff
    report = json.loads((folder / f"{latest}.json").read_text(encoding="utf-8"))
    columns = ["date", "value"] + [f"label_status_{h}d" for h in settings.RESEARCH_CONFIG["horizons"]]
    data = pd.read_parquet(artifact / "labels.parquet", columns=columns,
                           filters=[("date", ">=", settings.RESEARCH_CONFIG["dev_start"]),
                                    ("date", "<", cutoff),
                                    ("value", ">=", settings.RESEARCH_CONFIG["min_daily_value_rial"])])
    assert data["date"].max() < cutoff
    result = {"experiment_id": latest, "eligible_rows": len(data), "horizons": {}}
    for h in settings.RESEARCH_CONFIG["horizons"]:
        status = data[f"label_status_{h}d"]
        unknown_per_day = (~status.isin(["complete", "entry_blocked"])).groupby(data["date"]).sum()
        saved = report["results"]["development"]["horizons"][str(h)]["market"]
        result["horizons"][str(h)] = {
            "calendar_days": len(unknown_per_day), "unknown_benchmark_days": int((unknown_per_day > 0).sum()),
            "known_benchmark_days": int((unknown_per_day == 0).sum()),
            "unknown_day_fraction": float((unknown_per_day > 0).mean()),
            "status_counts": status.value_counts().to_dict(), "saved_report_market_days": saved["days"],
            "saved_market_mean_trade": saved["mean_trade"],
        }
    return result


def current_fixed_dip_diagnostic(db, root, rule=None):
    """همان قاعده ثبت‌شده، فقط برای شمارش خطاهای اجرای آن؛ بدون انتخاب پارامتر یا مدل جدید."""
    cfg = settings.RESEARCH_CONFIG
    raw = load_prices_until(db, cfg["holdout_start"])
    assert raw["date"].max() < cfg["holdout_start"]
    feature_path = root / "data" / "features" / "daily-v3" / "daily.parquet"
    feats = pd.read_parquet(feature_path, columns=["date", "symbol", "locked_up", "lock_up_streak"],
                            filters=[("date", ">=", cfg["dev_start"]), ("date", "<", cfg["holdout_start"])])
    assert feats["date"].max() < cfg["holdout_start"]
    liquid = raw.loc[(raw["date"] >= cfg["dev_start"]) & (raw["value"] >= cfg["min_daily_value_rial"]),
                     ["date", "symbol", "value"]]
    panel = feats.merge(liquid, on=["date", "symbol"], validate="one_to_one")
    strategy = next(s for s in STRATEGIES if s.name == "buy_queue")
    picks = pick_top(panel, strategy.score(panel), cfg["top_k"])[["date", "symbol"]]
    calendar = market_calendar(raw)
    rule = MAIN_RULES[0] if rule is None else rule
    started = time.monotonic()
    outputs = simulate(raw, calendar, rule, decisions=picks)
    outputs = outputs.loc[outputs["dip_status"] != "skipped"].copy()
    done = outputs.loc[outputs["dip_status"] == "complete"].copy()
    entry_positions = np.searchsorted(calendar, done["date"].to_numpy(), side="right")
    exit_positions = entry_positions + done["days_held"].to_numpy(dtype=int)
    done["entry_date"] = calendar[entry_positions]
    done["exit_date"] = calendar[exit_positions]
    flags = []
    for symbol, rows in raw.sort_values(["symbol", "date"]).groupby("symbol", sort=False):
        item = rows[["date", "symbol"]].copy()
        item["locked_down"] = market_rules.lock_flags(rows)["locked_down"].to_numpy()
        flags.append(item)
    exit_flags = pd.concat(flags, ignore_index=True).rename(columns={"date": "exit_date"})
    done = done.merge(exit_flags, on=["symbol", "exit_date"], validate="many_to_one")
    invalid_ref_holds = []
    raw_groups = {symbol: rows.sort_values("date") for symbol, rows in raw.groupby("symbol", sort=False)}
    for symbol, trades in done.groupby("symbol", sort=False):
        rows = raw_groups[symbol]
        unknown = rows.loc[rows["ref_price_raw"].isna() | (rows["ref_price_raw"] <= 0), "date"].tolist()
        for day in unknown:
            for record in trades.loc[(trades["entry_date"] < day) & (trades["exit_date"] >= day)].to_dict("records"):
                invalid_ref_holds.append({"symbol": symbol, "decision_date": record["date"],
                                          "entry_date": record["entry_date"], "exit_date": record["exit_date"],
                                          "unknown_reference_date": day})
    entry_pos = np.searchsorted(calendar, picks["date"].to_numpy(), side="right")
    with_next = entry_pos < len(calendar)
    entry_picks = picks.loc[with_next].copy()
    entry_picks["entry_date"] = calendar[entry_pos[with_next]]
    entry_prices = raw[["date", "symbol", "low_raw", "ref_price_raw"]].rename(columns={"date": "entry_date"})
    candidates = entry_picks.merge(entry_prices, on=["symbol", "entry_date"], how="left", validate="many_to_one")
    reached = candidates["low_raw"] <= candidates["ref_price_raw"] * (1 - rule.dip)
    result = {
        "rule": rule.__dict__, "top_k": cfg["top_k"], "min_daily_value_rial": cfg["min_daily_value_rial"],
        "prices_exclusive_end": cfg["holdout_start"], "picks": len(picks),
        "symbol_days_reaching_dip_level": int(reached.sum()),
        "status_counts": outputs["dip_status"].value_counts().to_dict(),
        "completed_trades": len(done), "mean_trade_current_model": float(done["net_ret"].mean()),
        "completed_exits_on_locked_down_days": int(done["locked_down"].sum()),
        "locked_down_exit_examples": done.loc[done["locked_down"],
                                               ["symbol", "date", "entry_date", "exit_date", "exit_kind", "net_ret"]]
        .head(8).to_dict("records"),
        "holds_with_missing_reference": invalid_ref_holds,
        "seconds": round(time.monotonic() - started, 3),
        "limitations": "Current inputs, not a pinned original DIP input; no parameter search; no corrected P&L claim.",
    }
    return result


def network_evidence(db):
    from data_collector.tsetmc import BASE_URL, TsetmcProvider

    ins = "46348559193224090"
    days = ["2016-06-14", "2019-09-01", "2024-10-01"]
    provider = TsetmcProvider(session=requests.Session())
    outcomes = []
    for day in days:
        assert day < settings.RESEARCH_CONFIG["holdout_start"]
        url = BASE_URL + f"/Trade/GetTradeHistory/{ins}/{day.replace('-', '')}/false"
        started = time.monotonic()
        data = provider._get_json(url, timeout=12)
        result = {"date": day, "url": url, "seconds": round(time.monotonic() - started, 3),
                  "response_received": data is not None}
        if isinstance(data, dict):
            rows = data.get("tradeHistory", [])
            active = [r for r in rows if r.get("canceled") == 0]
            result.update(rows=len(rows), uncanceled_rows=len(active),
                          cancellation_flags=sorted({r.get("canceled") for r in rows}),
                          uncanceled_volume=sum(r["qTitTran"] for r in active),
                          min_time=min((r["hEven"] for r in active), default=None),
                          max_time=max((r["hEven"] for r in active), default=None))
            row = db.conn.execute("""
                SELECT dp.volume, dp.trade_count, dp.low_raw, dp.high_raw FROM daily_prices dp
                JOIN symbols s ON dp.symbol_id = s.id
                WHERE s.ins_code = ? AND dp.date = ? AND dp.source = 'tsetmc';
            """, (ins, day)).fetchone()
            result["daily_archive"] = dict(row) if row else None
        outcomes.append(result)
    return outcomes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--network", action="store_true")
    parser.add_argument("--attachment", action="append", default=[])
    args = parser.parse_args()
    root = settings.BASE_DIR
    db_path = root / "data" / "brs.db"
    report = {"cutoff": settings.RESEARCH_CONFIG["holdout_start"], "database_mode": "ro"}
    with DatabaseManager(db_path=db_path, read_only=True) as db:
        db.conn.execute("PRAGMA query_only=ON")
        report["sql"] = sql_evidence(db)
        print("Read-only development-period SQL checks finished.", flush=True)
        report["saved_baseline"] = saved_baseline_evidence(root)
        print("Saved development baseline coverage checked.", flush=True)
        report["fixed_dip_diagnostic"] = current_fixed_dip_diagnostic(db, root)
        print("Existing fixed DIP rule inspected without optimizing or unsealing.", flush=True)
        if args.network:
            report["historical_tick_api"] = network_evidence(db)
    report["attachment_sha256"] = {f"attachment_{i}": sha256(path)
                                   for i, path in enumerate(args.attachment, start=1)}
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(report), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    print(f"Evidence saved: {path}", flush=True)


if __name__ == "__main__":
    main()
