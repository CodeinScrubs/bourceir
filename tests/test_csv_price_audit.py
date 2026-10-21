import json

import pytest

from config import settings
from ops.csv_price_audit import capture_csv, main, review_csv
from tests.test_legacy_price_csv import HEADER, row


@pytest.fixture
def local_csv(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    source = tmp_path / 'source.csv'
    source.write_text(HEADER+row(), encoding='utf-8')
    return source, tmp_path / 'private/audit'


def test_real_archive_recomputed_and_never_overwritten(local_csv):
    source, out = local_csv
    before = source.read_bytes()
    result = capture_csv(source, symbol='آزمون', unit='Rial', out=out)
    assert result == out / 'report.json' and source.read_bytes() == before
    review = review_csv(out)
    assert review['replay_verified'] and not review['accepted_for_signals']
    assert not review['database_mutated']
    with pytest.raises(FileExistsError):
        capture_csv(source, symbol='آزمون', unit='Rial', out=out)


@pytest.mark.parametrize('target', ['source.csv','normalized.csv','report.json','plan.json'])
def test_tampering_is_detected(local_csv, target):
    source, out = local_csv
    capture_csv(source, symbol='آزمون', unit='Rial', out=out)
    path = out / target
    if target.endswith('.csv'):
        path.write_bytes(path.read_bytes()+b'\n')
    else:
        obj = json.loads(path.read_text(encoding='utf-8'))
        if target == 'report.json':
            obj['summary']['source_rows'] += 1
        else:
            obj['code_sha256'] = {}
        path.write_text(json.dumps(obj), encoding='utf-8')
    with pytest.raises(ValueError):
        review_csv(out)


def test_private_outputs_only_and_invalid_attempt_retained(local_csv):
    source, out = local_csv
    with pytest.raises(ValueError, match='private'):
        capture_csv(source, symbol='آزمون', unit='Rial', out=source.parent / 'public')
    source.write_bytes(b'invalid')
    with pytest.raises(ValueError):
        capture_csv(source, symbol='آزمون', unit='Rial', out=out)
    assert (out / 'source.csv').read_bytes() == b'invalid'
    assert json.loads((out / 'report.json').read_text(encoding='utf-8'))['status'] == 'failed'


def test_cli_capture_review_and_bad_input_exit(local_csv, capsys):
    source, out = local_csv
    assert main(['capture',str(source),'--symbol','آزمون','--unit','Rial','--out',str(out)]) == 0
    assert main(['review',str(out)]) == 0
    assert main(['review',str(out / 'absent')]) == 2
    assert 'replay_verified' in capsys.readouterr().out
