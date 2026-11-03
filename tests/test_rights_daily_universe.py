import hashlib
import json

import pytest

from config import settings
from core.rights_daily_universe import assess_day, day_request
from core.timeutils import now_tehran_iso
from ops.rights_daily_universe import capture, create_plan, review


def observation(day, rows):
    content = json.dumps({'closingPriceDailyHistoryWithInstDetails': rows}, ensure_ascii=False).encode()
    now = now_tehran_iso()
    return dict(source='tsetmc', url=day_request(day), requested_date=day, http_status=200,
                requested_at=now, received_at=now,
                response_bytes=len(content), body_sha256=hashlib.sha256(content).hexdigest(), content=content)


def row(day=0, code=3362850293262749, symbol='دالبرح'):
    return dict(insCode=code, lVal18AFC=symbol, instrumentID='IRR1DALZ0101', dEven=day)


def test_candidate_day_unknown_is_retained_without_confirmation():
    result = assess_day('2014-06-08', observation('2014-06-08', [row(), row(code=22, symbol='الف')]))
    assert result['status'] == 'observed'
    assert result['native_date_verified'] is False
    assert result['historical_universe_complete'] is False
    assert result['candidates'][0]['ins_code'] == '3362850293262749'


def test_old_day_and_holdout_boundary():
    assert day_request('2008-03-10').endswith('20080310')
    with pytest.raises(ValueError):
        day_request(settings.RESEARCH_CONFIG['holdout_start'])
    with pytest.raises(ValueError):
        day_request('2008-02-30')


def test_invalid_day_duplicate_and_body_tamper_fail():
    with pytest.raises(ValueError):
        assess_day('2014-06-08', observation('2014-06-08', [row(day=20140609)]))
    with pytest.raises(ValueError):
        assess_day('2014-06-08', observation('2014-06-08', [row(), row()]))
    obs = observation('2014-06-08', [row()])
    obs['body_sha256'] = '0' * 64
    with pytest.raises(ValueError):
        assess_day('2014-06-08', obs)


def test_empty_not_proof_of_market_closure():
    result = assess_day('2014-06-08', observation('2014-06-08', []))
    assert result['status'] == 'observed_empty_unverified'
    assert not result['native_date_verified']


@pytest.mark.parametrize('bad_id', [123, 1.5, True, [], {}])
def test_non_string_instrument_id_rejected(bad_id):
    value = row()
    value['instrumentID'] = bad_id
    with pytest.raises(ValueError):
        assess_day('2014-06-08', observation('2014-06-08', [value]))


def test_capture_stops_and_resumes_preserving_identity_projection(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    folder = create_plan('2014-06-08', '2014-06-11', tmp_path / 'private' / 'case')

    class Fake:
        last_transport_evidence = None
        calls = []
        fail = True

        def get_historical_day_universe_observation(self, day):
            self.calls.append(day)
            if self.fail and day == '2014-06-09':
                return None
            return observation(day, [row()])

    fake = Fake()
    assert capture(fake, folder)['stop_reason'] == 'source_unavailable_or_invalid'
    assert fake.calls == ['2014-06-08', '2014-06-09']
    assert capture(fake, folder)['stop_reason'] == 'prior_failure_requires_explicit_retry'
    fake.fail = False
    assert capture(fake, folder, retry_failed=True)['stop_reason'] == 'plan_processed'
    result = review(folder)
    assert result['status_counts'] == {'observed': 3}
    assert result['candidate_codes'] == 1
    assert not result['historical_universe_complete']
    record = folder / 'observations' / '2014-06-08' / '0001.json'
    saved = json.loads(record.read_text(encoding='utf-8'))
    assert 'content_base64' not in saved['observation']
    assert saved['source_body_archived'] is False
    saved['identity_projection']['closingPriceDailyHistoryWithInstDetails'][0]['lVal18AFC'] = 'tampered'
    record.write_text(json.dumps(saved), encoding='utf-8')
    with pytest.raises(ValueError):
        review(folder)


def test_date_plan_rejects_crossing_holdout(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    with pytest.raises(ValueError):
        create_plan('2025-03-20', '2025-03-22', tmp_path / 'private' / 'wrong')


@pytest.mark.parametrize('response', ['unexpected', {'content': 'not bytes'}])
def test_invalid_provider_contract_can_be_reviewed(tmp_path, monkeypatch, response):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    folder = create_plan('2014-06-08', '2014-06-09', tmp_path / 'private' / 'bad')

    class Fake:
        last_transport_evidence = None

        def get_historical_day_universe_observation(self, day):
            return response

    assert capture(Fake(), folder)['stop_reason'] == 'source_unavailable_or_invalid'
    assert review(folder)['status_counts'] == {'invalid_response': 1}


def test_bad_receipt_chronology_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    folder = create_plan('2014-06-08', '2014-06-09', tmp_path / 'private' / 'time')

    class Fake:
        last_transport_evidence = None

        def get_historical_day_universe_observation(self, day):
            value = observation(day, [row()])
            value['requested_at'] = '2001-01-01T00:00:00+03:30'
            return value

    assert capture(Fake(), folder)['stop_reason'] == 'source_unavailable_or_invalid'
    assert review(folder)['status_counts'] == {'invalid_response': 1}
