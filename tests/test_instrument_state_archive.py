"""Adversarial evidence archives; synthetic public responses and temporary files only."""

import base64
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import data_collector.instrument_states as archives

CODE = '12345678901234567'
DAY = '2024-01-03'
NOW = '2024-01-03T14:00:00+03:30'
BEFORE = '2024-01-03T13:59:59+03:30'
AFTER = '2024-01-03T14:00:01+03:30'


def task(code=CODE, **changes):
    return dict(symbol='آزمون', ins_code=code, date=DAY, instrument_type_code=300) | changes


def observation(code=CODE, day=DAY, *, body=None, rows=None, **changes):
    if body is None:
        native = dict(insCode=code, dEven=int(day.replace('-', '')), hEven=90000,
                      realHeven=90000, cEtaval='A ')
        body = json.dumps({'instrumentState': [native] if rows is None else rows}).encode()
    return dict(source='tsetmc', ins_code=code, requested_date=day,
        url=f'https://cdn.tsetmc.com/api/MarketData/GetInstrumentState/{code}/{day.replace("-", "")}',
        requested_at=NOW, received_at=NOW, response_bytes=len(body),
        body_sha256=hashlib.sha256(body).hexdigest(), content=body,
        observation_kind='instrument_state') | changes


class Provider:
    supports_dated_instrument_states = True

    def __init__(self, transform=None):
        self.calls = []
        self.transform = transform

    def get_dated_instrument_state_observation(self, code, day):
        self.calls.append((code, day))
        source = observation(code, day)
        return self.transform(source) if self.transform else source


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(archives, 'now_tehran_iso', lambda: NOW)


def load(folder, name):
    return json.loads((Path(folder)/name).read_text(encoding='utf-8'))


def write(folder, name, value, *, rehash=False):
    target = Path(folder)/name
    target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    if rehash:
        result = load(folder, 'result.json')
        result['artifacts_sha256'][name] = hashlib.sha256(target.read_bytes()).hexdigest()
        write(folder, 'result.json', result)


def sealed(tmp_path, *, provider=None, tasks=None):
    archive = archives.InstrumentStateArchive(tmp_path, [task()] if tasks is None else tasks)
    archive.capture(provider or Provider())
    archive.finish()
    return archive.folder


def test_plan_is_written_before_first_request_and_detached_from_caller(tmp_path):
    tasks = [task()]
    archive = archives.InstrumentStateArchive(tmp_path, tasks)
    tasks[0]['ins_code'] = '999'
    tasks.append(task('2'))

    def inspect(source):
        plan = load(archive.folder, 'plan.json')
        assert plan['tasks'] == [task()]
        assert plan['registered_at'] == NOW
        assert not (archive.folder/'result.json').exists()
        assert all((archive.folder/'code'/name).is_file() for name in plan['code'])
        assert source['ins_code'] == CODE
        return source

    provider = Provider(inspect)
    archive.capture(provider)
    archive.finish()
    assert provider.calls == [(CODE, DAY)]
    assert archives.review_archive(archive.folder)['observations'] == 1


def test_capture_and_finish_are_one_shot_and_separate_runs_do_not_overwrite(tmp_path):
    archive = archives.InstrumentStateArchive(tmp_path, [task()])
    provider = Provider()
    archive.capture(provider)
    with pytest.raises(ValueError):
        archive.capture(provider)
    archive.finish()
    original = (archive.folder/'result.json').read_bytes()
    with pytest.raises(ValueError):
        archive.finish()
    assert (archive.folder/'result.json').read_bytes() == original
    assert len(provider.calls) == 1
    other = sealed(tmp_path)
    assert other != archive.folder
    assert archives.review_archive(other)['observations'] == 1


def test_finish_before_capture_does_not_produce_a_result(tmp_path):
    archive = archives.InstrumentStateArchive(tmp_path, [task()])
    with pytest.raises(ValueError):
        archive.finish()
    assert not (archive.folder/'result.json').exists()


