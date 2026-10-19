"""حفاظت تاریخ و مخرج پژوهش؛ داده فقط در پوشه موقت آزمون است."""

import hashlib

import pandas as pd
import pytest

from config import settings
from ops.research_review import _checked_mode, sha256
from research.probability import opportunity_features


class ArtifactHashes(dict):
    # رابط قدیمی مجموعه نام ها می خواست؛ این نگاشت همان قرارداد را نیز می پذیرد.
    def __ge__(self, other):
        return set(self) >= other


def test_review_rejects_sealed_curve_before_loading_financial_columns(tmp_path, monkeypatch):
    name = 'selected'
    paths = {kind: tmp_path / f'fold-00-{name}-{kind}.parquet'
             for kind in ('equity', 'executions', 'picks', 'decisions')}
    pd.DataFrame(dict(date=['2025-03-21'], equity_rial=[999.], positions=[0],
                      stale_positions=[0])).to_parquet(paths['equity'], index=False)
    for kind in ('executions', 'picks', 'decisions'):
        pd.DataFrame().to_parquet(paths[kind], index=False)
    protocol = dict(portfolio_version='portfolio-v3', holdout_start='2025-03-21',
                    config=dict(initial_capital_rial=1000., max_loss_pct=.1))
    record = dict(metrics={}, assumptions=dict(version='portfolio-v3',
                  config=dict(initial_capital_rial=1000., max_loss_pct=.1, stress=False)))
    fold = dict(test_start='2024-01-01', test_end_exclusive='2025-03-21')
    original, calls = pd.read_parquet, []

    def watched(*args, **kwargs):
        calls.append(kwargs.get('columns'))
        return original(*args, **kwargs)

    monkeypatch.setattr(pd, 'read_parquet', watched)
    with pytest.raises(ValueError):
        _checked_mode(tmp_path, 0, name, record, fold, protocol,
                      ArtifactHashes({p.name: sha256(p) for p in paths.values()}))
    assert calls == [['date']]


def test_nontrading_row_is_accounted_for_without_discarding_unknown_outcomes():
    features = pd.DataFrame(dict(date=['2024-01-01'] * 2, symbol=['الف', 'ب'], ret_5d=[.1, .2]))
    labels = pd.DataFrame(dict(date=['2024-01-01'], symbol=['الف'], value=[100.]))
    prices = pd.DataFrame(dict(date=['2024-01-01'] * 2, symbol=['الف', 'ب'],
                              value=[100., 0.], volume=[1., 0.]))
    selected = opportunity_features(features, labels, prices=prices)
    assert selected.symbol.tolist() == ['الف']
    assert selected.attrs['observed_nontrading_feature_rows'] == 1
    prices.loc[1, 'value'] = float('nan')
    with pytest.raises(ValueError):
        opportunity_features(features, labels, prices=prices)


def test_review_helper_is_part_of_archived_research_identity(tmp_path, monkeypatch):
    from research import run

    helper = tmp_path / 'ops' / 'research_review.py'
    helper.parent.mkdir()
    helper.write_text('# first\n', encoding='utf-8')
    monkeypatch.setattr(settings, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(run, '_code_fingerprint', lambda folders: 'fixed-test-only')
    monkeypatch.setattr(run, 'feature_environment', lambda: {})
    before = run.research_code_fingerprint()
    helper.write_text('# second\n', encoding='utf-8')
    assert run.research_code_fingerprint() != before
    archived = tmp_path / 'code'
    files = run.archive_research_code(archived)
    normalized = helper.read_bytes().replace(b'\r\n', b'\n')
    assert files['ops/research_review.py'] == hashlib.sha256(normalized).hexdigest()
    assert (archived / 'ops/research_review.py').read_bytes() == normalized


def test_nontrading_raw_row_cannot_silently_hide_a_contradictory_traded_label():
    features = pd.DataFrame(dict(date=['2024-01-01'], symbol=['آزمون']))
    labels = features.assign(value=123.)
    prices = features.assign(value=0., volume=0.)
    with pytest.raises(ValueError, match='عدم معامله'):
        opportunity_features(features, labels, prices=prices)


def test_training_cannot_use_an_outcome_resolved_before_its_decision():
    from research.expectancy import fit_expectancy
    from tests.test_expectancy import sample

    features, outcomes = sample()
    outcomes.loc[0, 'resolved_at'] = features.date.iloc[0]
    with pytest.raises(ValueError, match='خروج آموزش'):
        fit_expectancy(features, outcomes, train_end_exclusive='2023-05-03')


def test_replayed_empty_table_compares_semantically_but_extra_columns_fail():
    from research.expectancy_run import _assert_frame

    _assert_frame(pd.DataFrame(columns=pd.Index([], dtype='object')), pd.DataFrame(), 'مقایسه')
    with pytest.raises(ValueError, match='مقایسه'):
        _assert_frame(pd.DataFrame(columns=['unexpected']), pd.DataFrame(), 'مقایسه')
