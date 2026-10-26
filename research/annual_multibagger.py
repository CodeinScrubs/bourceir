"""Registered one-year multibagger event study; labels are not forecasts or executable returns."""

import argparse
import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from config import settings
from core.timeutils import now_tehran_iso
from ops.research_review import _load_json, _relative_file, _verify_inventory, read_development_parquet
from research.labels import CALENDAR_MIN_SYMBOLS, market_calendar
from research.annual_cohort import VERSION as COHORT_VERSION, first_observed_cohort
from research.rare_events import _panel, compute_rare_events, summarize_rare_events
from research.run import _json_safe, _sha256, archive_research_code, research_code_fingerprint

VERSION = "annual-multibagger-cohort-v2"
HORIZON_SESSIONS = 252
TARGETS = (2.0, 3.0)
TARGET_NAMES = {2.0: "double", 3.0: "triple"}


def _write(path, value):
    path.write_text(json.dumps(_json_safe(value), ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def _inputs(path, digest, cutoff, start, min_symbols, stride):
    prices = read_development_parquet(path, digest, cutoff)
    # Validate source keys before any protocol or archived cohort is registered. A
    # duplicated normalized symbol/date or missing raw column must not leave behind
    # a registered experiment whose outcomes can never be reconstructed.
    # Do not filter invalid numeric bars here: their event paths remain unknown.
    normalized_prices = _panel(prices, {"symbol", "date", "close_final_raw", "ref_price_raw", "volume"})
    if prices.empty or prices.date.min() > start:
        raise ValueError("سابقه خام باید شروع دوره توسعه را پوشش دهد")
    calendar = market_calendar(prices, min_symbols=min_symbols).tolist()
    dates = [day for day in calendar if day >= start][::stride]
    if not dates:
        raise ValueError("هیچ روز تصمیمی در تقویم داده نیست")
    # Do not select only symbols with a bar on each anchor. A previously seen
    # instrument missing its anchor-day price must stay in the denominator as
    # missing_anchor/unknown, rather than disappearing from the sample.
    # First-observation eligibility never uses a future first listing.
    decisions = first_observed_cohort(normalized_prices, dates)
    return prices, calendar, decisions


def compute_annual_multibagger_labels(prices, decisions, calendar, *, observed_through):
    """Return fixed 252-session first-hit labels for 2x and 3x synthetic price paths.

    The underlying event contract deliberately retains unknown paths. It does not infer an
    executable return, economic entitlement, calibrated probability, or investment signal.
    """
    frames = []
    for threshold in TARGETS:
        frame = compute_rare_events(
            prices,
            decisions,
            calendar,
            observed_through=observed_through,
            horizons=(HORIZON_SESSIONS,),
            threshold=threshold,
        )
        frame["target"] = TARGET_NAMES[threshold]
        frame["target"] = frame["target"].astype("string")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def summarize_annual_multibagger_labels(outcomes):
    """Summarize each target separately so different thresholds never share a denominator."""
    required = {"symbol", "date", "horizon_sessions", "threshold", "target", "status"}
    if not required <= set(outcomes.columns) or outcomes.columns.duplicated().any():
        raise ValueError("ستون‌های یکتای برچسب سالانه لازم‌اند")
    if outcomes.empty:
        return []
    if outcomes["threshold"].isna().any() or set(float(value) for value in outcomes["threshold"].unique()) != set(TARGETS):
        raise ValueError("هر دو هدف ثابت دوبرابر و سه‌برابر لازم‌اند")
    if outcomes["horizon_sessions"].ne(HORIZON_SESSIONS).any():
        raise ValueError("افق مطالعه باید دقیقاً ۲۵۲ جلسه باشد")
    if outcomes.duplicated(["symbol", "date", "threshold"]).any():
        raise ValueError("برچسب هدف/نماد/تاریخ تکراری است")

    summaries = []
    for threshold in TARGETS:
        group = outcomes.loc[outcomes["threshold"].eq(threshold)].copy()
        if group.empty or group["target"].isna().any() or set(group["target"].astype(str).unique()) != {TARGET_NAMES[threshold]}:
            raise ValueError("نام هدف با آستانه عددی سازگار نیست")
        group = group.drop(columns=["target"])
        for summary in summarize_rare_events(group):
            summary["target"] = TARGET_NAMES[threshold]
            summaries.append(summary)
    # Both targets must share the same decision cohort; comparing different denominators
    # creates spurious multibagger frequencies even when each target is valid by itself.
    doubles = outcomes.loc[outcomes["threshold"].eq(2.0)].set_index(["symbol", "date"]).sort_index()
    triples = outcomes.loc[outcomes["threshold"].eq(3.0)].set_index(["symbol", "date"]).sort_index()
    if not doubles.index.equals(triples.index):
        raise ValueError("هر فرصت باید دقیقاً برای هر دو هدف دوبرابر و سه‌برابر وجود داشته باشد")

    # A 3x first hit implies that a 2x first hit occurred no later on the same path.
    triple_hit = triples["status"].eq("hit")
    if (triple_hit & ~doubles["status"].eq("hit")).any():
        raise ValueError("رخداد سه‌برابر بدون رخداد دوبرابر در مسیر یکسان ناممکن است")
    first_double = pd.to_numeric(doubles.loc[triple_hit, "first_hit_session"], errors="coerce")
    first_triple = pd.to_numeric(triples.loc[triple_hit, "first_hit_session"], errors="coerce")
    if first_double.isna().any() or first_triple.isna().any() or (first_double > first_triple).any():
        raise ValueError("ترتیب اولین عبور دوبرابر و سه‌برابر ناسازگار است")

    # A 2x and a 3x label must describe the SAME reference-price path. Targets
    # change only the threshold, never the observation coverage, calendar or
    # endpoint multiple. Mismatches can silently distort training base rates.
    shared_path = (
        "observed_through", "nominal_end_date", "valid_prefix_sessions",
        "prefix_reference_changes", "path_problem", "path_problem_date",
        "endpoint_multiple",
    )
    if (doubles["status"].isna().any() or triples["status"].isna().any()
            or any(not doubles[field].equals(triples[field]) for field in shared_path)):
        raise ValueError("دو هدف باید دقیقاً از یک مسیر قیمت و یک تصویر مشاهده باشند")
    double_hit = doubles["status"].eq("hit")
    double_no_hit = doubles["status"].eq("complete_no_hit")
    triple_no_hit = triples["status"].eq("complete_no_hit")
    if (double_no_hit & ~triple_no_hit).any():
        raise ValueError("عدم رخداد قطعی دوبرابر به معنای عدم رخداد قطعی سه‌برابر است")
    if (triple_no_hit & ~(double_hit | double_no_hit)).any():
        raise ValueError("عدم رخداد قطعی سه‌برابر با مسیر نامعلوم دوبرابر ناسازگار است")
    both_unknown = ~(double_hit | double_no_hit) & ~(triple_hit | triple_no_hit)
    if doubles.loc[both_unknown, "status"].ne(triples.loc[both_unknown, "status"]).any():
        raise ValueError("علت نامعلوم بودن دو هدف روی یک مسیر باید یکسان باشد")
    return summaries


def _register(db, identifier, protocol, metrics):
    result = db.log_experiment(
        experiment_id=identifier,
        strategy_name=VERSION,
        symbols_tested="every symbol first observed by each fixed development anchor; historical active membership unverified",
        parameters_json=json.dumps(_json_safe(protocol), ensure_ascii=False, allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics), ensure_ascii=False, allow_nan=False),
        start_date=protocol["development_start"],
        end_date=protocol["holdout_start"],
        dataset_version=protocol["prices_sha256"],
        features_version="none",
        model_version=VERSION,
    )
    if result is None:
        raise RuntimeError("ثبت مطالعه چندبرابری ناموفق بود")


def run_annual_census(
    db,
    *,
    prices_path,
    prices_sha256,
    root,
    anchor_stride=20,
    min_symbols=CALENDAR_MIN_SYMBOLS,
):
    """Register the protocol before outcomes, then archive inputs and a replayable result."""
    if type(anchor_stride) is not int or anchor_stride < 1 or type(min_symbols) is not int or min_symbols < 1:
        raise ValueError("گام تصمیم و حد پوشش تقویم باید صحیح مثبت باشند")
    cfg = settings.RESEARCH_CONFIG
    path, root = Path(prices_path).resolve(), Path(root).resolve()
    prices, calendar, decisions = _inputs(
        path, prices_sha256, cfg["holdout_start"], cfg["dev_start"], min_symbols, anchor_stride
    )
    identifier = f"EXP-{now_tehran_iso()[:10]}-MULTIBAGGER-{uuid.uuid4().hex}"
    folder = root / identifier
    folder.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(path, folder / "prices.parquet")
    if _sha256(folder / "prices.parquet") != prices_sha256 or _sha256(path) != prices_sha256:
        raise ValueError("ورودی خام حین کپی عوض شد")
    decisions.to_parquet(folder / "cohort.parquet", index=False)

    protocol = dict(
        version=VERSION,
        registered_at=now_tehran_iso(),
        development_start=cfg["dev_start"],
        holdout_start=cfg["holdout_start"],
        observed_through=str(prices.date.max()),
        prices_sha256=prices_sha256,
        cohort_sha256=_sha256(folder / "cohort.parquet"),
        source_code=archive_research_code(folder / "code"),
        code_sha256=research_code_fingerprint(),
        anchor_stride=anchor_stride,
        cohort_version=COHORT_VERSION,
        calendar_min_symbols=min_symbols,
        horizon_sessions=HORIZON_SESSIONS,
        target_thresholds=list(TARGETS),
        target_names={str(key): value for key, value in TARGET_NAMES.items()},
        calendar=calendar,
        source_rows=len(prices),
        opportunities=len(decisions),
        cohort="all symbols first observed by or before each fixed nth inferred market session; missing anchor kept unknown; later delistings/membership unverified",
        event="first official-close/reference synthetic index crossing of 2x or 3x within 252 future sessions after decision close",
        endpoint="separate synthetic index at exact 252-session endpoint; no intraday-high or delayed-exit substitution",
        unknowns="missing, invalid, off-calendar and right-censored paths remain unknown unless a first hit was already established",
        limits="no fees, fills, economic entitlement, historical market membership, calibrated probabilities or independence proof",
        calibration_performed=False,
        strategy_trials_added=0,
        target_definitions=len(TARGETS),
        prospective_independence=False,
        holdout_opened=False,
        accepted_for_signals=False,
        profitability_proven=False,
    )
    _write(folder / "protocol.json", protocol)
    _register(db, identifier + "-PROTOCOL", protocol, {})

    outcomes = compute_annual_multibagger_labels(
        prices, decisions, calendar, observed_through=protocol["observed_through"]
    )
    outcomes.to_parquet(folder / "outcomes.parquet", index=False)
    summary = summarize_annual_multibagger_labels(outcomes)
    artifacts = {
        name: _sha256(folder / name)
        for name in ("prices.parquet", "cohort.parquet", "outcomes.parquet")
    }
    if research_code_fingerprint() != protocol["code_sha256"]:
        raise ValueError("کد پژوهش هنگام اجرا تغییر کرد؛ نتیجه پذیرفته نمی‌شود")
    result = dict(
        experiment_id=identifier,
        protocol=protocol,
        summary=summary,
        artifacts_sha256=artifacts,
        completed_at=now_tehran_iso(),
        accepted_for_signals=False,
        profitability_proven=False,
    )
    _register(db, identifier, protocol, summary)
    _write(folder / "result.json", result)
    return result


def review_annual_census(db, folder):
    """Rebuild the cohort, labels and summaries from the archived bytes."""
    folder = Path(folder).resolve()
    result, protocol = _load_json(folder / "result.json"), _load_json(folder / "protocol.json")
    if result["protocol"] != protocol or protocol["version"] != VERSION or folder.name != result["experiment_id"]:
        raise ValueError("پروتکل یا هویت آرشیو ناسازگار است")
    if (
        any(result.get(key) is not False for key in ("accepted_for_signals", "profitability_proven"))
        or any(
            protocol.get(key) is not False
            for key in ("calibration_performed", "prospective_independence", "holdout_opened",
                        "accepted_for_signals", "profitability_proven")
        )
        or type(protocol.get("strategy_trials_added")) is not int
        or protocol["strategy_trials_added"] != 0
        or protocol.get("target_definitions") != len(TARGETS)
    ):
        raise ValueError("ادعای پذیرش یا پیش‌بینی با قرارداد توصیفی ناسازگار است")
    if protocol["horizon_sessions"] != HORIZON_SESSIONS or protocol["target_thresholds"] != list(TARGETS):
        raise ValueError("اهداف ثابت مطالعه تغییر کرده‌اند")
    if protocol.get("cohort_version") != COHORT_VERSION:
        raise ValueError("نسخه ساخت جامعه پیش از تصمیم با کد پژوهش ناسازگار است")
    expected_names = {str(key): value for key, value in TARGET_NAMES.items()}
    if protocol.get("target_names") != expected_names:
        raise ValueError("نام‌گذاری اهداف تغییر کرده است")

    _verify_inventory(folder / "code", protocol["source_code"])
    _verify_inventory(folder, result["artifacts_sha256"], parquet=True)
    for identifier, metrics in ((result["experiment_id"] + "-PROTOCOL", {}), (result["experiment_id"], result["summary"])):
        row = db.conn.execute(
            "SELECT parameters_json,metrics_json FROM experiments WHERE experiment_id=? AND status=?",
            (identifier, "completed"),
        ).fetchone()
        if row is None or json.loads(row["parameters_json"]) != protocol or json.loads(row["metrics_json"]) != metrics:
            raise ValueError("ثبت مستقل با آرشیو مطالعه برابر نیست")
    if result["artifacts_sha256"]["prices.parquet"] != protocol["prices_sha256"]:
        raise ValueError("قیمت محفوظ با ثبت قبلی برابر نیست")

    prices, calendar, cohort = _inputs(
        _relative_file(folder, "prices.parquet"),
        protocol["prices_sha256"],
        protocol["holdout_start"],
        protocol["development_start"],
        protocol["calendar_min_symbols"],
        protocol["anchor_stride"],
    )
    if calendar != protocol["calendar"] or len(prices) != protocol["source_rows"] or len(cohort) != protocol["opportunities"]:
        raise ValueError("تقویم یا مخرج فرصت‌ها عوض شد")
    if str(prices.date.max()) != protocol["observed_through"] or _sha256(folder / "cohort.parquet") != protocol["cohort_sha256"]:
        raise ValueError("زمان مشاهده یا جامعه محفوظ عوض شد")
    # Read archived Parquet with the same hash-bound, date-first holdout guard as raw prices.
    # A direct pd.read_parquet() would load numeric values from a rehashed sealed row
    # before the replay comparison could detect that the archive had been poisoned.
    archived_cohort = read_development_parquet(
        _relative_file(folder, "cohort.parquet"),
        result["artifacts_sha256"]["cohort.parquet"],
        protocol["holdout_start"],
    )
    pd.testing.assert_frame_equal(cohort.reset_index(drop=True), archived_cohort.reset_index(drop=True))

    replay = compute_annual_multibagger_labels(prices, cohort, calendar, observed_through=protocol["observed_through"])
    archived_outcomes = read_development_parquet(
        _relative_file(folder, "outcomes.parquet"),
        result["artifacts_sha256"]["outcomes.parquet"],
        protocol["holdout_start"],
    )
    pd.testing.assert_frame_equal(replay, archived_outcomes)
    summary = summarize_annual_multibagger_labels(replay)
    if _json_safe(summary) != result["summary"]:
        raise ValueError("خلاصه اهداف چندبرابری با بازحساب برابر نیست")
    return dict(
        experiment_id=result["experiment_id"],
        verified_at=now_tehran_iso(),
        outcomes_recomputed=len(replay),
        cohort_reconstructed=len(cohort),
        source_rows=len(prices),
        current_code_matches_capture=research_code_fingerprint() == protocol["code_sha256"],
        accepted_for_signals=False,
        profitability_proven=False,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path)
    parser.add_argument("--sha256")
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--root", type=Path, default=settings.DATA_DIR / "private" / "annual-multibagger-studies")
    parser.add_argument("--review", type=Path)
    args = parser.parse_args(argv)

    from database.db import DatabaseManager

    if args.review:
        with DatabaseManager(args.db, read_only=True) as db:
            output = review_annual_census(db, args.review)
    else:
        if args.prices is None or args.sha256 is None:
            parser.error("prices and sha256 must be explicit")
        if args.db.resolve() == settings.DB_PATH.resolve():
            parser.error("A separate experiment DB is required")
        with DatabaseManager(args.db) as db:
            result = run_annual_census(
                db, prices_path=args.prices, prices_sha256=args.sha256, root=args.root
            )
        output = dict(
            experiment_id=result["experiment_id"],
            summary=result["summary"],
            accepted_for_signals=False,
        )
    print(json.dumps(_json_safe(output), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
