"""برابری واقعی spawn و مسیر ترتیبی؛ داده صرفا مصنوعی آزمون است."""

import numpy as np
import pandas as pd
import pytest

from research.expectancy_diagnostics import checked_workers, compare_jobs


def test_spawn_and_serial_diagnostics_are_exactly_equal_and_keep_request_order():
    dates = pd.bdate_range('2023-01-02', periods=65).strftime('%Y-%m-%d').tolist()
    capital = 100_000_000
    changes = .002 + .005 * np.sin(np.arange(65))
    strategy = pd.DataFrame(dict(date=dates, equity_rial=capital*np.cumprod(1+changes)))
    benchmark = pd.DataFrame(dict(date=dates, equity_rial=np.repeat(float(capital), 65)))
    common = dict(initial_capital_rial=capital, expected_sessions=dates, replications=1000,
                  trials_attempted=100, family_comparisons=300)
    requests = [(strategy, benchmark, dict(common, seed=seed)) for seed in (41, 97)]
    serial = compare_jobs(requests)
    assert compare_jobs(requests, workers=2) == serial
    assert [r['seed'] for r in serial] == [41, 97]
    assert all(r['intervals'] for r in serial)


@pytest.mark.parametrize('workers', [True, False, None, 0, 5, 2., '2'])
def test_worker_count_is_bounded_and_does_not_accept_coerced_values(workers):
    with pytest.raises(ValueError):
        checked_workers(workers)
