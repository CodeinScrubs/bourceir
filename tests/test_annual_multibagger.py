import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from research.annual_multibagger import (
    HORIZON_SESSIONS,
    compute_annual_multibagger_labels,
    review_annual_census,
    run_annual_census,
    summarize_annual_multibagger_labels,
)
from research.run import _sha256


def synthetic_prices(days, final_close=200.0):
    closes = [100.0] * len(days)
    closes[-1] = final_close
    return pd.DataFrame(
        dict(symbol="آزمون", date=days, close_final_raw=closes, ref_price_raw=[100.0] * len(days), volume=100)
    )


def test_annual_labels_distinguish_double_and_triple_targets():
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days, final_close=200.0)
    decisions = prices.iloc[:1][["symbol", "date"]]

    result = compute_annual_multibagger_labels(
        prices, decisions, days, observed_through=days[-1]
    ).set_index("target")

    assert result.loc["double", "status"] == "hit"
    assert result.loc["double", "first_hit_session"] == HORIZON_SESSIONS
    assert result.loc["double", "endpoint_multiple"] == pytest.approx(2.0)
    assert result.loc["triple", "status"] == "complete_no_hit"
    assert result.loc["triple", "outcome"] == False
    assert result.loc["triple", "endpoint_multiple"] == pytest.approx(2.0)


def test_missing_session_remains_unknown_for_both_targets():
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days, final_close=300.0).drop(index=100)
    decisions = pd.DataFrame([{"symbol": "آزمون", "date": days[0]}])

    result = compute_annual_multibagger_labels(
        prices, decisions, days, observed_through=days[-1]
    )
    assert result["status"].tolist() == ["missing_observation", "missing_observation"]
    assert result["outcome"].isna().all()

    summary = summarize_annual_multibagger_labels(result)
    assert [item["target"] for item in summary] == ["double", "triple"]
    assert all(item["opportunities"] == 1 and item["unknown"] == 1 for item in summary)
    assert all(item["observed_hit_fraction_lower"] == 0 for item in summary)
    assert all(item["possible_hit_fraction_upper"] == 1 for item in summary)


def test_summary_rejects_mismatched_target_names():
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days)
    result = compute_annual_multibagger_labels(
        prices, prices.iloc[:1][["symbol", "date"]], days, observed_through=days[-1]
    )
    result.loc[result["target"].eq("triple"), "target"] = "double"
    with pytest.raises(ValueError, match="نام هدف"):
        summarize_annual_multibagger_labels(result)


@pytest.mark.parametrize(
    "corruption,expected",
    [
        ("missing_partner", "هر فرصت"),
        ("impossible_triple", "بدون رخداد دوبرابر"),
        ("reverse_first_hit", "ترتیب اولین عبور"),
        ("partial_missing_name", "نام هدف"),
    ],
)
def test_summary_refuses_incomparable_target_cohorts(corruption, expected):
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days, final_close=300.0)
    decisions = prices.iloc[[0, 1]][["symbol", "date"]]
    rows = compute_annual_multibagger_labels(prices, decisions, days, observed_through=days[-1])
    first = rows["date"].eq(days[0])
    second = rows["date"].eq(days[1])
    is_double = rows["threshold"].eq(2.0)
    is_triple = rows["threshold"].eq(3.0)

    # A correct result includes both targets for both anchors, regardless of censoring.
    assert [s["opportunities"] for s in summarize_annual_multibagger_labels(rows)] == [2, 2]

    if corruption == "missing_partner":
        rows = rows.loc[~(second & is_triple)]
    elif corruption == "impossible_triple":
        rows.loc[first & is_double, "status"] = "complete_no_hit"
    elif corruption == "reverse_first_hit":
        rows.loc[first & is_triple, "first_hit_session"] = 1.0
    else:
        rows.loc[second & is_triple, "target"] = pd.NA

    with pytest.raises(ValueError, match=expected):
        summarize_annual_multibagger_labels(rows)


def runner_input(tmp_path, monkeypatch):
    monkeypatch.setitem(settings.RESEARCH_CONFIG, "dev_start", "2024-01-01")
    days = pd.date_range("2024-01-01", periods=65).strftime("%Y-%m-%d").tolist()
    values = [100.0, *[200.0] * 64]
    prices = pd.DataFrame(
        dict(symbol="آزمون", date=days, close_final_raw=values,
             ref_price_raw=[100.0, *values[:-1]], volume=100)
    )
    path = tmp_path / "input.parquet"
    prices.to_parquet(path, index=False)
    return path



