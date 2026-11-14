"""Read-only integrity check for review ZIPs with a flat SHA-256 manifest."""

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path
from zipfile import BadZipFile, ZipFile

MAX_ENTRIES = 10_000
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def _name(value, *, directory=False):
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid archive entry name')
    check = value[:-1] if directory and value.endswith('/') else value
    if ('\\' in check or ':' in check or check.startswith('/')
            or any(part in ('', '.', '..') for part in check.split('/'))):
        raise ValueError('Unsafe or noncanonical archive entry name')
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest key')
        result[key] = value
    return result


def _file_hash(stream):
    stream.seek(0)
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(block)
    return digest.hexdigest()


def verify_bundle(path, *, manifest_name='manifest.json'):
    """Verify CRC and listed hashes without extracting or running bundle code."""
    _name(manifest_name)
    path = Path(path)
    with path.open('rb') as source:
        archive_hash = _file_hash(source)
        source.seek(0)
        with ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ENTRIES:
                raise ValueError('Archive entry limit exceeded')
            inventory = {}
            for entry in entries:
                _name(entry.filename, directory=entry.is_dir())
                if entry.filename in inventory:
                    raise ValueError('Duplicate archive entry')
                if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                    raise ValueError('Symlink or encrypted entries are unsupported')
                if entry.is_dir() and entry.file_size:
                    raise ValueError('Directory entries must not contain payload bytes')
                if entry.file_size > MAX_MEMBER_BYTES:
                    raise ValueError('Archive member limit exceeded')
                inventory[entry.filename] = entry
            if sum(e.file_size for e in entries) > MAX_TOTAL_BYTES:
                raise ValueError('Archive total size limit exceeded')
            files = {name: e for name, e in inventory.items() if not e.is_dir()}
            if manifest_name not in files or files[manifest_name].file_size > MAX_MANIFEST_BYTES:
                raise ValueError('Bounded manifest is missing')
            manifest = json.loads(archive.read(manifest_name).decode('utf-8-sig'), object_pairs_hook=_unique)
            if not isinstance(manifest, dict) or not manifest:
                raise ValueError('A nonempty flat SHA-256 manifest is required')
            for name, digest in manifest.items():
                _name(name)
                if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                    raise ValueError('Unsupported manifest shape or SHA-256 value')
            if manifest_name in manifest:
                raise ValueError('The manifest cannot hash itself')
            payload = set(files) - {manifest_name}
            if set(manifest) - payload:
                raise ValueError('Manifest lists missing payload files')
            total = 0
            for name in sorted(payload):
                digest = hashlib.sha256()
                size = 0
                with archive.open(name) as member:
                    for block in iter(lambda: member.read(1024 * 1024), b''):
                        size += len(block)
                        total += len(block)
                        if size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                            raise ValueError('Actual decompressed size exceeds limits')
                        digest.update(block)
                if size != files[name].file_size:
                    raise ValueError('Payload size differs from archive metadata')
                if name in manifest and digest.hexdigest() != manifest[name].lower():
                    raise ValueError('Payload SHA-256 mismatch: ' + name)
            uncovered = sorted(payload - set(manifest))
        if _file_hash(source) != archive_hash:
            raise ValueError('Archive changed while being verified')
    return dict(status='verified_partial_coverage' if uncovered else 'verified_complete',
                archive_sha256=archive_hash, manifest_name=manifest_name,
                listed_hashes_verified=len(manifest), payload_files=len(payload),
                unhashed_payload_files=uncovered, manifest_coverage_complete=not uncovered,
                crc_verified=True, extracted=False, contents_executed=False,
                source_authenticity_verified=False, profitability_verified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--manifest', default='manifest.json')
    parser.add_argument('--require-complete', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args(argv)
    try:
        result = verify_bundle(args.archive, manifest_name=args.manifest)
        exit_code = 2 if args.require_complete and not result['manifest_coverage_complete'] else 0
    except (OSError, ValueError, BadZipFile, RuntimeError, NotImplementedError) as error:
        result, exit_code = dict(status='failed', error=str(error)), 1
    if args.output is not None:
        root = Path(__file__).resolve().parents[4]
        target = args.output.resolve()
        if not target.is_relative_to((root / 'data/private').resolve()):
            parser.error('Output must be inside this repository data/private directory')
        with target.open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps(result))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