@pytest.mark.parametrize('change', ['saved-plan', 'in-memory-tasks'])
def test_changed_preregistered_plan_blocks_every_provider_call(tmp_path, change):
    archive = archives.InstrumentStateArchive(tmp_path, [task()])
    if change == 'saved-plan':
        plan = load(archive.folder, 'plan.json')
        plan['tasks'][0]['ins_code'] = '2'
        write(archive.folder, 'plan.json', plan)
    else:
        archive.tasks[0]['ins_code'] = '2'
    provider = Provider()
    with pytest.raises(ValueError):
        archive.capture(provider)
    assert not provider.calls
    assert not list(archive.folder.glob('observation-*.json'))


def test_plan_change_after_capture_cannot_be_sealed(tmp_path):
    archive = archives.InstrumentStateArchive(tmp_path, [task()])
    archive.capture(Provider())
    plan = load(archive.folder, 'plan.json')
    plan['registered_at'] = BEFORE
    write(archive.folder, 'plan.json', plan)
    with pytest.raises(ValueError):
        archive.finish()
    assert not (archive.folder/'result.json').exists()


@pytest.mark.parametrize('tasks', [[], [task(), task()], [task('0')], [task('01')],
    [task(instrument_type_code=True)], [task(instrument_type_code=305)], [task(symbol='آزمونح1')],
    [task(symbol='كیما')], [task(date='2099-01-01')], [task()]*101])
def test_invalid_or_duplicate_plan_rejected_before_creating_archive(tmp_path, tasks):
    with pytest.raises(ValueError):
        archives.InstrumentStateArchive(tmp_path, tasks)
    assert not list(tmp_path.iterdir())


def test_exact_http_bytes_replay_without_reserializing_or_archiving_headers(tmp_path):
    body = b'\xef\xbb\xbf{ "instrumentState" : [ {"cEtaval":" ZZ-new ","hEven":1,"realHeven":100843} ] }\n'
    folder = sealed(tmp_path, provider=Provider(lambda _: observation(body=body,
        headers={'Authorization': 'synthetic-secret-never-save'}, token='another-synthetic-secret')))
    record = load(folder, 'observation-000.json')
    assert base64.b64decode(record['source']['body_base64']) == body
    assert record['source']['body_sha256'] == hashlib.sha256(body).hexdigest()
    text = (folder/'observation-000.json').read_text(encoding='utf-8')
    assert 'synthetic-secret' not in text and 'Authorization' not in text
    parsed = record['parsed']
    assert parsed['status'] == 'request_url_only'
    assert parsed['rows'][0]['state_code_raw'] == ' ZZ-new '
    assert parsed['rows'][0]['state_meaning'] is None
    assert parsed['rows'][0]['clocks']['hEven']['raw'] == 1
    assert parsed['rows'][0]['clocks']['realHeven']['raw'] == 100843
    assert parsed['session_state'] is None
    assert not parsed['full_session_coverage_verified']
    reviewed = archives.review_archive(folder)
    assert reviewed['byte_replay_verified']
    assert reviewed['statuses'] == {'request_url_only': 1}
    assert reviewed['accepted_for_features'] is reviewed['accepted_for_signals'] is False


def test_maximum_valid_row_count_roundtrips_expanded_provenance(tmp_path):
    rows = [{'cEtaval': 'A'} for _ in range(4096)]
    source = observation(rows=rows)
    assert len(source['content']) < archives.MAX_BYTES
    folder = sealed(tmp_path, provider=Provider(lambda _: source))
    record = load(folder, 'observation-000.json')
    assert record['parsed']['row_count'] == 4096
    assert len(record['parsed']['rows']) == 4096
    assert record['parsed']['duplicate_source_indices'] == list(range(1, 4096))
    assert base64.b64decode(record['source']['body_base64']) == source['content']
    assert archives.review_archive(folder)['statuses'] == {'request_url_only': 1}


