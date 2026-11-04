"""قرارداد CLI و نمایش شواهد؛ داده فقط در پایگاه و پوشه موقت آزمون."""

import json

import pytest
from streamlit.testing.v1 import AppTest

from brs import build_parser
from config import settings
from dashboard.reliability import load_report
from ops.research_preflight import research_preflight
from tests.test_research_preflight import store


def _panel():
    from dashboard.reliability import show_reliability
    show_reliability(lambda *args: None)


def write_report(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    output = tmp_path / 'private' / 'research-preflight.json'
    output.parent.mkdir()
    report = research_preflight(db)
    output.write_text(json.dumps(report), encoding='utf-8')
    return output, report


def test_cli_delegates_preflight_readonly_with_explicit_private_output(db, tmp_path, monkeypatch):
    store(db)
    monkeypatch.setattr(settings, 'DB_PATH', db.db_path)
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    before = db.conn.total_changes
    target = tmp_path / 'private' / 'quality.json'
    args = build_parser().parse_args(['research-preflight', '--json', str(target)])
    assert args.func(args) == 0
    report = load_report(target)
    assert report['price_rows'] == 25 and not report['profitability_proven']
    assert before == db.conn.total_changes
    with pytest.raises(SystemExit) as exc:
        blocked = build_parser().parse_args(['research-preflight', '--before', '2099-01-01', '--json', str(target)])
        blocked.func(blocked)
    assert exc.value.code == 2 and load_report(target) == report


def test_uncertainty_cli_keeps_unknown_trial_count_and_passes_explicit_calendar(monkeypatch, tmp_path):
    received = []
    monkeypatch.setattr('research.uncertainty.main', lambda argv: received.extend(argv) or 2)
    args = build_parser().parse_args(['research-uncertainty', '--strategy', 'a.parquet', '--benchmark', 'b.parquet',
                                     '--initial-capital-rial', '100000', '--calendar', 'sessions.json',
                                     '--blocks', '3', '9', '--output', str(tmp_path / 'out.json')])
    assert args.func(args) == 2
    assert '--trials-attempted' not in received
    assert received[received.index('--calendar') + 1] == 'sessions.json'
    assert received[received.index('--blocks') + 1:received.index('--replications')] == ['3', '9']


def test_quality_panel_shows_unknown_denominator_without_claiming_edge(db, tmp_path, monkeypatch):
    store(db)
    with db.conn:
        db.conn.execute('DELETE FROM client_types_daily')
    write_report(db, tmp_path, monkeypatch)
    at = AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert at.metric[2].value == '25'
    assert any('آمادگی صدور سیگنال' in item.value for item in at.info)
    assert not at.success


def test_quality_panel_displays_retained_trade_only_lineage_without_claiming_all_are_wrong(db, tmp_path, monkeypatch):
    from core.recovery_lineage import VERSION as LINEAGE_VERSION
    from core.timeutils import now_tehran_iso
    from ops.trader_readiness import VERSION, code_identity

    store(db)
    write_report(db,tmp_path,monkeypatch)
    lineage=dict(version=LINEAGE_VERSION,cutoff_exclusive=settings.RESEARCH_CONFIG['holdout_start'],
        development_start=settings.RESEARCH_CONFIG['dev_start'],legacy_trade_receipts=3,retained_all_rows=2,
        retained_partial_rows=1,superseded_rows=0,retained_development_rows=2,retained_prior_rows=1,
        unrecognized_receipts=0,malformed_receipts=0,excluded_out_of_scope=0,review_clear=False,status='unresolved',
        source_conflict_count_known=False,independent_source_correctness_verified=False,
        historical_availability_verified=False,accepted_for_signals=False)
    readiness=dict(version=VERSION,generated_at=now_tehran_iso(),code=code_identity(),status='blocked',
        recovery_lineage=lineage,accepted_for_signals=False,profitability_proven=False)
    (tmp_path/'private'/'trader-readiness.json').write_text(json.dumps(readiness),encoding='utf-8')
    at=AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert any('۳' in w.value or '3' in w.value for w in at.warning if 'ریزمعامله' in w.value)
    assert any('غلط‌بودن' in w.value and 'همه' in w.value for w in at.warning)


def test_quality_panel_does_not_trust_a_malformed_or_old_lineage_receipt(db,tmp_path,monkeypatch):
    store(db)
    write_report(db,tmp_path,monkeypatch)
    target=tmp_path/'private'/'trader-readiness.json'
    target.write_text(json.dumps({'version':'trader-readiness-v1','recovery_lineage':{'review_clear':True}}),encoding='utf-8')
    at=AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert any('منشأ' in w.value and ('نامعتبر' in w.value or 'قدیمی' in w.value) for w in at.warning)


def test_lineage_receipt_rejects_duplicate_json_keys(tmp_path):
    from dashboard.reliability import load_lineage_report

    target=tmp_path/'lineage.json'
    target.write_text('{"version":"old","version":"new"}',encoding='utf-8')
    with pytest.raises(ValueError,match='تکراری'):
        load_lineage_report(target)


def test_quality_panel_marks_different_code_as_archival(db, tmp_path, monkeypatch):
    output, report = write_report(db, tmp_path, monkeypatch)
    report['diagnostic_code_sha256'] = 'different'
    output.write_text(json.dumps(report), encoding='utf-8')
    at = AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert any('گزارش آرشیوی' in item.value for item in at.warning)


def test_quality_panel_distinguishes_total_history_from_development(db, tmp_path, monkeypatch):
    prices, clients, sid = store(db)
    prior_prices = prices.head(1).assign(date='2012-01-03')
    prior_clients = clients.head(1).assign(date='2012-01-03')
    db.upsert_daily_prices_df(sid, prior_prices)
    db.upsert_client_types_df(sid, prior_clients)
    _, report = write_report(db, tmp_path, monkeypatch)
    assert report['price_rows'] == 26
    assert report['records'][0]['development_price_rows'] == 25
    at = AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert at.metric[0].label == 'ردیف قیمت بررسی‌شده'
    assert at.metric[0].value == '26'
    assert any('25 ردیف' in item.value for item in at.caption)


def test_quality_panel_marks_changed_environment_as_archival(db, tmp_path, monkeypatch):
    output, report = write_report(db, tmp_path, monkeypatch)
    report['feature_environment'] = {}
    output.write_text(json.dumps(report), encoding='utf-8')
    at = AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert any('گزارش آرشیوی' in item.value for item in at.warning)


def test_quality_report_rejects_inconsistent_symbol_denominators(db, tmp_path, monkeypatch):
    store(db)
    output, report = write_report(db, tmp_path, monkeypatch)
    report['records'][0]['unknown_client_pairs'] = 1
    output.write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(ValueError, match='نماد'):
        load_report(output)


@pytest.mark.parametrize('body', ['{', '[]', '{"version":"unsupported"}'])
def test_quality_panel_handles_damaged_reports(tmp_path, monkeypatch, body):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    output = tmp_path / 'private' / 'research-preflight.json'
    output.parent.mkdir()
    output.write_text(body, encoding='utf-8')
    at = AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert any('قابل نمایش نیست' in item.value for item in at.warning)


def test_quality_report_cannot_claim_accepted_signals(db, tmp_path, monkeypatch):
    output, report = write_report(db, tmp_path, monkeypatch)
    report['accepted_for_signals'] = True
    output.write_text(json.dumps(report), encoding='utf-8')
    with pytest.raises(ValueError, match='قرارداد'):
        load_report(output)
