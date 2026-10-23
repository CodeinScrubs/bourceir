"""Synthetic state observations only; source semantics and execution stay unknown."""

import copy
import hashlib
import json

import pytest

from core.instrument_states import FALSE_FLAGS, MAX_BYTES, MAX_ROWS, parse_state_observation, state_request

CODE = '12345678901234567'
DAY = '2024-01-03'
NOW = '2024-01-03T14:00:00+03:30'


def row(**changes):
    return dict(idn=0, dEven=20240103, hEven=90000, insCode=CODE, lVal18AFC=None,
        lVal30=None, cEtaval='A ', realHeven=90000, underSupervision=0, cEtavalTitle=None) | changes


def observation(rows=None, *, body=None, **changes):
    if body is None:
        body = json.dumps({'instrumentState': [row()] if rows is None else rows}).encode()
    return dict(source='tsetmc', ins_code=CODE, requested_date=DAY,
        url=state_request(CODE, DAY)[1], requested_at=NOW, received_at=NOW, content=body,
        response_bytes=len(body), body_sha256=hashlib.sha256(body).hexdigest()) | changes


def parse(source):
    return parse_state_observation(source, CODE, DAY)


def test_request_uses_exact_validated_official_path():
    assert state_request(int(CODE), DAY) == (CODE,
        'https://cdn.tsetmc.com/api/MarketData/GetInstrumentState/12345678901234567/20240103')


@pytest.mark.parametrize('code,day', [(True, DAY), (False, DAY), (0, DAY), ('01', DAY),
    ('1/2', DAY), ('1?x=y', DAY), ('۱', DAY), ('1'*31, DAY), (1.0, DAY), (CODE, None),
    (CODE, True), (CODE, '20240103'), (CODE, '2024-1-03'), (CODE, '2024-02-30')])
def test_invalid_request_never_constructs_a_url(code, day):
    with pytest.raises(ValueError):
        state_request(code, day)


def test_verified_native_identity_still_proves_no_state_semantics_or_tradability():
    result = parse(observation())
    assert result['status'] == 'observed_states'
    assert result['evidence_grade'] == 'response_matches_request'
    assert result['native_identity_verified'] and result['native_date_verified'] and result['native_record_verified']
    assert result['requested_date'] == DAY and result['requested_date_basis'] == 'request_url'
    assert result['session_state'] is None and result['suspension_cause'] is None
    assert result['original_publication_time'] is None
    assert all(result[key] is False for key in FALSE_FLAGS)
    item = result['rows'][0]
    assert item['state_code_raw'] == 'A ' and item['state_code'] == 'A'
    assert item['code_recognition'] == 'unmapped' and item['state_meaning'] is None
    assert not item['state_semantics_verified'] and not item['tradability_verified']
    assert item['clocks']['hEven']['plausible_hhmmss']
    assert item['transition_timestamp'] is None


def test_unknown_state_code_is_preserved_without_silent_open_or_closed_mapping():
    result = parse(observation([row(cEtaval=' ZZ-new ')]))
    assert result['rows'][0]['state_code'] == 'ZZ-new'
    assert result['rows'][0]['code_recognition'] == 'unmapped'
    assert result['session_state'] is None and not result['tradability_verified']


@pytest.mark.parametrize('value', [None, 0, '0'])
@pytest.mark.parametrize('field', ['insCode', 'dEven'])
def test_native_absence_or_zero_is_request_url_evidence_only(field, value):
    result = parse(observation([row(**{field: value})]))
    assert result['status'] == 'request_url_only' and not result['native_record_verified']
    assert result['rows'][0]['evidence_grade'] == 'request_url_only'
    assert not result['rows'][0]['native_identity_verified' if field == 'insCode' else 'native_date_verified']
    assert result['rows'][0]['raw'][field] == value