@pytest.mark.parametrize('mode', ['unsupported', 'exception', 'none'])
def test_all_source_failures_retain_every_planned_row(tmp_path, mode):
    def fail(_):
        if mode == 'exception':
            raise RuntimeError('synthetic-secret-not-for-error-log')
        return None

    provider = Provider(fail)
    if mode == 'unsupported':
        provider.supports_dated_instrument_states = False
    tasks = [task(), task('2'), task('3')]
    folder = sealed(tmp_path, provider=provider, tasks=tasks)
    reviewed = archives.review_archive(folder)
    assert reviewed['observations'] == 3
    assert reviewed['statuses'] == {'source_unavailable': 3}
    assert len(provider.calls) == (0 if mode == 'unsupported' else 3)
    for i, expected in enumerate(tasks):
        record = load(folder, f'observation-{i:03d}.json')
        assert record['task'] == expected and record['source'] is None
        assert record['parsed']['rows'] == []
        assert not record['parsed']['accepted_for_signals']
        assert 'synthetic-secret' not in (folder/f'observation-{i:03d}.json').read_text()


@pytest.mark.parametrize('body', [b'not json', b'\xff', b'[]', b'{"instrumentState":[],"instrumentState":[]}',
    b'['*12000+b'0'+b']'*12000, b'{"instrumentState":NaN}', b'{"instrumentState":1e999}',
    b'x'*(archives.MAX_BYTES+1)], ids=['invalid', 'encoding', 'root', 'duplicate-key', 'deep', 'nan', 'overflow', 'oversize'])
def test_bad_payload_does_not_crash_or_drop_remaining_batch(tmp_path, body):
    provider = Provider(lambda source: observation(body=body) if source['ins_code'] == CODE else source)
    folder = sealed(tmp_path, provider=provider, tasks=[task(), task('2')])
    reviewed = archives.review_archive(folder)
    assert reviewed['statuses'] == {'rejected': 1, 'observed_states': 1}
    assert provider.calls == [(CODE, DAY), ('2', DAY)]
    saved = load(folder, 'observation-000.json')['source']['body_base64']
    if len(body) <= archives.MAX_BYTES:
        assert base64.b64decode(saved) == body


@pytest.mark.parametrize('wrapper', [[], 'bad', 12, {'content': object()}, {'content': b'{}', 'ins_code': 'x'*1_100_000}])
def test_malformed_wrapper_is_bounded_and_does_not_abort_remaining_batch(tmp_path, wrapper):
    provider = Provider(lambda source: wrapper if source['ins_code'] == CODE else source)
    folder = sealed(tmp_path, provider=provider, tasks=[task(), task('2')])
    assert archives.review_archive(folder)['statuses'] == {'rejected': 1, 'observed_states': 1}
    assert len(provider.calls) == 2


@pytest.mark.parametrize('field,value', [('ins_code', 'x'*(archives.MAX_FILE_BYTES+1)),
    ('response_bytes', 10**5000), ('ins_code', '\ud800')],
    ids=['oversized-text', 'oversized-integer', 'non-scalar-unicode'])
def test_hostile_metadata_size_does_not_abort_remaining_batch(tmp_path, field, value):
    def hostile(source):
        return source | {field: value} if source['ins_code'] == CODE else source

    provider = Provider(hostile)
    folder = sealed(tmp_path, provider=provider, tasks=[task(), task('2')])
    reviewed = archives.review_archive(folder)
    assert reviewed['observations'] == 2 and reviewed['statuses']['observed_states'] == 1
    assert len(provider.calls) == 2
    first = load(folder, 'observation-000.json')
    assert first['parsed']['status'] in {'rejected', 'source_unavailable'}
    assert first['parsed']['accepted_for_signals'] is False


@pytest.mark.parametrize('field,value', [('requested_at', BEFORE), ('received_at', AFTER),
    ('requested_at', AFTER), ('received_at', BEFORE), ('requested_at', '2024-01-03T14:00:00'),
    ('response_bytes', True), ('body_sha256', '0'*64), ('ins_code', '2'), ('requested_date', '2024-01-02'),
    ('url', 'https://example.invalid/'), ('observation_kind', 'price')])
