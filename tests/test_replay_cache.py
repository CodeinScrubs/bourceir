"""Checkpoint faults must fail closed, and interrupted work must not be cached."""

import hashlib
import json
import zipfile

import pytest

from research.replay_cache import META, RESULT, ReplayCache
from research.search_ledger import identity

SPEC = dict(raw_sha256='a' * 64, code_sha256='b' * 64, environment={'python': '3.11'},
            fees={'buy': .003712, 'sell': .0088}, seed=41, fold=0)


def producer(counter):
    def compute(path):
        counter.append(True)
        body = b'{"test_only":true}'
        with zipfile.ZipFile(path, 'x') as archive:
            archive.writestr('fold-0000/case-0000/normal.json', body)
        return dict(records=[dict(member='fold-0000/case-0000/normal.json',
                                 sha256=hashlib.sha256(body).hexdigest())])
    return compute


def cache(tmp_path, **kwargs):
    return ReplayCache(tmp_path / 'private' / 'cache', private_root=tmp_path / 'private', **kwargs)


def test_verified_hit_avoids_all_compute_and_preserves_member_bytes(tmp_path):
    calls = []
    store = cache(tmp_path)
    first, reused = store.run(SPEC, tmp_path / 'first.zip', producer(calls))
    second, hit = store.run(SPEC, tmp_path / 'second.zip', lambda *_: pytest.fail('Cache hit recomputed'))
    assert first == second and not reused and hit and len(calls) == 1
    with zipfile.ZipFile(tmp_path / 'first.zip') as a, zipfile.ZipFile(tmp_path / 'second.zip') as b:
        assert a.namelist() == b.namelist() and all(a.read(n) == b.read(n) for n in a.namelist())


@pytest.mark.parametrize('key,value', [('raw_sha256', 'c' * 64), ('code_sha256', 'c' * 64),
    ('environment', {'python': '3.11.99'}), ('fees', {'buy': .004}), ('seed', 97), ('fold', 1)])
def test_any_computational_contract_change_is_a_miss(tmp_path, key, value):
    calls = []
    store = cache(tmp_path)
    store.run(SPEC, tmp_path / 'first.zip', producer(calls))
    _, hit = store.run(dict(SPEC, **{key: value}), tmp_path / 'changed.zip', producer(calls))
    assert not hit and len(calls) == 2


@pytest.mark.parametrize('error', [RuntimeError, KeyboardInterrupt])
def test_interruption_never_publishes_partial_work_and_lock_is_released(tmp_path, error):
    store = cache(tmp_path)
    def interrupted(path):
        with zipfile.ZipFile(path, 'x') as archive:
            archive.writestr('unfinished', 'partial')
        raise error('test interruption')
    with pytest.raises(error):
        store.run(SPEC, tmp_path / 'partial.zip', interrupted)
    assert not list(store.root.glob('*.zip'))
    calls = []
    _, hit = store.run(SPEC, tmp_path / 'restart.zip', producer(calls))
    assert len(calls) == 1 and not hit


def test_same_job_cannot_compute_twice_while_native_lock_is_held(tmp_path):
    store = cache(tmp_path)
    calls = []
    def compute(path):
        with pytest.raises(ValueError, match='busy'):
            store.run(SPEC, tmp_path / 'racing.zip', lambda *_: pytest.fail('Duplicate compute started'))
        return producer(calls)(path)
    store.run(SPEC, tmp_path / 'first.zip', compute)
    assert len(calls) == 1


@pytest.mark.parametrize('defect', ['payload', 'metadata', 'missing', 'extra', 'duplicate'])
def test_corrupt_checkpoint_is_not_recomputed_overwritten_or_used(tmp_path, defect):
    store = cache(tmp_path)
    store.run(SPEC, tmp_path / 'first.zip', producer([]))
    target = store.root / (identity(SPEC) + '.zip')
    with zipfile.ZipFile(target) as archive:
        values = {name: archive.read(name) for name in archive.namelist()}
    if defect == 'payload':
        values['fold-0000/case-0000/normal.json'] = b'changed'
    elif defect == 'metadata':
        meta = json.loads(values[META])
        meta['spec']['seed'] = 999
        values[META] = json.dumps(meta).encode()
    elif defect == 'missing':
        values.pop(RESULT)
    elif defect == 'extra':
        values['extra'] = b'extra'
    with zipfile.ZipFile(target, 'w') as archive:
        for name, body in values.items():
            archive.writestr(name, body)
        if defect == 'duplicate':
            with pytest.warns(UserWarning, match='Duplicate'):
                archive.writestr(META, values[META])
    original = target.read_bytes()
    with pytest.raises(ValueError):
        store.run(SPEC, tmp_path / 'second.zip', lambda *_: pytest.fail('Corruption silently recomputed'))
    assert target.read_bytes() == original and not (tmp_path / 'second.zip').exists()


def test_storage_budget_can_stop_work_without_computation(tmp_path):
    store = cache(tmp_path, disk_budget=1)
    store.root.mkdir(parents=True)
    (store.root / 'old.zip').write_bytes(b'xx')
    with pytest.raises(ValueError, match='budget exhausted'):
        store.run(SPEC, tmp_path / 'out.zip', lambda *_: pytest.fail('Over-budget compute started'))


def test_publication_failure_leaves_no_valid_checkpoint(tmp_path, monkeypatch):
    import research.replay_cache as module

    store = cache(tmp_path)
    def failure(*_):
        raise OSError('test publication failure')
    monkeypatch.setattr(module.os, 'link', failure)
    with pytest.raises(OSError):
        store.run(SPEC, tmp_path / 'out.zip', producer([]))
    assert not list(store.root.glob('*.zip')) and not list(store.root.glob('*.tmp'))


def test_cache_cannot_escape_private_root_or_overwrite_an_output(tmp_path):
    with pytest.raises(ValueError, match='private'):
        ReplayCache(tmp_path / 'public', private_root=tmp_path / 'private')
    output = tmp_path / 'original.zip'
    output.write_bytes(b'original')
    with pytest.raises(ValueError, match='new'):
        cache(tmp_path).run(SPEC, output, lambda *_: pytest.fail('Existing output was overwritten'))
    assert output.read_bytes() == b'original'
