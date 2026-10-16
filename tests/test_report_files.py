"""Report failures preserve inputs and the previous completed private report."""

import json
import os

import pytest

from config import settings
from ops.report_files import private_report_path, write_private_report


@pytest.fixture
def private(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    target = tmp_path / 'private' / 'report.json'
    target.parent.mkdir()
    target.write_text('{"previous": true}')
    return target


def test_success_replaces_a_complete_json_report(private):
    assert write_private_report(private, dict(unknown=None, note='نامعلوم')) == private
    assert json.loads(private.read_text()) == dict(unknown=None, note='نامعلوم')
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