def test_wrapper_clock_and_identity_errors_are_archived_as_rejected(tmp_path, field, value):
    folder = sealed(tmp_path, provider=Provider(lambda source: source | {field: value}))
    assert archives.review_archive(folder)['statuses'] == {'rejected': 1}


@pytest.mark.parametrize('rows,status', [([], 'observed_empty'),
    ([{'cEtaval': 'A ', 'insCode': '0', 'dEven': 0}], 'request_url_only'),
    ([{'cEtaval': 'Unknown-new-code', 'insCode': CODE, 'dEven': 20240103}], 'observed_states'),
    ([{'cEtaval': 'A ', 'insCode': '999', 'dEven': 20240103}], 'source_conflict')])
def test_empty_unknown_and_conflicting_states_never_prove_a_session(tmp_path, rows, status):
    folder = sealed(tmp_path, provider=Provider(lambda _: observation(rows=rows)))
    parsed = load(folder, 'observation-000.json')['parsed']
    assert parsed['status'] == status and parsed['session_state'] is None
    for flag in ('full_session_coverage_verified', 'tradability_verified', 'usable_for_session_reconstruction',
                 'accepted_for_features', 'accepted_for_signals', 'profitability_proven'):
        assert parsed[flag] is False
    assert archives.review_archive(folder)['statuses'] == {status: 1}


@pytest.mark.parametrize('name,flag', [('plan.json', 'historical_membership_verified'),
    ('plan.json', 'model_selection'), ('plan.json', 'holdout_unsealed'),
    ('plan.json', 'accepted_for_signals'), ('plan.json', 'profitability_proven'),
    ('result.json', 'applied'), ('result.json', 'accepted_for_features'),
    ('result.json', 'accepted_for_signals'), ('result.json', 'profitability_proven')])
@pytest.mark.parametrize('forgery', [True, 0, None])
def test_false_acceptance_flags_must_be_exact_false_even_after_rehash(tmp_path, name, flag, forgery):
    folder = sealed(tmp_path)
    payload = load(folder, name)
    payload[flag] = forgery
    write(folder, name, payload, rehash=name != 'result.json')
    with pytest.raises(ValueError):
        archives.review_archive(folder)


@pytest.mark.parametrize('change', ['parsed', 'body', 'wrapper', 'task', 'attempted', 'completed', 'version'])
def test_record_tampering_cannot_pass_by_only_rehashing_artifacts(tmp_path, change):
    folder = sealed(tmp_path)
    record = load(folder, 'observation-000.json')
    if change == 'parsed':
        record['parsed']['accepted_for_signals'] = True
    elif change == 'body':
        record['source']['body_base64'] = base64.b64encode(b'{"instrumentState":[]}').decode()
    elif change == 'wrapper':
        record['source']['ins_code'] = '2'
    elif change == 'task':
        record['task']['ins_code'] = '2'
    elif change == 'attempted':
        record['attempted_at'] = BEFORE
    elif change == 'completed':
        record['completed_at'] = AFTER
    else:
        record['version'] = 'other-version'
    write(folder, 'observation-000.json', record, rehash=True)
    with pytest.raises(ValueError):
        archives.review_archive(folder)


def test_result_end_before_preregistration_fails(tmp_path):
    folder = sealed(tmp_path)
    result = load(folder, 'result.json')
    result['completed_at'] = BEFORE
    write(folder, 'result.json', result)
    with pytest.raises(ValueError):
        archives.review_archive(folder)


@pytest.mark.parametrize('name', ['plan.json', 'observation-000.json'])
def test_byte_changes_without_rehash_fail(tmp_path, name):
    folder = sealed(tmp_path)
    path = folder/name
    path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError):
        archives.review_archive(folder)


