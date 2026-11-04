"""Interval-state invariants only; stubs do not attest HTTP/archive evidence.

The companion integration suite owns ledger/source/transport validation. Here a
finite set of uncovered hourly cells is an independent oracle for the required
boundary: reopening an earlier window cannot reuse a previous distant frontier.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from ops import radar_continuity

ORIGIN = datetime(2026, 1, 1, tzinfo=timezone.utc)
HORIZON = 64


def instant(hour, offset_hours=0):
    return (ORIGIN + timedelta(hours=hour)).astimezone(
        timezone(timedelta(hours=offset_hours))
    ).isoformat()


def registration(name, ordinal, at, since):
    return dict(id=name, ordinal=ordinal, registered_at=instant(at),
                initial_since=None if since is None else instant(since),
                protocol_sha256='1' * 64, codal_sha256='2' * 64)


def observation(number, run, at, since, through):
    return dict(id=number, run_id=run, observed_at=instant(at), coverage=dict(
        window_start=instant(since), checkpoint_through=instant(through),
        window_scan_satisfied=True))


def uncovered_boundary(obligations, *, overlap_hours=0):
    """Set-of-cells oracle, independent of the production cursor arithmetic.

    Entries are in the specified logical event order. A registration or scan
    reopens its claimed suffix; only a proven scan removes its covered prefix.
    Thus an uncovered gap cannot disappear through reuse of an older frontier.
    """
    uncovered = set()
    for beginning, proven_through in obligations:
        uncovered.update(range(beginning, HORIZON + 1))
        if proven_through is not None:
            uncovered.difference_update(range(beginning, proven_through - overlap_hours))
    return ORIGIN + timedelta(hours=min(uncovered))


@pytest.fixture
def evaluate(monkeypatch, tmp_path):
    def invoke(runs, cycles, credits, *, overlap_hours=0):
        data = dict(runs=deepcopy(runs), cycles=deepcopy(cycles))
        original = deepcopy(data)

        def inventory(_db, **_kwargs):
            return data

        def arithmetic_proof(_root, _run, cycle):
            # Deliberately bypasses native-source validation, not its claims.
            return dict(credited=credits.get(cycle['id'], False),
                        reason='arithmetic_test_stub_only')

        monkeypatch.setattr(radar_continuity, 'inventory', inventory)
        monkeypatch.setattr(radar_continuity, '_positive_news', arithmetic_proof)
        result = radar_continuity.news_boundary(
            tmp_path, object(), as_of=instant(HORIZON),
            overlap_seconds=overlap_hours * 3600,
        )
        assert data == original, 'A continuity query must not mutate its predecessor inventory'
        assert result['complete_coverage_verified'] is False
        assert result['externally_timestamped'] is False
        return datetime.fromisoformat(result['required_since']), result

    return invoke


def test_empty_inventory_retains_explicit_one_day_unknown_window(evaluate):
    actual, result = evaluate([], [], {})
    assert actual == ORIGIN + timedelta(hours=HORIZON - 24)
    assert result['decisions'] == []


def test_empty_upgrades_cannot_pay_an_older_news_debt(evaluate):
    runs = [registration('first', 1, 20, 0),
            registration('upgrade', 2, 30, 20),
            registration('another-upgrade', 3, 40, 25)]
    actual, _ = evaluate(runs, [], {})
    assert actual == uncovered_boundary([(0, None), (20, None), (25, None)])


def test_a_proven_narrow_scan_cannot_cover_the_missing_older_prefix(evaluate):
    runs = [registration('first', 1, 10, 0)]
    cycles = [observation(1, 'first', 30, 10, 20)]
    actual, result = evaluate(runs, cycles, {1: True})
    assert actual == uncovered_boundary([(0, None), (10, 20)])
    assert result['decisions'][-1]['credited'] is False


def test_later_broad_coverage_can_retire_the_retained_debt(evaluate):
    runs = [registration('first', 1, 10, 0)]
    cycles = [observation(1, 'first', 30, 10, 20),
              observation(2, 'first', 40, 0, 35)]
    actual, _ = evaluate(runs, cycles, {1: True, 2: True})
    assert actual == uncovered_boundary([(0, None), (10, 20), (0, 35)])


def test_overlap_remains_owed_across_empty_upgrade_and_narrow_success(evaluate):
    runs = [registration('first', 1, 5, 0), registration('upgrade', 2, 25, 20)]
    cycles = [observation(1, 'first', 20, 0, 20),
              observation(2, 'upgrade', 35, 20, 30),
              observation(3, 'upgrade', 45, 18, 40)]
    prefix, _ = evaluate(runs, cycles[:2], {1: True, 2: True}, overlap_hours=2)
    assert prefix == uncovered_boundary([(0, None), (0, 20), (20, None), (20, 30)],
                                        overlap_hours=2)
    actual, _ = evaluate(runs, cycles, {1: True, 2: True, 3: True}, overlap_hours=2)
    assert actual == uncovered_boundary([(0, None), (0, 20), (20, None), (20, 30), (18, 40)],
                                        overlap_hours=2)


def test_reopened_debt_and_partial_scan_cannot_jump_an_uncovered_gap(evaluate):
    runs = [registration('first', 1, 10, 10), registration('widened', 2, 25, 0)]
    cycles = [observation(1, 'first', 20, 10, 20),
              observation(2, 'widened', 30, 0, 5)]
    actual, _ = evaluate(runs, cycles, {1: True, 2: True})
    assert actual == uncovered_boundary([(10, None), (10, 20), (0, None), (0, 5)])
    assert actual == ORIGIN + timedelta(hours=5)
    assert actual < ORIGIN + timedelta(hours=10), 'The uncovered [5,10) gap remains owed'


def test_an_unproven_success_flag_never_creates_coverage_credit(evaluate):
    runs = [registration('first', 1, 5, 0)]
    cycles = [observation(1, 'first', 30, 0, 20)]
    actual, result = evaluate(runs, cycles, {1: False})
    assert actual == uncovered_boundary([(0, None), (0, None)])
    assert result['decisions'][-1]['credited'] is False


def test_equivalent_offset_representations_have_the_same_boundary(evaluate):
    runs = [registration('first', 1, 5, 0), registration('upgrade', 2, 31, 20)]
    cycles = [observation(1, 'first', 30, 0, 30),
              observation(2, 'upgrade', 40, 20, 35)]
    expected = uncovered_boundary([(0, None), (0, 30), (20, None), (20, 35)])
    original, _ = evaluate(runs, cycles, {1: True, 2: True})
    changed_runs, changed_cycles = deepcopy(runs), deepcopy(cycles)
    for row, offset in zip(changed_runs, (3.5, -7)):
        for key in ('registered_at', 'initial_since'):
            row[key] = datetime.fromisoformat(row[key]).astimezone(
                timezone(timedelta(hours=offset))).isoformat()
    for row, offset in zip(changed_cycles, (-7, 3.5)):
        row['observed_at'] = datetime.fromisoformat(row['observed_at']).astimezone(
            timezone(timedelta(hours=offset))).isoformat()
        for key in ('window_start', 'checkpoint_through'):
            row['coverage'][key] = datetime.fromisoformat(row['coverage'][key]).astimezone(
                timezone(timedelta(hours=-offset))).isoformat()
    equivalent, _ = evaluate(changed_runs[::-1], changed_cycles[::-1], {1: True, 2: True})
    assert equivalent == original == expected


def test_a_stale_checkpoint_cannot_advance_the_current_boundary(evaluate):
    runs = [registration('first', 1, 10, 10)]
    cycles = [observation(1, 'first', 20, 10, 20),
              observation(2, 'first', 30, 20, 15)]
    actual, _ = evaluate(runs, cycles, {1: True, 2: True})
    assert actual == uncovered_boundary([(10, None), (10, 20), (20, 15)])
    assert actual == ORIGIN + timedelta(hours=20)
