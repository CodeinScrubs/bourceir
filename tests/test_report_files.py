"""Report failures preserve inputs and the previous completed private report."""

import json
import os
import subprocess
import sys

import pytest

from config import settings
from ops.report_files import private_report_path, read_private_report, write_private_report


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    target = tmp_path / 'private' / 'report.json'
    target.parent.mkdir()
    target.write_text('{"previous": true}')
    return target


def test_success_replaces_a_complete_json_report(private):
    assert write_private_report(private, dict(unknown=None, note='نامعلوم')) == private
    assert json.loads(private.read_text(encoding='utf-8')) == dict(unknown=None, note='نامعلوم')
    assert list(private.parent.iterdir()) == [private]


@pytest.mark.parametrize('payload', [dict(bad=float('nan')), dict(bad=float('inf')), dict(bad=object())])
def test_invalid_payload_does_not_damage_previous_report(private, payload):
    before = private.read_bytes()
    with pytest.raises((ValueError, TypeError)):
        write_private_report(private, payload)
    assert private.read_bytes() == before
    assert list(private.parent.iterdir()) == [private]


def test_replace_failure_preserves_previous_report_and_removes_temp(private, monkeypatch):
    before = private.read_bytes()

    def fail(*args):
        raise OSError('simulated interrupted publication')

    monkeypatch.setattr('ops.report_files.os.replace', fail)
    with pytest.raises(OSError):
        write_private_report(private, dict(new=True))
    assert private.read_bytes() == before
    assert list(private.parent.iterdir()) == [private]


@pytest.mark.parametrize('destination', ['outside.json', 'private/report.db', 'private/../outside.json'])
def test_private_json_boundary(private, destination):
    with pytest.raises(ValueError):
        private_report_path(settings.DATA_DIR / destination)


def test_input_alias_and_hardlink_cannot_be_a_report(private):
    before = private.read_bytes()
    with pytest.raises(ValueError):
        write_private_report(private, {}, forbidden_paths=[private.parent / 'unused' / '..' / private.name])
    alias = private.parent / 'alias.json'
    os.link(private, alias)
    with pytest.raises(ValueError):
        write_private_report(alias, {}, forbidden_paths=[private])
    assert private.read_bytes() == before and alias.read_bytes() == before


@pytest.mark.parametrize('body', [b'[]', b'{"x": NaN}', b'{"x": Infinity}', b'{"x": 1e999}',
                                b'{"x": 1, "x": 2}', b'{"inner": {"x": 1, "x": 2}}', b'\xff'])
def test_reader_rejects_ambiguous_or_nonfinite_json(private, body):
    private.write_bytes(body)
    with pytest.raises(ValueError):
        read_private_report(private)


def test_reader_bounds_bytes_and_cannot_read_outside_private(private):
    with pytest.raises(ValueError, match='اندازه'):
        read_private_report(private, max_bytes=2)
    with pytest.raises(ValueError):
        read_private_report(private.parent.parent / 'outside.json')


def test_persian_private_json_reads_with_python_utf8_mode_disabled(private):
    write_private_report(private, dict(note='نامعلوم'))
    code = ('import json,sys; from pathlib import Path; from config import settings; '
            'from ops.report_files import read_private_report; '
            'assert sys.flags.utf8_mode == 0; settings.DATA_DIR=Path(sys.argv[1]); '
            'print(json.dumps(read_private_report(sys.argv[2]), ensure_ascii=True))')
    completed = subprocess.run([sys.executable, '-X', 'utf8=0', '-c', code, str(settings.DATA_DIR), str(private)],
                               check=True, capture_output=True, encoding='ascii')
    assert json.loads(completed.stdout) == dict(note='نامعلوم')
