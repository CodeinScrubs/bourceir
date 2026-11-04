"""فقط داده مصنوعی موقت: مخرج، فرض صف و مرز دوره مهروموم."""

import hashlib
import io
import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from ops import entry_availability_audit as audit


def bar(symbol, **updates):
    row = dict(date='2024-01-02', symbol=symbol, volume=1000., value=100000., low_raw=99., high_raw=101.,
               close_raw=100., close_final_raw=100., ref_price_raw=100.)
    row.update(updates)
    return row


def sample():
    labels = pd.DataFrame([dict(date='2024-01-01', symbol=s, entry_date='2024-01-02',
                                entry_executable=False, label_status_2d='entry_blocked', exit_date_2d='')
                           for s in ('a', 'b', 'c', 'd', 'e')])
    prices = pd.DataFrame([
        bar('b', volume=0., value=0.),
        bar('c', low_raw=100.6, high_raw=100.6, close_raw=100.6, close_final_raw=100.6, value=100600.),
        bar('d'), bar('e', value=np.nan),
    ])
    return labels, prices


def parquet(frame):
    target = io.BytesIO()
    frame.to_parquet(target, index=False)
    return target.getvalue()


def test_five_blocked_categories_are_disjoint_and_include_unknowns():
    labels, prices = sample()
    labels_before, prices_before = labels.copy(deep=True), prices.copy(deep=True)
    report = audit.audit_frames(labels, prices, horizon=2)
    assert report['blocked_labels'] == 5
    assert list(report['categories'].values()) == [1, 1, 1, 1, 1]
    assert report['missing_entry_row_fraction'] == .2
    assert report['locked_proxy_move_bins']['below_2_percent'] == 1
    assert report['by_year'][0]['blocked_labels'] == 5
    assert not report['historical_price_band_verified'] and not report['actual_order_fill_verified']
    assert not report['accepted_for_signals'] and not report['real_trading_performance_measured']
    pd.testing.assert_frame_equal(labels, labels_before)
    pd.testing.assert_frame_equal(prices, prices_before)


def test_zero_blocked_rows_yield_zero_counts_and_unknown_fraction():
    labels, prices = sample()
    labels['label_status_2d'] = 'censored'
    result = audit.audit_frames(labels, prices, horizon=2)
    assert result['blocked_labels'] == 0 and result['missing_entry_row_fraction'] is None
    assert all(count == 0 for count in result['categories'].values())


@pytest.mark.parametrize('column,value', [('value', '100600'), ('high_raw', True), ('close_raw', np.inf),
                                          ('ref_price_raw', None), ('volume', 1.5), ('volume', -1)])
def test_bad_numeric_cells_never_become_valid_or_zero(column, value):
    labels, prices = sample()
    prices[column] = prices[column].astype(object)
    prices.loc[prices.symbol.eq('c'), column] = value
    result = audit.audit_frames(labels, prices, horizon=2)
    assert result['categories']['locked_up_daily_proxy'] == 0
    assert result['categories']['invalid_or_incomplete_entry_row'] == 2


@pytest.mark.parametrize('value', [True, 0, -1, 1.5, '2'])
def test_invalid_horizon_is_not_accepted(value):
    labels, prices = sample()
    with pytest.raises(ValueError, match='افق'):
        audit.audit_frames(labels, prices, horizon=value)


@pytest.mark.parametrize('damage', ['duplicate_labels', 'duplicate_prices', 'invalid_status',
                                    'filled_block', 'blank_entry', 'entry_on_decision', 'nonboolean'])
def test_identity_and_status_contracts_reject_inconsistent_inputs(damage):
    labels, prices = sample()
    if damage == 'duplicate_labels':
        labels = pd.concat([labels, labels.iloc[[0]]], ignore_index=True)
    elif damage == 'duplicate_prices':
        prices = pd.concat([prices, prices.iloc[[0]]], ignore_index=True)
    elif damage == 'invalid_status':
        labels.loc[0, 'label_status_2d'] = 'other'
    elif damage == 'filled_block':
        labels.loc[0, 'entry_executable'] = True
    elif damage == 'blank_entry':
        labels.loc[0, 'entry_date'] = ''
    elif damage == 'entry_on_decision':
        labels.loc[0, 'entry_date'] = labels.loc[0, 'date']
    else:
        labels['entry_executable'] = labels.entry_executable.astype(object)
        labels.loc[0, 'entry_executable'] = 0
    with pytest.raises(ValueError):
        audit.audit_frames(labels, prices, horizon=2)


def test_duplicate_persian_arabic_variants_are_not_silently_joined():
    labels, prices = sample()
    labels.loc[0, 'symbol'] = 'كي'
    labels.loc[1, 'symbol'] = 'کی'
    with pytest.raises(ValueError, match='تکراری'):
        audit.audit_frames(labels, prices, horizon=2)


def test_normalization_cannot_turn_a_phantom_symbol_into_a_match():
    labels, prices = sample()
    labels.loc[0, 'symbol'] = '\u200c'
    with pytest.raises(ValueError, match='خالی'):
        audit.audit_frames(labels, prices, horizon=2)


