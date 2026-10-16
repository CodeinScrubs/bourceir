"""داده مصنوعی فقط در دیتابیس و پوشه موقت آزمون؛ شبکه در آزمون استفاده نمی‌شود."""

import json

import pandas as pd
import pytest

from brs import build_parser
from config import settings
from core.price_quality import price_quality
from ops.price_recheck import compare_row, run_recheck
from ops.research_preflight import research_preflight
from tests.test_research_preflight import store


def damage(db):
    prices, _, sid = store(db)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET open_raw=NULL WHERE symbol_id=? AND date=?',
                        (sid, prices.date.iloc[0]))
    return prices, sid


def test_missing_open_has_one_precise_reason_and_no_invented_range_error(db):
    prices, _ = damage(db)
    result = research_preflight(db)
    issue = result['price_issues'][0]
    assert issue['reasons'] == ['missing_or_nonfinite_open_raw']
    assert issue['raw']['open_raw'] is None and issue['date'] == prices.date.iloc[0]
    assert result['price_issue_counts'] == {'missing_or_nonfinite_open_raw': 1}
    assert not result['price_details_truncated'] and not result['input_contract_passed']


def test_official_final_outside_traded_range_is_not_price_corruption(db):
    prices, _, _ = store(db)
    prices['close_final_raw'] = prices.high_raw * 1.5
    _, flags = price_quality(prices)
    assert not flags.any(axis=None)


def test_pre_development_history_is_counted_separately_not_hidden(db):
    prices, sid = damage(db)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET date=? WHERE symbol_id=? AND date=?',
                        ('2011-01-01', sid, prices.date.iloc[0]))
    result = research_preflight(db)
    record = result['records'][0]
    assert record['prior_price_rows'] == record['invalid_prior_price_rows'] == 1
    assert record['development_price_rows'] == 24 and record['invalid_development_price_rows'] == 0
    assert not result['input_contract_passed'] and result['price_issues'][0]['phase'] == 'prior_history'


def test_detail_truncation_does_not_change_counts_or_raw_identity(db):
    damage(db)
    full, bounded = research_preflight(db), research_preflight(db, detail_limit=0)
    assert bounded['price_details_truncated'] and bounded['price_issues'] == []
    for key in ('raw_inputs', 'price_rows', 'records', 'price_issue_counts', 'blockers'):
        assert full[key] == bounded[key]


def test_recheck_registers_before_fetch_preserves_old_and_new_and_keeps_database_unchanged(db, tmp_path, monkeypatch):
    prices, _ = damage(db)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    before = db.conn.total_changes
    class Provider:
        name = 'tsetmc'
        def get_symbol_history(self, ins_code):
            plans = list((tmp_path / 'private' / 'price-rechecks').glob('*/plan.json'))
            assert len(plans) == 1
            plan = json.loads(plans[0].read_text(encoding='utf-8'))
            assert plan['selected'][0]['ins_code'] == ins_code
            assert plan['automatic_repair'] is False and not db.conn.in_transaction
            return prices
    result, folder = run_recheck(db, provider=Provider())
    assert result['source_consistent_candidates'] == 1 and result['requests_made'] == 1
    row = result['observations'][0]
    assert row['status'] == 'source_changed_consistent' and row['before']['open_raw'] is None
    assert row['after']['open_raw'] == prices.open_raw.iloc[0]
    assert row['local_row_unchanged_at_completion'] and not row['applied']
    assert db.conn.total_changes == before and not db.conn.in_transaction
    assert (folder / 'observation-001.json').exists() and (folder / 'code' / 'core' / 'price_quality.py').exists()
    assert research_preflight(db)['price_issues'][0]['raw']['open_raw'] is None


@pytest.mark.parametrize('kind, status', [('missing', 'provider_row_missing'), ('duplicate', 'provider_date_ambiguous'),
                                        ('unavailable', 'source_unavailable'), ('same', 'source_unchanged'),
                                        ('bad', 'source_changed_invalid')])
