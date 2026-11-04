import hashlib
import json
from datetime import timedelta

import pytest

from config import settings
from core.rights_daily_retrospective import assess_day, day_request
from core.timeutils import now_tehran, now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from ops.rights_daily_retrospective import capture, create_plan, review


def obs(day):
    content = json.dumps({'closingPriceDailyHistoryWithInstDetails': [
        {'insCode': 42, 'lVal18AFC': 'ABCح', 'instrumentID': 'IRR1TEST0101',
         'dEven': 0, 'priceFirst': 975318642}]}).encode()
    now = now_tehran_iso()
    return dict(source='tsetmc', url=day_request(day), requested_date=day, http_status=200,
                requested_at=now, received_at=now, response_bytes=len(content),
                body_sha256=hashlib.sha256(content).hexdigest(), content=content)


def test_post_holdout_is_separate_and_future_rejected():
    assert day_request(settings.RESEARCH_CONFIG['holdout_start'])
    with pytest.raises(ValueError):
        day_request((now_tehran().date() + timedelta(days=1)).isoformat())
    with pytest.raises(ValueError):
        day_request('2014-06-08')
    with pytest.raises(ValueError):
        TsetmcProvider().get_retrospective_day_universe_observation(settings.RESEARCH_CONFIG['holdout_start'])


@pytest.mark.parametrize('bad_id', [123, 1.5, True, [], {}])
def test_non_string_instrument_id_rejected(bad_id):
    day = settings.RESEARCH_CONFIG['holdout_start']
    value = obs(day)
    payload = json.loads(value['content'])
    payload['closingPriceDailyHistoryWithInstDetails'][0]['instrumentID'] = bad_id
    value['content'] = json.dumps(payload).encode()
    value['response_bytes'] = len(value['content'])
    value['body_sha256'] = hashlib.sha256(value['content']).hexdigest()
    with pytest.raises(ValueError):
        assess_day(day, value)


def test_opt_in_and_projected_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path)
    day = (now_tehran().date() - timedelta(days=1)).isoformat()
    end = now_tehran().date().isoformat()
    folder = tmp_path / 'private' / 'retro'
    with pytest.raises(ValueError):
        create_plan(day, end, folder)
    create_plan(day, end, folder, retrospective_rights_only=True)

    class Fake:
        last_transport_evidence = None

        def get_retrospective_day_universe_observation(self, requested, *, retrospective_rights_only=False):
            assert retrospective_rights_only is True
            return obs(requested)

    assert capture(Fake(), folder)['stop_reason'] == 'plan_processed'
    result = review(folder)
    assert result['holdout_contaminated_for_rights_hypothesis'] is True
    assert result['candidate_codes'] == 1
    assert result['original_source_bodies_archived'] is False
    record = folder / 'observations' / day / '0001.json'
    data = record.read_text(encoding='utf-8')
    assert '975318642' not in data
    assert 'holdout_contaminated_for_rights_hypothesis' in data
    assert assess_day(day, obs(day))['holdout_contaminated_for_rights_hypothesis'] is True
