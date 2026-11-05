import json
import sys

import pytest

from ops import jobs


@pytest.fixture
def job_directory(tmp_path, monkeypatch):
    directory = tmp_path / 'jobs'
    directory.mkdir()
    monkeypatch.setattr(jobs, 'JOBS_DIR', directory)
    monkeypatch.setattr(jobs.settings, 'LOG_DIR', tmp_path / 'logs')
    monkeypatch.setattr(jobs, '_OWNED_PROCESSES', {})
    return directory


@pytest.mark.parametrize('body', ['["invalid"]', '{"pid":"broken"}', '{"pid":123,"created_at":1e999}'])
def test_unreadable_job_identity_is_unknown_and_blocks_start(job_directory, monkeypatch, body):
    (job_directory / 'live.json').write_text(body, encoding='utf-8')

    def unexpected_launch(*args, **kwargs):
        pytest.fail('A damaged record can belong to a running worker')

    monkeypatch.setattr(jobs.subprocess, 'Popen', unexpected_launch)
    assert jobs.job_status('live')['running'] is None
    assert jobs.start_job('live', ['live']) is False
    assert jobs.stop_job('live') is False


def test_job_status_reads_only_the_named_job_log(job_directory, tmp_path, monkeypatch):
    other_file = tmp_path / 'other.txt'
    other_file.write_text('Unrelated test file', encoding='utf-8')
    (job_directory / 'live.json').write_text(
        json.dumps({'pid': 123, 'created_at': 100, 'log': str(other_file)}), encoding='utf-8',
    )
    monkeypatch.setattr(jobs.psutil, 'pid_exists', lambda pid: False)

    def missing_process(pid):
        raise jobs.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(jobs.psutil, 'Process', missing_process)
    assert jobs.job_status('live')['log_tail'] == []


def test_stale_owned_handle_does_not_hide_a_job_started_by_another_dashboard(job_directory, tmp_path, monkeypatch):
    script = tmp_path / 'worker.py'
    script.write_text('import sys, time\nif sys.argv[1] == "wait":\n    time.sleep(60)\n', encoding='utf-8')
    monkeypatch.setattr(jobs, 'BRS_SCRIPT', script)
    owner_a = jobs._OWNED_PROCESSES
    owner_b = {}
    assert jobs.start_job('worker', ['exit'])
    owner_a['worker'].wait(timeout=10)
    monkeypatch.setattr(jobs, '_OWNED_PROCESSES', owner_b)
    assert jobs.start_job('worker', ['wait'])
    try:
        monkeypatch.setattr(jobs, '_OWNED_PROCESSES', owner_a)
        status = jobs.job_status('worker')
        assert status['running'] is True
        assert status['returncode'] is None
        assert jobs.start_job('worker', ['exit']) is False
        assert jobs.stop_job('worker') is True
        owner_b['worker'].wait(timeout=10)
    finally:
        monkeypatch.setattr(jobs, '_OWNED_PROCESSES', owner_b)
        if owner_b['worker'].poll() is None:
            jobs.stop_job('worker')


def test_stop_uses_the_process_handle_that_passed_identity_validation(job_directory, monkeypatch):
    (job_directory / 'live.json').write_text(
        json.dumps({'pid': 123, 'created_at': 100, 'args': ['live']}), encoding='utf-8',
    )
    stopped = []

    class Process:
        pid = 123

        def __init__(self, label, created_at):
            self.label = label
            self.created_at = created_at

        def create_time(self):
            return self.created_at

        def is_running(self):
            return True

        def status(self):
            return 'running'

        def cmdline(self):
            return [sys.executable, str(jobs.BRS_SCRIPT), 'live']

        def children(self, recursive):
            return []

        def terminate(self):
            stopped.append(self.label)

    handles = iter([Process('verified', 100), Process('recycled', 101)])
    monkeypatch.setattr(jobs.psutil, 'Process', lambda pid: next(handles))
    monkeypatch.setattr(jobs.psutil, 'pid_exists', lambda pid: True)
    monkeypatch.setattr(jobs.psutil, 'wait_procs', lambda procs, timeout: (procs, []))
    assert jobs.stop_job('live') is True
    assert stopped == ['verified']
