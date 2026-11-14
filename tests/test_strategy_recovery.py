"""No retry loop, no inferred tradability, and no replacement of native vintages."""

import json
from pathlib import Path

import pytest

from config import settings
from ops.strategy_recovery import RecoveryBudget, collect_recovery, merge_recovery, recovery_requests
from research.strategy_diagnostics import audit_saved_study
from research.strategy_execution import run_execution_followup
from research.strategy_suite import _sha256
from research.strategy_triage import run_triage
from tests.test_strategy_execution import parent as parent
from tests.test_strategy_execution import publication_payload


@pytest.mark.parametrize('kwargs', [dict(max_requests=0),dict(max_requests=True),dict(max_requests=1025),
    dict(max_seconds=0),dict(max_seconds=float('inf')),dict(max_seconds=float('nan'))])
def test_recovery_budget_is_explicit_and_finite(kwargs):
    with pytest.raises(ValueError):
        RecoveryBudget(**kwargs)


@pytest.mark.parametrize('failure', [None,'none','invalid'])
def test_collection_is_bounded_and_keeps_invalid_native_bytes(tmp_path,monkeypatch,failure):
    import base64

    import ops.strategy_recovery as recovery

    root = tmp_path/'data'
    monkeypatch.setattr(settings,'DATA_DIR',root)
    study,triage = root/'private/study',root/'private/triage'
    for p in (study,triage):
        p.mkdir(parents=True)
        (p/'manifest.json').write_text('{}',encoding='utf-8')
    requests = [dict(ins_code='1',symbol='a',date=f'2024-01-0{i}',affected_replays=1) for i in (1,2)]
    monkeypatch.setattr(recovery,'recovery_requests',lambda *_:requests)

    class Provider:
        last_transport_evidence = {'status':'deferred'}
        calls = 0

        def get_daily_publication_observation(self,code,day,kind):
            self.calls += 1
            if failure=='none':
                return None
            row = publication_payload(code,'a',day)['records'][0]['observation']
            row['content'] = base64.b64decode(row.pop('content_base64'))
            if failure=='invalid':
                row['body_sha256'] = '0'*64
            return row

    provider = Provider()
    result = collect_recovery(study,triage,budget=RecoveryBudget(max_requests=1),provider=provider)
    assert provider.calls == 1 and result['attempted']==1 and result['unattempted']==1
    assert not result['complete_received'] and result['actual_unknowns_resolved']==0
    payload = json.loads((Path(result['folder'])/'publications.json').read_bytes())
    if failure=='invalid':
        assert payload['records'][0]['observation']['content_base64']
        assert result['statuses'] == {'unverified_native_response':1}
    if failure=='none':
        assert not payload['records'][0]['native_body_exposed_by_provider']


def test_real_triage_binding_and_native_merge_are_reusable_without_raw_changes(parent):
    folder,observations,missing,_,_ = parent
    observations.write_text(json.dumps(dict(kind='priority_missing_session_publications',records=[])),encoding='utf-8')
    study = Path(run_execution_followup(folder,observations)['folder'])
    audit = Path(audit_saved_study(study)['folder'])
    triage = Path(run_triage(study,audit)['folder'])
    requests = recovery_requests(study,triage)
    assert len(requests)==1 and requests[0]['date']==missing

    class Provider:
        def get_daily_publication_observation(self,code,day,kind):
            import base64

            row = publication_payload(code,'a',day)['records'][0]['observation']
            row['content'] = base64.b64decode(row.pop('content_base64'))
            return row

    before = (folder/'manifest.json').read_bytes()
    result = collect_recovery(study,triage,provider=Provider())
    assert result['complete_received']
    merged = observations.parent/'merged.json'
    outcome = merge_recovery(study,result['folder'],merged)
    assert outcome['unique_publications']==1
    assert outcome['publication_statuses']=={'observed_zero_activity':1}
    assert (folder/'manifest.json').read_bytes()==before
    with pytest.raises(ValueError,match='overwrite'):
        merge_recovery(study,result['folder'],merged)
    # A received native version never silently replaces an existing one.
    second = Path(run_execution_followup(folder,merged)['folder'])
    native = json.loads(merged.read_bytes())['records'][0]
    revision = json.loads(json.dumps(native))
    import base64
    import hashlib

    body = json.loads(base64.b64decode(revision['observation']['content_base64']))
    body['closingPriceDaily']['pClosing']=101
    body['closingPriceDaily']['priceYesterday']=101
    contents = json.dumps(body).encode()
    revision['observation'].update(content_base64=base64.b64encode(contents).decode(),
        body_sha256=hashlib.sha256(contents).hexdigest(),response_bytes=len(contents))
    archived = Path(result['folder'])/'revision'
    archived.mkdir()
    (archived/'protocol.json').write_text(json.dumps(dict(source_study_manifest_sha256=_sha256(second/'manifest.json'))))
    (archived/'publications.json').write_text(json.dumps(dict(kind='priority_missing_session_publications',records=[revision])))
    (archived/'manifest.json').write_text(json.dumps({p.name:_sha256(p) for p in archived.iterdir()}))
    preserved = observations.parent/'preserved.json'
    report = merge_recovery(second,archived,preserved)
    assert not report['overlaps'][0]['same_native_body']
    assert json.loads(preserved.read_bytes())['records'][0] == native