def test_absent_native_fields_are_not_filled_from_request():
    raw = row()
    del raw['insCode'], raw['dEven'], raw['hEven'], raw['realHeven']
    result = parse(observation([raw]))
    assert result['status'] == 'request_url_only'
    assert 'insCode' not in result['rows'][0]['raw'] and 'dEven' not in result['rows'][0]['raw']
    assert result['rows'][0]['clocks']['hEven']['missing']
    assert result['rows'][0]['clocks_differ'] is None


def test_source_order_zero_and_sentinel_clocks_are_not_an_event_timeline():
    raw_rows = [row(insCode='0', dEven=0, hEven=1, realHeven=100843),
                row(hEven=114018, realHeven=114018, cEtaval='IS'),
                row(hEven=114010, realHeven=114010, cEtaval='I '),
                row(hEven=0, realHeven=0)]
    source = observation(raw_rows)
    before = copy.deepcopy(source)
    result = parse(source)
    assert source == before
    assert result['status'] == 'request_url_only' and result['source_order_preserved']
    assert [item['raw'] for item in result['rows']] == raw_rows
    assert [item['source_index'] for item in result['rows']] == [0, 1, 2, 3]
    sentinel = result['rows'][0]['clocks']['hEven']
    assert sentinel['raw'] == 1 and sentinel['sentinel_candidate'] and not sentinel['plausible_hhmmss']
    assert sentinel['transition_timestamp'] is None
    assert result['rows'][0]['clocks_differ'] is True
    assert result['rows'][3]['clocks']['hEven']['zero']
    assert not result['rows'][3]['clocks']['hEven']['plausible_hhmmss']
    assert not result['transition_order_verified'] and not result['precise_transition_clock_verified']


def test_equal_clocks_are_not_verified_effective_transition_times():
    result = parse(observation([row(hEven=235959, realHeven=235959)]))
    item = result['rows'][0]
    assert item['clocks_differ'] is False
    assert item['clocks']['hEven']['plausible_hhmmss']
    assert item['clocks']['hEven']['transition_timestamp'] is None
    assert not result['precise_transition_clock_verified']


def test_exact_duplicate_rows_remain_source_observations_instead_of_disappearing():
    raw = row()
    result = parse(observation([raw, raw, row(cEtaval='X')]))
    assert result['row_count'] == 3 and result['duplicate_source_indices'] == [1]
    assert [item['source_index'] for item in result['rows']] == [0, 1, 2]
    assert result['rows'][0]['raw'] == result['rows'][1]['raw']
    assert not result['full_session_coverage_verified']


@pytest.mark.parametrize('change', [dict(insCode='2'), dict(insCode=2), dict(dEven=20240102),
    dict(dEven='20240102'), dict(insCode='2', dEven=20240102)])
def test_explicit_native_conflict_invalidates_entire_result_but_retains_diagnostics(change):
    rows = [row(), row(**change), row(insCode='0', dEven=0)]
    result = parse(observation(rows))
    assert result['status'] == 'source_conflict' and result['evidence_grade'] == 'source_conflict'
    assert result['session_state'] is None and not result['native_record_verified']
    assert not result['usable_for_session_reconstruction']
    assert [item['raw'] for item in result['rows']] == rows
    assert result['source_conflicts'][0]['source_index'] == 1


def test_exact_large_native_integer_identity_does_not_round_through_binary64():
    result = parse(observation([row(insCode=int(CODE))]))
    assert result['native_identity_verified']
    assert result['rows'][0]['raw']['insCode'] == int(CODE)


def test_empty_response_has_no_native_or_open_session_attestation():
    result = parse(observation([]))
    assert result['status'] == 'observed_empty' and result['rows'] == []
    assert not result['native_record_verified'] and not result['native_identity_verified']
    assert not result['native_date_verified'] and result['session_state'] is None
    assert all(result[key] is False for key in FALSE_FLAGS)


