"""An exit failure or incomplete coverage cannot be reported as a pass."""

import json
import os

import pytest

from ops.validation_run import assess_pytest, source_manifest, write_state


@pytest.mark.parametrize('defect', ['none', 'exit', 'failure', 'error', 'missing', 'duplicate', 'unexpected', 'source'])
def test_receipt_distinguishes_completion_coverage_and_success(tmp_path, defect):
    cases = '<testcase classname="tests.test_a" name="test_one"/>'
    if defect in {'failure', 'error'}:
        cases = f'<testcase classname="tests.test_a" name="test_one"><{defect}/></testcase>'
    if defect == 'missing':
        cases = ''
    if defect == 'duplicate':
        cases *= 2
    if defect == 'unexpected':
        cases += '<testcase classname="tests.test_b" name="test_extra"/>'
    report = tmp_path/'report.xml'
    report.write_text(f'<testsuites><testsuite>{cases}</testsuite></testsuites>', encoding='utf-8')
    result = assess_pytest(['tests/test_a.py::test_one'], report, 1 if defect == 'exit' else 0,
                           source_unchanged=defect != 'source')
    assert result['execution_complete']
    assert result['status'] == ('passed' if defect == 'none' else 'failed')


def test_skipped_is_counted_separately_and_class_node_names_are_preserved(tmp_path):
    report = tmp_path/'report.xml'
    report.write_text('<testsuites><testsuite><testcase classname="tests.test_a.TestGroup" '
                      'name="test_one[1]"><skipped/></testcase></testsuite></testsuites>', encoding='utf-8')
    result = assess_pytest(['tests/test_a.py::TestGroup::test_one[1]'], report, 0, source_unchanged=True)
    assert result['status'] == 'passed' and result['counts']['skipped'] == 1


def test_state_is_terminal_after_atomic_replacement_and_sources_include_tests(tmp_path):
    path = tmp_path/'state.json'
    write_state(path, dict(status='running', execution_complete=False))
    write_state(path, dict(status='failed', execution_complete=True))
    assert json.loads(path.read_text()) == dict(status='failed', execution_complete=True)
    assert not path.with_suffix('.json.tmp').exists()
    (tmp_path/'tests').mkdir()
    test = tmp_path/'tests/test_a.py'
    test.write_text('assert True', encoding='utf-8')
    before = source_manifest(tmp_path)
    test.write_text('assert False', encoding='utf-8')
    assert source_manifest(tmp_path) != before
    assert set(before) == {'tests/test_a.py'}


def test_transient_windows_reader_lock_retries_without_exposing_partial_state(tmp_path, monkeypatch):
    import ops.validation_run as validation

    path = tmp_path/'state.json'
    write_state(path, dict(status='running'))
    replace, attempts = os.replace, []

    def locked_twice(source, target):
        attempts.append(source)
        assert json.loads(path.read_text()) == dict(status='running')
        if len(attempts) <= 2:
            raise PermissionError('simulated Windows sharing violation')
        return replace(source, target)

    monkeypatch.setattr(validation.os, 'replace', locked_twice)
    monkeypatch.setattr(validation.time, 'sleep', lambda _: None)
    write_state(path, dict(status='passed'))
    assert len(attempts) == 3
    assert json.loads(path.read_text()) == dict(status='passed')
    assert not list(tmp_path.glob('*.tmp'))


def test_persistent_lock_is_bounded_and_preserves_original_receipt(tmp_path, monkeypatch):
    import ops.validation_run as validation

    path = tmp_path/'state.json'
    write_state(path, dict(status='running'))
    ticks = iter([0., 11.])

    def locked(*_):
        raise PermissionError('persistent lock')

    monkeypatch.setattr(validation.os, 'replace', locked)
    monkeypatch.setattr(validation.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(PermissionError):
        write_state(path, dict(status='passed'))
    assert json.loads(path.read_text()) == dict(status='running')
    assert not list(tmp_path.glob('*.tmp'))


def test_generated_timing_receipts_do_not_change_source_fingerprint(tmp_path):
    test = tmp_path/'tests/test_a.py'
    test.parent.mkdir()
    test.write_text('assert True', encoding='utf-8')
    before = source_manifest(tmp_path)
    generated = tmp_path/'tests/.timing-output/run/report.json'
    generated.parent.mkdir(parents=True)
    generated.write_text('{"output":1}', encoding='utf-8')
    assert source_manifest(tmp_path) == before
    # A real JSON fixture is still an input, even with the same file name.
    fixture = tmp_path/'tests/fixtures/report.json'
    fixture.parent.mkdir()
    fixture.write_text('{"input":1}', encoding='utf-8')
    assert source_manifest(tmp_path) != before


def test_failed_sync_does_not_publish_an_unpersisted_receipt(tmp_path, monkeypatch):
    path = tmp_path/'state.json'
    write_state(path, {'status': 'running'})

    def failed_sync(_):
        raise OSError('disk sync failed')

    monkeypatch.setattr(os, 'fsync', failed_sync)
    with pytest.raises(OSError, match='disk sync failed'):
        write_state(path, {'status': 'complete'})
    assert json.loads(path.read_bytes()) == {'status': 'running'}
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('suffix', ['.html', '.css', '.csv', '.parquet'])
def test_non_python_fixture_mutation_is_detected(tmp_path, suffix):
    asset = tmp_path/('tests/fixtures/input'+suffix)
    asset.parent.mkdir(parents=True)
    asset.write_bytes(b'original test input')
    before = source_manifest(tmp_path)
    asset.write_bytes(b'changed test input')
    assert source_manifest(tmp_path) != before