def test_numeric_overflow_is_unknown_not_a_zero_or_a_crash():
    labels, prices = sample()
    prices['volume'] = prices.volume.astype(object)
    prices.loc[prices.symbol.eq('c'), 'volume'] = 10**400
    report = audit.audit_frames(labels, prices, horizon=2)
    assert report['categories']['invalid_or_incomplete_entry_row'] == 2
    assert report['categories']['locked_up_daily_proxy'] == 0


@pytest.mark.parametrize('target,column', [('labels', 'date'), ('labels', 'entry_date'),
                                           ('labels', 'exit_date_2d'), ('prices', 'date')])
def test_sealed_dates_are_rejected_before_reading_any_financial_column(monkeypatch, target, column):
    labels, prices = sample()
    (labels if target == 'labels' else prices).loc[0, column] = '2025-03-21'
    label_bytes, price_bytes = parquet(labels), parquet(prices)
    original, calls = pd.read_parquet, []
    def watched(*args, **kwargs):
        calls.append(kwargs.get('columns'))
        return original(*args, **kwargs)
    monkeypatch.setattr(pd, 'read_parquet', watched)
    with pytest.raises(ValueError, match='مهروموم'):
        audit.audit_snapshots(label_bytes, price_bytes, horizon=2, cutoff='2025-03-21')
    assert calls == [['date', 'entry_date', 'exit_date_2d'], ['date']]


@pytest.mark.parametrize('cutoff', ['2025-03-22', '2025-3-21', True])
def test_cutoff_never_advances_into_holdout(cutoff):
    labels, prices = sample()
    with pytest.raises(ValueError, match='مرز'):
        audit.audit_snapshots(parquet(labels), parquet(prices), horizon=2, cutoff=cutoff)


def test_configuration_cannot_advance_the_original_hard_seal(monkeypatch):
    labels, prices = sample()
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2026-01-01')
    prices.loc[0, 'date'] = '2025-04-01'
    with pytest.raises(ValueError, match='مهروموم'):
        audit.audit_snapshots(parquet(labels), parquet(prices), horizon=2, cutoff=None)
    with pytest.raises(ValueError, match='مرز'):
        audit.audit_snapshots(parquet(labels), parquet(prices), horizon=2, cutoff='2025-06-01')


def test_configuration_can_only_make_the_boundary_earlier(monkeypatch):
    labels, prices = sample()
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2024-01-02')
    with pytest.raises(ValueError, match='مهروموم'):
        audit.audit_snapshots(parquet(labels), parquet(prices), horizon=2, cutoff=None)


def test_snapshots_are_hash_bound_and_no_future_values_are_loaded(tmp_path, monkeypatch, capsys):
    labels, prices = sample()
    label_path, price_path = tmp_path / 'labels.parquet', tmp_path / 'prices.parquet'
    label_path.write_bytes(parquet(labels)); price_path.write_bytes(parquet(prices))
    label_hash = hashlib.sha256(label_path.read_bytes()).hexdigest()
    price_hash = hashlib.sha256(price_path.read_bytes()).hexdigest()
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    arguments = ['--labels', str(label_path), '--labels-sha256', label_hash, '--prices', str(price_path),
                 '--prices-sha256', price_hash, '--horizon', '2']
    for _ in range(2):
        assert audit.main(arguments) == 0
        result = json.loads(capsys.readouterr().out)
        assert result['blocked_labels'] == 5 and not result['accepted_for_signals']
    reports = list((settings.DATA_DIR / 'private' / 'entry-availability-audit').glob('*/report.json'))
    assert len(reports) == 2 and all(json.loads(p.read_text(encoding='utf-8'))['labels_sha256'] == label_hash for p in reports)
    assert hashlib.sha256(label_path.read_bytes()).hexdigest() == label_hash
    assert hashlib.sha256(price_path.read_bytes()).hexdigest() == price_hash
    with pytest.raises(SystemExit) as error:
        audit.main(arguments[:1] + [str(label_path)] + ['--labels-sha256', '0'*64] + arguments[4:])
    assert error.value.code == 2 and len(list((settings.DATA_DIR / 'private').rglob('report.json'))) == 2


def test_blocked_label_without_price_row_is_reported_as_unresolved_evidence():
    labels, prices = sample()
    report = audit.audit_snapshots(parquet(labels), parquet(prices), horizon=2, cutoff='2025-03-21')
    assert report['categories']['absent_entry_price_row'] == 1
    assert not report['actual_order_fill_verified']


@pytest.mark.parametrize('target,column', [('labels', 'date'), ('labels', 'entry_date'),
                                           ('labels', 'exit_date_2d'), ('prices', 'date')])
def test_in_memory_api_also_rejects_sealed_dates(target, column):
    labels, prices = sample()
    (labels if target == 'labels' else prices).loc[0, column] = '2025-03-21'
    with pytest.raises(ValueError, match='مهروموم'):
        audit.audit_frames(labels, prices, horizon=2)