@pytest.mark.parametrize('field,value', [
    ('insCode', True), ('insCode', False), ('insCode', ''), ('insCode', '00'), ('insCode', 'bad'),
    ('insCode', 1.0), ('insCode', -1), ('insCode', '1'*31),
    ('dEven', True), ('dEven', '20240230'), ('dEven', ''), ('dEven', 20240103.0),
    ('dEven', '2024-01-03'), ('dEven', -1), ('dEven', '00'),
    ('hEven', True), ('hEven', -1), ('hEven', '90000'), ('hEven', 90000.0),
    ('hEven', 240000), ('hEven', 125960), ('hEven', 126000), ('hEven', 999999),
    ('realHeven', False), ('realHeven', 240000), ('realHeven', 2**53),
    ('idn', -1), ('idn', 2**53), ('idn', True), ('underSupervision', False),
    ('cEtaval', None), ('cEtaval', ''), ('cEtaval', '  '), ('cEtaval', 'x'*33),
    ('cEtaval', 'A\n'), ('cEtaval', {'nested': 'A'}), ('lVal30', 1), ('lVal18AFC', 'x'*257),
    ('cEtavalTitle', True), ('extra_field', None),
])
def test_malformed_native_fields_do_not_degrade_to_request_only_or_empty(field, value):
    with pytest.raises(ValueError):
        parse(observation([row(**{field: value})]))


@pytest.mark.parametrize('field,value', [('source', 'other'), ('ins_code', int(CODE)),
    ('ins_code', True), ('url', 'https://example.invalid'), ('url', state_request(CODE, DAY)[1]+'?x=1'),
    ('requested_date', '2024-01-02'), ('requested_date', True), ('response_bytes', True),
    ('response_bytes', float('nan')), ('body_sha256', '0'*64), ('body_sha256', True),
    ('requested_at', True), ('requested_at', '2024-01-03T14:00:00'),
    ('received_at', '2024-01-03T13:59:59+03:30'), ('received_at', '2024-01-02T14:00:00+03:30'),
    ('requested_at', '0001-01-01T00:00:00+23:59'),
    ('received_at', '9999-12-31T23:59:59-23:59'), ('observation_kind', 'price'),
    ('observation_kind', True), ('content', bytearray(b'{}'))])
def test_wrapper_contract_rejects_wrong_identity_hash_clocks_and_types(field, value):
    with pytest.raises(ValueError):
        parse(observation(**{field: value}))


def test_optional_kind_is_exact_and_response_utc_clock_is_normalized():
    result = parse(observation(observation_kind='instrument_state',
        requested_at='2024-01-03T10:30:00+00:00', received_at='2024-01-03T10:30:00+00:00'))
    assert result['requested_at'] == NOW and result['received_at'] == NOW


def test_future_requested_date_and_missing_timing_envelope_are_rejected():
    source = observation(requested_at='2024-01-02T14:00:00+03:30', received_at='2024-01-02T14:01:00+03:30')
    with pytest.raises(ValueError):
        parse(source)
    for kwargs in ({'requested_after': NOW}, {'received_by': NOW},
                   {'requested_after': '2024-01-03T14:00:01+03:30', 'received_by': NOW},
                   {'requested_after': NOW, 'received_by': '2024-01-03T13:59:59+03:30'}):
        with pytest.raises(ValueError):
            parse_state_observation(observation(), CODE, DAY, **kwargs)
    assert parse_state_observation(observation(), CODE, DAY, requested_after=NOW, received_by=NOW)['row_count'] == 1