@pytest.mark.parametrize('change', ['missing-file', 'missing-inventory', 'extra-inventory', 'extra-file', 'traversal', 'absolute'])
def test_archive_inventory_is_exact_and_paths_cannot_escape(tmp_path, change):
    folder = sealed(tmp_path)
    result = load(folder, 'result.json')
    if change == 'missing-file':
        (folder/'observation-000.json').unlink()
    elif change == 'missing-inventory':
        del result['artifacts_sha256']['observation-000.json']
    elif change == 'extra-file':
        (folder/'observation-999.json').write_text('{}')
    else:
        extra = ('../outside.json' if change == 'traversal' else str(tmp_path/'outside.json')
                 if change == 'absolute' else 'observation-999.json')
        result['artifacts_sha256'][extra] = '0'*64
    write(folder, 'result.json', result)
    with pytest.raises(ValueError):
        archives.review_archive(folder)


@pytest.mark.parametrize('name', ['plan.json', 'result.json', 'observation-000.json'])
def test_oversized_archive_json_is_rejected_before_parsing(tmp_path, name):
    folder = sealed(tmp_path)
    (folder/name).write_bytes(b' '*(archives.MAX_FILE_BYTES+1))
    with pytest.raises(ValueError):
        archives.review_archive(folder)


@pytest.mark.parametrize('name', ['plan.json', 'result.json', 'observation-000.json'])
def test_duplicate_json_keys_in_archive_metadata_are_rejected(tmp_path, name):
    folder = sealed(tmp_path)
    original = (folder/name).read_text(encoding='utf-8')
    (folder/name).write_text('{"version":"forged",'+original[1:], encoding='utf-8')
    if name != 'result.json':
        result = load(folder, 'result.json')
        result['artifacts_sha256'][name] = hashlib.sha256((folder/name).read_bytes()).hexdigest()
        write(folder, 'result.json', result)
    with pytest.raises(ValueError):
        archives.review_archive(folder)


def test_code_snapshot_byte_mutation_is_rejected(tmp_path):
    folder = sealed(tmp_path)
    code = folder/'code'/'core'/'instrument_states.py'
    code.write_bytes(code.read_bytes()+b'\n# tampered\n')
    with pytest.raises(ValueError):
        archives.review_archive(folder)


def test_indirected_code_directory_cannot_escape_archive_root(tmp_path):
    folder = sealed(tmp_path/'archive')
    source, outside = folder/'code', tmp_path/'outside-code'
    assert source.resolve().is_relative_to(tmp_path.resolve())
    assert outside.resolve().is_relative_to(tmp_path.resolve())
    source.rename(outside)
    if os.name == 'nt':
        subprocess.run(['cmd', '/c', 'mklink', '/J', str(source), str(outside)], check=True, capture_output=True)
    else:
        source.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='مسیر'):
        archives.review_archive(folder)


def test_current_code_change_blocks_capture_completion_and_replay(tmp_path, monkeypatch):
    folder = sealed(tmp_path/'finished')
    pending = archives.InstrumentStateArchive(tmp_path/'pending', [task()])
    real_code = archives._code()
    changed = real_code | {'core/instrument_states.py': 'f'*64}
    monkeypatch.setattr(archives, '_code', lambda: changed)
    with pytest.raises(ValueError):
        archives.review_archive(folder)
    with pytest.raises(ValueError):
        pending.capture(Provider())
    assert not (pending.folder/'result.json').exists()
    with pytest.raises(ValueError):
        pending.finish()


def test_record_mutation_before_sealing_cannot_be_ratified(tmp_path):
    archive = archives.InstrumentStateArchive(tmp_path, [task()])
    archive.capture(Provider())
    record = load(archive.folder, 'observation-000.json')
    record['parsed']['status'] = 'observed_empty'
    write(archive.folder, 'observation-000.json', record)
    with pytest.raises(ValueError):
        archive.finish()
    assert not (archive.folder/'result.json').exists()


@pytest.mark.parametrize('argv', [[], ['--fetch'], ['--review', 'unused', '--fetch'],
    ['--review', 'unused', '--tasks', 'unused'], ['--help']])
def test_cli_requires_an_explicit_valid_action_without_network(argv, capsys):
    with pytest.raises(SystemExit) as exc:
        archives.main(argv)
    assert exc.value.code == (0 if argv == ['--help'] else 2)
    capsys.readouterr()