@pytest.mark.parametrize(
    "corruption",
    ["duplicate_key", "normalized_duplicate_key", "missing_raw_column", "blank_symbol"],
)
def test_invalid_source_is_refused_before_protocol_registration(db, tmp_path, monkeypatch, corruption):
    """Invalid dataset structure must not leave an unreplayable experiment in the registry."""
    path = runner_input(tmp_path, monkeypatch)
    prices = pd.read_parquet(path)
    if corruption == "duplicate_key":
        prices = pd.concat([prices, prices.iloc[[0]]], ignore_index=True)
    elif corruption == "normalized_duplicate_key":
        prices.loc[0, "symbol"] = "كيان"
        variant = prices.iloc[[0]].copy()
        variant["symbol"] = "کیان"
        prices = pd.concat([prices, variant], ignore_index=True)
    elif corruption == "missing_raw_column":
        prices = prices.drop(columns="ref_price_raw")
    else:
        prices.loc[0, "symbol"] = "   "
    prices.to_parquet(path, index=False)

    monkeypatch.setattr(
        db,
        "log_experiment",
        lambda **kwargs: pytest.fail("invalid source was registered before source validation"),
    )
    root = tmp_path / "runs"
    with pytest.raises(ValueError):
        run_annual_census(
            db, prices_path=path, prices_sha256=_sha256(path), root=root, min_symbols=1
        )
    assert not root.exists()

def test_registered_cohort_retains_missing_anchor_after_first_observation(db, tmp_path, monkeypatch):
    """An unobserved decision-day bar cannot erase a previously seen instrument."""
    path = runner_input(tmp_path, monkeypatch)
    prices = pd.read_parquet(path)
    control = prices.copy()
    control["symbol"] = "کنترل"
    missing_anchor = "2024-01-21"  # the second fixed 20-session decision
    prices = pd.concat([prices, control], ignore_index=True)
    prices = prices.loc[
        ~((prices.symbol == "آزمون") & (prices.date == missing_anchor))
    ].reset_index(drop=True)
    prices.to_parquet(path, index=False)

    result = run_annual_census(
        db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path / "runs", min_symbols=1
    )
    folder = tmp_path / "runs" / result["experiment_id"]
    cohort = pd.read_parquet(folder / "cohort.parquet")
    outcomes = pd.read_parquet(folder / "outcomes.parquet")
    missing = outcomes.loc[
        outcomes.symbol.eq("آزمون") & outcomes.date.eq(missing_anchor)
    ]

    assert result["protocol"]["cohort_version"] == "first-observed-anchor-cohort-v1"
    assert len(cohort) == 8  # two previously observed symbols across four anchors
    assert len(missing) == 2
    assert missing.status.eq("missing_anchor").all()
    assert missing.outcome.isna().all()
    assert [row["opportunities"] for row in result["summary"]] == [8, 8]
    assert review_annual_census(db, folder)["cohort_reconstructed"] == 8


def test_study_registers_before_labels_and_reconstructs_from_archived_bytes(db, tmp_path, monkeypatch):
    path = runner_input(tmp_path, monkeypatch)
    result = run_annual_census(
        db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path / "runs", min_symbols=1
    )
    folder = tmp_path / "runs" / result["experiment_id"]
    review = review_annual_census(db, folder)

    assert review["outcomes_recomputed"] == 8
    assert review["cohort_reconstructed"] == 4
    assert review["current_code_matches_capture"]
    assert not review["accepted_for_signals"] and not review["profitability_proven"]
    assert result["protocol"]["strategy_trials_added"] == 0
    assert [item["target"] for item in result["summary"]] == ["double", "triple"]
    assert [item["opportunities"] for item in result["summary"]] == [4, 4]
    assert [item["unknown"] for item in result["summary"]] == [3, 4]


def test_registration_failure_stops_before_outcome_computation(db, tmp_path, monkeypatch):
    path = runner_input(tmp_path, monkeypatch)
    monkeypatch.setattr(db, "log_experiment", lambda **kwargs: None)

    import research.annual_multibagger as module

    monkeypatch.setattr(
        module, "compute_annual_multibagger_labels",
        lambda *args, **kwargs: pytest.fail("labels must not run before protocol registration"),
    )
    with pytest.raises(RuntimeError, match="ثبت مطالعه"):
        run_annual_census(
            db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path / "runs", min_symbols=1
        )
    assert not list((tmp_path / "runs").rglob("outcomes.parquet"))


def test_tampered_outcomes_are_rejected_even_after_rehash(db, tmp_path, monkeypatch):
    path = runner_input(tmp_path, monkeypatch)
    result = run_annual_census(
        db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path / "runs", min_symbols=1
    )
    folder = tmp_path / "runs" / result["experiment_id"]
    outcomes = pd.read_parquet(folder / "outcomes.parquet")
    outcomes.loc[0, "first_hit_session"] = 8
    outcomes.to_parquet(folder / "outcomes.parquet", index=False)
    result["artifacts_sha256"]["outcomes.parquet"] = _sha256(folder / "outcomes.parquet")
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")

    with pytest.raises(AssertionError):
        review_annual_census(db, folder)


