"""تطبیق محیط در حال اجرا با فایل قفل پروژه؛ بدون اتصال شبکه یا نصب سراسری."""

import argparse
import hashlib
import json
import os
import sys
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


def inspect_environment(lock, marker, *, installed_version=version, python_minor=None, marker_environment=None):
    """شرط‌های سیستم‌عامل و نسخه دقیق بسته‌ها بررسی و تمام ناسازگاری‌ها گزارش می‌شوند."""
    lock, marker = Path(lock), Path(marker)
    problems = []
    python_minor = python_minor or sys.version_info[:2]
    if tuple(python_minor) != (3, 11):
        problems.append('Python 3.11 is required')
    if not lock.is_file():
        return dict(ready=False, dependencies_match=False, lock_sha256=None, problems=problems + ['Lock file missing'])
    content = lock.read_bytes().replace(b'\r\n', b'\n')
    identity = hashlib.sha256(content).hexdigest()
    try:
        from packaging.requirements import InvalidRequirement, Requirement
    except ImportError:
        return dict(ready=False, dependencies_match=False, lock_sha256=identity,
                    problems=problems + ['packaging is missing; run SETUP.bat'])
    count = 0
    for line in content.decode('utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            requirement = Requirement(line)
        except InvalidRequirement:
            problems.append('Unsupported lock line: ' + line)
            continue
        pins = list(requirement.specifier)
        if requirement.url or len(pins) != 1 or pins[0].operator != '==' or '*' in pins[0].version:
            problems.append('Requirement is not exactly pinned: ' + requirement.name)
            continue
        if requirement.marker and not requirement.marker.evaluate(environment=marker_environment):
            continue
        count += 1
        try:
            actual = installed_version(requirement.name)
            if not requirement.specifier.contains(actual, prereleases=True):
                problems.append(f'{requirement.name}: installed {actual}, required {requirement.specifier}')
        except PackageNotFoundError:
            problems.append('Package missing: ' + requirement.name)
    if not count:
        problems.append('No applicable pinned packages in lock')
    matches = not problems
    try:
        marker_matches = marker.is_file() and marker.read_text(encoding='ascii').strip() == identity
    except (OSError, UnicodeError):
        marker_matches = False
    if not marker_matches:
        problems.append('Installation lock receipt is absent or outdated')
    return dict(ready=matches and marker_matches, dependencies_match=matches, lock_sha256=identity,
                checked_packages=count, problems=problems)


def record_environment(lock, marker, **kwargs):
    """رسید فقط پس از تأیید همه نسخه‌های نصب‌شده مشمول این سیستم نوشته می‌شود."""
    report = inspect_environment(lock, marker, **kwargs)
    if not report['dependencies_match']:
        raise ValueError('; '.join(report['problems']))
    marker = Path(marker)
    marker.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='ascii', dir=marker.parent,
                                         prefix='.brs-lock.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(report['lock_sha256'] + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, marker)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return inspect_environment(lock, marker, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--record', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    lock = Path(__file__).resolve().parents[1] / 'requirements.lock.txt'
    marker = Path(sys.prefix) / '.brs-lock.sha256'
    try:
        report = record_environment(lock, marker) if args.record else inspect_environment(lock, marker)
    except (OSError, UnicodeError, ValueError) as exc:
        report = dict(ready=False, problems=[str(exc)])
    print(json.dumps(report, ensure_ascii=True))
    return 0 if report['ready'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