def test_recheck_failure_and_inconsistent_correction_never_become_valid(db, kind, status):
    prices, _ = damage(db)
    issue = research_preflight(db)['price_issues'][0]
    if kind == 'missing': history = prices.iloc[1:]
    elif kind == 'duplicate': history = pd.concat([prices, prices.head(1)])
    elif kind == 'unavailable': history = None
    elif kind == 'same': history = pd.DataFrame([issue['raw']])
    else:
        history = prices.copy()
        history.loc[0, 'open_raw'] = history.high_raw.iloc[0] + 1
    result = compare_row(issue, history)
    assert result['status'] == status and not result['applied']


def test_recheck_detects_local_mutation_during_network_wait(db, tmp_path, monkeypatch):
    prices, sid = damage(db)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    class Provider:
        name = 'tsetmc'
        def get_symbol_history(self, ins_code):
            with db.conn:
                db.conn.execute('UPDATE daily_prices SET close_raw=123 WHERE symbol_id=?', (sid,))
            return prices
    result, _ = run_recheck(db, provider=Provider())
    assert not result['observations'][0]['local_row_unchanged_at_completion']
    assert result['source_consistent_candidates'] == 0


def test_network_is_opt_in_and_raw_holdout_not_selected(db, tmp_path, monkeypatch):
    prices, sid = damage(db)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET date=? WHERE symbol_id=? AND date=?',
                        ('2026-01-01', sid, prices.date.iloc[0]))
    result, _ = run_recheck(db)
    assert result['requests_made'] == 0 and result['selected_rows'] == 0
    args = build_parser().parse_args(['research-recheck', '--max-rows', '2'])
    assert args.fetch is False


def test_recheck_continues_after_provider_failure_without_logging_secret(db, tmp_path, monkeypatch):
    damage(db)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    class Provider:
        name = 'tsetmc'
        def get_symbol_history(self, ins_code):
            raise RuntimeError('SECRET IN URL')
    result, folder = run_recheck(db, provider=Provider())
    assert result['observations'][0]['error_kind'] == 'RuntimeError'
    assert 'SECRET' not in (folder / 'result.json').read_text(encoding='utf-8')
    assert result['statuses'] == {'source_unavailable': 1}


@pytest.mark.parametrize('limit', [0, 51, True, 1.5])
def test_bad_recheck_limits_fail_before_any_artifacts(db, tmp_path, monkeypatch, limit):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    with pytest.raises(ValueError):
        run_recheck(db, max_rows=limit)
    assert not (tmp_path / 'private').exists()


@pytest.mark.parametrize('target', ['plan.json', 'code/core/price_quality.py'])
def test_tampering_during_fetch_prevents_final_publication(db, tmp_path, monkeypatch, target):
    prices, _ = damage(db)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    class Provider:
        name = 'tsetmc'
        def get_symbol_history(self, ins_code):
            folder = next((tmp_path / 'private' / 'price-rechecks').iterdir())
            (folder / target).write_text('changed during capture', encoding='utf-8')
            return prices
    with pytest.raises(ValueError):
        run_recheck(db, provider=Provider())
    folder = next((tmp_path / 'private' / 'price-rechecks').iterdir())
    assert not (folder / 'result.json').exists()
    assert (folder / 'observation-001.json').exists()


def test_quality_report_shows_missing_price_on_invalid_date_without_crashing(db, tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from tests.test_reliability_workflow import _panel, write_report

    prices, sid = damage(db)
    with db.conn:
        db.conn.execute('UPDATE daily_prices SET date=? WHERE symbol_id=? AND date=?',
                        ('2024-01-32', sid, prices.date.iloc[0]))
    write_report(db, tmp_path, monkeypatch)
    app = AppTest.from_function(_panel).run(timeout=60)
    assert not app.exception
    assert any('قرارداد داده عبور نکرده' in message.value for message in app.error)
    assert not any('قابل نمایش نیست' in message.value for message in app.warning)