@pytest.mark.parametrize('body', [b'', b'[]', b'{}', b'<html>captcha</html>', b'\xff', b'x'*(MAX_BYTES+1),
    b'{"instrumentState":null}', b'{"instrumentState":{}}', b'{"instrumentState":[null]}',
    b'{"instrumentState":[true]}', b'{"instrumentState":[{}]}',
    b'{"instrumentState":[],"instrumentState":[]}',
    b'{"instrumentState":[{"cEtaval":"A","cEtaval":"I"}]}',
    b'{"instrumentState":[],"unused":NaN}', b'{"instrumentState":[],"unused":Infinity}',
    b'{"instrumentState":[],"unused":1e999}', b'{"instrumentState":[],"unused":1e-999}',
    b'{"instrumentState":[],"unused":1234567890123456789012345678901}',
    b'{"instrumentState":[],"unused":true}', b'{"instrumentState":[],"unused":null}',
    b'['*1500+b'0'+b']'*1500,
    b'{"instrumentState":[],"unused":'+b'['*12+b'0'+b']'*12+b'}'],
    ids=['empty', 'array', 'missing-key', 'html', 'encoding', 'oversize', 'null-rows', 'object-rows',
         'null-row', 'bool-row', 'empty-row', 'duplicate-root-key', 'duplicate-row-key', 'nan',
         'infinity', 'overflow', 'underflow', 'oversize-integer', 'extra-bool', 'extra-null',
         'parser-recursion', 'contract-depth'])
def test_bad_json_and_unsupported_structure_raise_valueerror(body):
    with pytest.raises(ValueError):
        parse(observation(body=body))


def test_bounded_row_count_and_bytes_are_enforced_separately():
    body = json.dumps({'instrumentState': [{'cEtaval': 'X'}]*(MAX_ROWS+1)}).encode()
    assert len(body) < MAX_BYTES
    with pytest.raises(ValueError):
        parse(observation(body=body))
    result = parse(observation([{'cEtaval': 'X'}]*MAX_ROWS))
    assert result['row_count'] == MAX_ROWS and result['status'] == 'request_url_only'


def test_valid_utf8_bom_and_preserved_nullable_metadata():
    raw = row(lVal18AFC='نماد', lVal30='شرکت', cEtavalTitle='عنوان خام')
    source = observation([raw])
    source = observation(body=b'\xef\xbb\xbf'+source['content'])
    assert parse(source)['rows'][0]['raw'] == raw


@pytest.mark.parametrize('field', ['cEtaval', 'cEtavalTitle', 'lVal18AFC', 'lVal30'])
@pytest.mark.parametrize('value', ['\ud800', '\udfff', 'before\ud800after'],
                         ids=['lone-high-surrogate', 'lone-low-surrogate', 'embedded-surrogate'])
def test_escaped_lone_surrogates_cannot_break_later_utf8_evidence_writes(field, value):
    # The input bytes are valid UTF-8 JSON; its decoded string is not Unicode scalar text.
    source = observation([row(**{field: value})])
    with pytest.raises(ValueError, match='یونیکد'):
        parse(source)


@pytest.mark.parametrize('body', [
    b'{"instrumentState":[],"unused":{"nested":"\\ud800"}}',
    b'{"instrumentState":[],"\\udfff":null}',
], ids=['nested-string', 'object-key'])
def test_unrecognized_nested_strings_and_keys_are_also_unicode_scalar_checked(body):
    with pytest.raises(ValueError, match='یونیکد'):
        parse(observation(body=body))


def test_valid_surrogate_pair_decodes_to_scalar_and_can_be_archived_as_utf8():
    source = observation([row(cEtavalTitle='عنوان \U0001f600')])
    assert b'\\ud83d\\ude00' in source['content']
    result = parse(source)
    encoded = json.dumps(result, ensure_ascii=False).encode('utf-8')
    assert json.loads(encoded)['rows'][0]['raw']['cEtavalTitle'] == 'عنوان \U0001f600'


def test_output_cannot_mutate_source_and_no_observation_is_invented():
    source = observation()
    result = parse(source)
    result['rows'][0]['raw']['cEtaval'] = 'X'
    assert parse(source)['rows'][0]['raw']['cEtaval'] == 'A '
    for missing in (None, [], 'unavailable'):
        with pytest.raises(ValueError):
            parse(missing)
