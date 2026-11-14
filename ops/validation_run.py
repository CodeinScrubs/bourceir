"""One fresh offline pytest run, with atomic and unambiguous terminal receipts."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from config import settings
from core.timeutils import now_tehran_iso


def write_state(path, value):
    """Sync complete receipt contents before publishing the replacement."""
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        deadline = time.monotonic() + 10
        while True:
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
    finally:
        temporary.unlink(missing_ok=True)


def source_manifest(root):
    """Bind checks to source, tests, schemas and dependency/runner configuration."""
    result = {}
    for name in ('ai', 'backtest', 'config', 'core', 'dashboard', 'data_collector', 'database', 'features',
                 'ops', 'research', 'scripts', 'tests', '.github'):
        folder = root / name
        for path in sorted(folder.rglob('*')):
            if (path.is_file() and not {'__pycache__', '.timing-output', '.pytest_cache'}.intersection(path.parts)
                    and path.suffix in {'.py', '.sql', '.yaml', '.yml', '.json', '.mjs', '.js',
                                        '.ps1', '.cs', '.txt', '.html', '.css', '.csv', '.parquet'}):
                result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    for pattern in ('*.py', '*.toml', 'requirements*.txt'):
        for path in sorted(root.glob(pattern)):
            result[path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def assess_pytest(expected, report, exit_code, *, source_unchanged):
    """Completion never means pass. Missing/duplicate/unexpected tests fail closed."""
    executed, counts = [], Counter()
    for case in ET.parse(report).findall('.//testcase'):
        parts = case.get('classname', '').split('.')
        if len(parts) < 2 or parts[0] != 'tests':
            node = '<unrecognized>:' + case.get('name', '')
        else:
            node = f'tests/{parts[1]}.py::' + '::'.join([*parts[2:], case.get('name', '')])
        executed.append(node)
        counts[next((k for k in ('failure', 'error', 'skipped') if case.find(k) is not None), 'passed')] += 1
    missing, unexpected = sorted(set(expected)-set(executed)), sorted(set(executed)-set(expected))
    duplicates = sorted(k for k, n in Counter(executed).items() if n > 1)
    passed = (exit_code == 0 and not counts['failure'] and not counts['error'] and bool(expected)
              and not missing and not unexpected and not duplicates and source_unchanged)
    return dict(status='passed' if passed else 'failed', execution_complete=True, pytest_exit_code=exit_code,
        source_unchanged=source_unchanged, counts=dict(counts),
        coverage=dict(collected=len(expected), executed=len(executed), unique_executed=len(set(executed)),
                      missing=missing, unexpected=unexpected, duplicates=duplicates))


def run_offline_checks(output):
    root = settings.BASE_DIR.resolve()
    output = Path(output).resolve()
    if not output.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Validation receipts must remain in data/private')
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    state = dict(status='running', execution_complete=False, started_at=now_tehran_iso(),
                 command=None, stage='collection')
    write_state(output/'state.json', state)
    env = dict(os.environ, PYTHONUTF8='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    try:
        before = source_manifest(root)
        write_state(output/'sources-before.json', before)
        collect = subprocess.run([sys.executable, '-m', 'pytest', 'tests/', '--collect-only', '-m', 'not network',
            '-o', 'addopts=', '-q'], cwd=root, env=env, capture_output=True, text=True, encoding='utf-8')
        (output/'collection.log').write_text(collect.stdout + collect.stderr, encoding='utf-8')
        nodes = [s.strip() for s in collect.stdout.splitlines() if s.startswith('tests/') and '::' in s]
        if collect.returncode or not nodes or len(nodes) != len(set(nodes)):
            raise RuntimeError('Offline collection did not produce a unique complete node list')
        write_state(output/'collection.json', dict(nodes=nodes, count=len(nodes)))
        temporary = Path(tempfile.mkdtemp(prefix='brs-offline-validation-'))
        command = [sys.executable, '-m', 'pytest', 'tests/', '-m', 'not network', '-o', 'addopts=', '-q',
            '--tb=short', '--durations=25', '--junitxml=' + str(output/'pytest.xml'), '--basetemp=' + str(temporary/'pytest')]
        state.update(stage='pytest', command=command, temporary_root=str(temporary))
        write_state(output/'state.json', state)
        with (output/'pytest.log').open('w', encoding='utf-8') as stream:
            completed = subprocess.run(command, cwd=root, env=env, stdout=stream, stderr=subprocess.STDOUT)
        after = source_manifest(root)
        write_state(output/'sources-after.json', after)
        result = assess_pytest(nodes, output/'pytest.xml', completed.returncode, source_unchanged=before == after)
        result.update(started_at=state['started_at'], finished_at=now_tehran_iso(), command=command,
            elapsed_seconds=round(time.monotonic()-started, 2), temporary_root=str(temporary))
    except BaseException as error:
        result = dict(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'error',
            execution_complete=False, started_at=state['started_at'], finished_at=now_tehran_iso(),
            elapsed_seconds=round(time.monotonic()-started, 2), error=f'{type(error).__name__}: {error}')
        write_state(output/'result.json', result)
        write_state(output/'state.json', result)
        raise
    write_state(output/'result.json', result)
    write_state(output/'state.json', result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    result = run_offline_checks(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
