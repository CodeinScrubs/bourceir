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


def test_quality_panel_marks_different_code_as_archival(db, tmp_path, monkeypatch):
    output, report = write_report(db, tmp_path, monkeypatch)
    report['diagnostic_code_sha256'] = 'different'
    output.write_text(json.dumps(report), encoding='utf-8')
    at = AppTest.from_function(_panel).run(timeout=60)
    assert not at.exception
    assert any('گزارش آرشیوی' in item.value for item in at.warning)


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
