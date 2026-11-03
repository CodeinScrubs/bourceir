"""Retrospective rows preserve unknowns, symbols and private output boundaries."""

import pandas as pd
import pytest

from config import settings
from research import hunter_audit_run
from research.expectancy import forecast_expectancy, validate_expectancy_model
from research.hunter_audit import checked_opportunities
from research.hunter_audit_run import _rows_target, _write_opportunity_rows
from tests.test_hunter_audit import audit_inputs
from tests.test_prediction_contracts import fitted


def test_model_coverage_counts_obey_intersection_bounds():
    model, _, _ = fitted()
    model['fitted_rows'] -= 1
    # Every training row has a known outcome and complete features in this fixture.
    with pytest.raises(ValueError, match='شمار پوشش'):
        validate_expectancy_model(model)


def test_noncanonical_symbol_is_rejected_before_inference():
    model, current, _ = fitted()
    current = current.iloc[:1].copy()
    current['symbol'] = 'كي'
    with pytest.raises(ValueError, match='کلید'):
        forecast_expectancy(current, model)


def details():
    forecast, outcomes, picks, model = audit_inputs()
    rows = checked_opportunities(forecast, outcomes, picks.iloc[1:].assign(rank=0), model,
                                 holdout_start='2025-03-21')
    return rows.assign(fold=0)


def test_complete_symbol_panel_and_unknown_misses_survive_parquet(tmp_path):
    target = tmp_path/'opportunities.parquet'
    rows = details()
    rows['private_note'] = 'synthetic sentinel must not be exported'
    result = _write_opportunity_rows(rows, target)
    actual = pd.read_parquet(target)
    assert result['rows'] == len(actual) == 3
    assert actual.symbol.tolist() == rows.symbol.tolist()
    assert actual.selected.tolist() == [False, False, True]
    assert bool(actual.loc[0, 'normal_missed_positive'])
    assert bool(actual.loc[0, 'normal_missed_above_3pct'])
    assert not bool(actual.loc[0, 'normal_missed_above_10pct'])  # Strictly greater than 10%.
    assert pd.isna(actual.loc[2, 'normal_missed_positive'])
    assert pd.isna(actual.loc[2, 'stress_missed_above_10pct'])
    assert pd.isna(actual.loc[2, 'corporate_actions'])
    assert actual.loc[0, 'corporate_actions'] == 2
    assert 'private_note' not in actual and 'private_note' not in result['columns']
    assert not result['selections_are_fills']


def test_detailed_table_publication_never_overwrites_existing_file(tmp_path):
    target = tmp_path/'opportunities.parquet'
    old = b'original file remains unchanged'
    target.write_bytes(old)
    with pytest.raises(FileExistsError):
        _write_opportunity_rows(details(), target)
    assert target.read_bytes() == old
    assert not list(tmp_path.glob('*.tmp'))


def test_unsupported_exclusive_publication_fails_without_partial_destination(tmp_path, monkeypatch):
    target = tmp_path/'opportunities.parquet'
    original = OSError('filesystem does not support hard links')
    def fail(*args):
        raise original
    monkeypatch.setattr(hunter_audit_run.os, 'link', fail)
    with pytest.raises(OSError) as caught:
        _write_opportunity_rows(details(), target)
    assert caught.value is original
    assert not target.exists() and not list(tmp_path.glob('*.tmp'))


def test_cleanup_error_never_hides_the_primary_write_failure(tmp_path, monkeypatch):
    from pathlib import Path

    target = tmp_path/'opportunities.parquet'
    primary = OSError('flush failed before publication')
    unlink = Path.unlink
    def fail_flush(*args):
        raise primary
    def fail_cleanup(*args, **kwargs):
        raise PermissionError('temporary lock')
    monkeypatch.setattr(hunter_audit_run.os, 'fsync', fail_flush)
    monkeypatch.setattr(Path, 'unlink', fail_cleanup)
    try:
        with pytest.raises(OSError) as caught:
            _write_opportunity_rows(details(), target)
        assert caught.value is primary
        assert any('cleanup also failed' in note for note in primary.__notes__)
        assert not target.exists()
    finally:
        for temporary in tmp_path.glob('*.tmp'):
            unlink(temporary)


@pytest.mark.parametrize('case', ['outside', 'inside_archive', 'existing', 'wrong_suffix'])
def test_rows_target_cannot_modify_source_archives_or_existing_paths(tmp_path, monkeypatch, case):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    private = settings.DATA_DIR/'private'
    root = private/'frozen-research'
    target = private/'new.parquet'
    if case == 'outside':
        target = tmp_path/'outside.parquet'
    elif case == 'inside_archive':
        target = root/'new.parquet'
    elif case == 'wrong_suffix':
        target = private/'new.json'
    else:
        target.parent.mkdir(parents=True)
        target.write_bytes(b'original')
    with pytest.raises(ValueError):
        _rows_target(target, root)


def test_new_private_rows_path_is_normalized_without_creating_directories(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    target = settings.DATA_DIR/'private'/'audit'/'all.parquet'
    assert _rows_target(target, settings.DATA_DIR/'research') == target.resolve()
    assert not target.parent.exists()
