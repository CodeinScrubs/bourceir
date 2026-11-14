"""Atomic, bounded fold checkpoints. Reuse is computation, not new evidence."""

import hashlib
import json
import os
import shutil
import time
import uuid
import zipfile
from contextlib import contextmanager
from pathlib import Path

from research.search_ledger import canonical, identity

VERSION = 'verified-fold-cache-v1'
MAX_MEMBER = 32 * 1024 * 1024
MAX_UNCOMPRESSED = 512 * 1024 * 1024
DEFAULT_DISK_BUDGET = 2 * 1024 * 1024 * 1024
META, RESULT = '__checkpoint__.json', '__result__.json'


def _json(body):
    def reject(value):
        raise ValueError('Nonfinite JSON in checkpoint: ' + value)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate JSON key in checkpoint')
            result[key] = value
        return result
    return json.loads(body, parse_constant=reject, object_pairs_hook=pairs)


@contextmanager
def _lock(path, wait_seconds=0.):
    """OS releases the lock on crash. A retained one-byte file is not a stale lease."""
    with path.open('a+b') as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        acquired = False
        try:
            deadline = time.monotonic() + wait_seconds
            while not acquired:
                try:
                    if os.name == 'nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise ValueError('Checkpoint is busy; no duplicate computation started') from error
                    time.sleep(.02)
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ReplayCache:
    def __init__(self, root, *, private_root, disk_budget=DEFAULT_DISK_BUDGET):
        self.root = Path(root).resolve()
        if (not self.root.is_relative_to(Path(private_root).resolve())
                or type(disk_budget) is not int or disk_budget < 1):
            raise ValueError('A private cache root and positive byte budget are required')
        self.disk_budget = disk_budget

    def _read(self, target, spec):
        with zipfile.ZipFile(target) as archive:
            info = archive.infolist()
            names = [row.filename for row in info]
            if (len(names) != len(set(names)) or any(row.file_size > MAX_MEMBER for row in info)
                    or sum(row.file_size for row in info) > MAX_UNCOMPRESSED
                    or META not in names or RESULT not in names):
                raise ValueError('Checkpoint archive denominator or size is invalid')
            meta = _json(archive.read(META))
            result_body = archive.read(RESULT)
            if (meta != dict(version=VERSION, spec=spec, key=identity(spec), result_sha256=hashlib.sha256(result_body).hexdigest(),
                             new_independent_evidence=False)):
                raise ValueError('Checkpoint identity differs from its complete input contract')
            partial = _json(result_body)
            rows = partial.get('records', [])
            members = [row['member'] for row in rows]
            if (not rows or len(set(members)) != len(members) or set(names) != {*members, META, RESULT}
                    or any(hashlib.sha256(archive.read(row['member'])).hexdigest() != row['sha256'] for row in rows)):
                raise ValueError('Checkpoint replay hashes or full denominator differ')
        return partial

    def run(self, spec, destination, compute):
        if not isinstance(spec, dict) or not spec or len(canonical(spec).encode()) > MAX_MEMBER:
            raise ValueError('An explicit bounded input contract is required')
        key = identity(spec)
        self.root.mkdir(parents=True, exist_ok=True)
        target = self.root / (key + '.zip')
        lock = self.root / (key + '.lock')
        if any(not path.resolve().is_relative_to(self.root) for path in (target, lock)):
            raise ValueError('Checkpoint symlink escapes its private root')
        destination = Path(destination)
        if destination.exists() or destination.resolve().is_relative_to(self.root):
            raise ValueError('Checkpoint output must be new and separate from the cache')
        with _lock(lock):
            if target.exists():
                partial = self._read(target, spec)
                self._copy_replays(target, destination, partial)
                return partial, True
            used = sum(path.stat().st_size for path in self.root.glob('*.zip'))
            if used >= self.disk_budget:
                raise ValueError('Checkpoint disk budget exhausted before computation')
            partial = compute(destination)
            body = canonical(partial).encode('utf-8')
            meta = dict(version=VERSION, spec=spec, key=key, result_sha256=hashlib.sha256(body).hexdigest(),
                        new_independent_evidence=False)
            temporary = self.root / (key + '.' + uuid.uuid4().hex + '.tmp')
            try:
                with zipfile.ZipFile(destination) as replay, zipfile.ZipFile(
                        temporary, 'x', compression=zipfile.ZIP_DEFLATED) as output:
                    for name in replay.namelist():
                        output.writestr(name, replay.read(name))
                    output.writestr(META, canonical(meta))
                    output.writestr(RESULT, body)
                self._read(temporary, spec)
                # Recheck after compute: another independent key may have completed meanwhile.
                with temporary.open('r+b') as stream:
                    stream.flush()
                    os.fsync(stream.fileno())
                publication_lock = self.root / '.publication.lock'
                if not publication_lock.resolve().is_relative_to(self.root):
                    raise ValueError('Checkpoint publication lock escapes its private root')
                with _lock(publication_lock, wait_seconds=5.):
                    used = sum(path.stat().st_size for path in self.root.glob('*.zip'))
                    if used + temporary.stat().st_size > self.disk_budget:
                        raise ValueError('Checkpoint would exceed its disk budget; no cache receipt published')
                    os.link(temporary, target)  # atomic publication, never replace an old checkpoint
            finally:
                temporary.unlink(missing_ok=True)
            return partial, False

    @staticmethod
    def _copy_replays(source, destination, partial):
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(
                destination, 'x', compression=zipfile.ZIP_DEFLATED) as output:
            for row in partial['records']:
                with original.open(row['member']) as incoming, output.open(row['member'], 'w') as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