def test_holdout_input_is_refused_before_registration(db, tmp_path, monkeypatch):
    path = runner_input(tmp_path, monkeypatch)
    prices = pd.read_parquet(path)
    prices.loc[len(prices) - 1, "date"] = settings.RESEARCH_CONFIG["holdout_start"]
    prices.to_parquet(path, index=False)
    monkeypatch.setattr(db, "log_experiment", lambda **kwargs: pytest.fail("sealed data must fail before registration"))

    with pytest.raises(ValueError):
        run_annual_census(
            db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path / "runs", min_symbols=1
        )


def test_review_refuses_rehashed_holdout_outcome_before_reading_values(db, tmp_path, monkeypatch):
    """Reject a poisoned output date before opening its future-derived numeric columns."""
    path = runner_input(tmp_path, monkeypatch)
    result = run_annual_census(
        db, prices_path=path, prices_sha256=_sha256(path), root=tmp_path / "runs", min_symbols=1
    )
    folder = tmp_path / "runs" / result["experiment_id"]
    target = folder / "outcomes.parquet"

    original_read = pd.read_parquet
    outcomes = original_read(target)
    outcomes.loc[0, "date"] = settings.RESEARCH_CONFIG["holdout_start"]
    outcomes.to_parquet(target, index=False)
    sealed_digest = _sha256(target)
    # Rehash the poisoned artifact: the inventory alone is insufficient protection.
    result["artifacts_sha256"]["outcomes.parquet"] = sealed_digest
    (folder / "result.json").write_text(json.dumps(result), encoding="utf-8")

    inspected_columns = []

    def guarded_read(source, *args, **kwargs):
        if isinstance(source, (str, Path)) and Path(source).resolve() == target.resolve():
            pytest.fail("unprotected path read exposed sealed outcomes before date validation")
        if hasattr(source, "getvalue") and hashlib.sha256(source.getvalue()).hexdigest() == sealed_digest:
            inspected_columns.append(kwargs.get("columns"))
            if kwargs.get("columns") != ["date"]:
                pytest.fail("sealed numeric outcomes were read before the date-only check")
        return original_read(source, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", guarded_read)
    with pytest.raises(ValueError, match="مهروموم"):
        review_annual_census(db, folder)
    assert inspected_columns == [["date"]]


@pytest.mark.parametrize(
    "double_status,triple_status",
    [
        ("complete_no_hit", "missing_observation"),
        ("missing_observation", "complete_no_hit"),
        ("missing_observation", "invalid_observation"),
    ],
)
def test_summary_rejects_impossible_paired_path_statuses(double_status, triple_status):
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days, final_close=300.0)
    rows = compute_annual_multibagger_labels(
        prices, prices.iloc[:1][["symbol", "date"]], days, observed_through=days[-1]
    )
    rows.loc[rows.threshold.eq(2.0), "status"] = double_status
    rows.loc[rows.threshold.eq(3.0), "status"] = triple_status
    with pytest.raises(ValueError):
        summarize_annual_multibagger_labels(rows)


@pytest.mark.parametrize(
    "field,change",
    [
        ("valid_prefix_sessions", 251),
        ("path_problem", "missing_observation"),
        ("endpoint_multiple", 2.5),
        ("observed_through", "2024-09-01"),
    ],
)
def test_summary_rejects_different_paths_for_the_same_opportunity(field, change):
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days, final_close=300.0)
    rows = compute_annual_multibagger_labels(
        prices, prices.iloc[:1][["symbol", "date"]], days, observed_through=days[-1]
    )
    rows.loc[rows.threshold.eq(3.0), field] = change
    with pytest.raises(ValueError, match="یک مسیر"):
        summarize_annual_multibagger_labels(rows)


def test_summary_accepts_both_no_hits_on_the_same_complete_path():
    days = pd.date_range("2024-01-01", periods=HORIZON_SESSIONS + 1).strftime("%Y-%m-%d").tolist()
    prices = synthetic_prices(days, final_close=100.0)
    rows = compute_annual_multibagger_labels(
        prices, prices.iloc[:1][["symbol", "date"]], days, observed_through=days[-1]
    )
    summaries = summarize_annual_multibagger_labels(rows)
    assert [entry["complete_no_hit"] for entry in summaries] == [1, 1]
    assert [entry["unknown"] for entry in summaries] == [0, 0]
