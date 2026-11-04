"""Evidence-only IDs, bounded group failure, legacy numeric decoder and date layout."""

import hashlib
import json
import shutil
import threading
import time
from types import SimpleNamespace

import pytest

from config import settings
from core.source_cooldown import VERSION as SOURCE_VERSION
from core.source_cooldown import make_backoff
from data_collector import daily_publications as daily
from data_collector import publication_cache as cache
from data_collector.publication_compat import (
    LEGACY_CODE,
    LEGACY_VERSION,
    POST_BOOK_V2_CODE,
    PRE_0750_COMPAT_FIX_V2_CODE,
    PRE_0750_V2_CODE,
    PRE_BOOK_V2_CODE,
    PRE_DATE_CIRCUIT_V2_CODE,
    PRE_HISTORY_QUALITY_V2_CODE,
    archive_contract,
)
from tests.test_daily_publications import DAY, NOW, Provider, observation, task


@pytest.fixture(autouse=True)
def archive_clock(monkeypatch):
    monkeypatch.setattr(daily,'now_tehran_iso',lambda:NOW)


def test_evidence_only_native_task_has_no_invented_database_id_and_replays(tmp_path):
    archive=daily.DailyPublicationArchive(tmp_path,[task()|dict(symbol_id=None)])
    assert archive.folder.parent==tmp_path/DAY
    records=archive.capture(Provider())
    path=archive.finish(dict(database_writes=False))
    assert records[0]['task']['symbol_id']is None and records[0]['transport']is None
    assert daily.review_archive(path)['byte_replay_verified']
    with pytest.raises(ValueError):daily._tasks([task()|dict(symbol_id=None)],NOW,version=LEGACY_VERSION)


def test_exact_known_legacy_bundle_still_uses_unchanged_native_decoder():
    assert archive_contract(LEGACY_VERSION,dict(LEGACY_CODE))==tuple(LEGACY_CODE)
    for changed in (dict(LEGACY_CODE)|{'core/timeutils.py':'0'*64},{},dict(LEGACY_CODE)|{'unknown.py':'0'*64}):
        with pytest.raises(ValueError):archive_contract(LEGACY_VERSION,changed)
    with pytest.raises(ValueError):archive_contract('unknown',LEGACY_CODE)


def test_exact_pre_book_v2_bundle_remains_readable_but_unknown_vintages_do_not():
    prior = dict(LEGACY_CODE) | {
        'data_collector/tsetmc.py': 'd2695d815618f263d817fd00303258b5f5743824ca4bb839067c2c1a9a1fe6b6',
        'data_collector/daily_publications.py': '384b072a3d21566a466d01d526c79bc925cc3a96072606f3ab0514920a79e3e7',
        'core/source_cooldown.py': '7607ff9a681f7abaed13c53571b9a0d70afa01eee1ede460f2ef8414f5df22e0',
        'data_collector/source_cooldown.py': 'dad2bf40d95043edbcda61381196c09106deafe0706acbf28f9e4a248af5cbd5',
        'data_collector/publication_compat.py': 'f370ecef0d8a8c1da0e022c879d9551ea45d3b674a0d75bbf1e3a19c3c2dc934',
    }
    assert set(archive_contract(daily.VERSION, prior)) == set(prior)
    with pytest.raises(ValueError):
        archive_contract(daily.VERSION, prior | {'data_collector/scheduler.py': '0' * 64})


def test_exact_post_book_v2_bundle_remains_readable_but_unknown_vintages_do_not():
    # This identity was recorded in public-market archives before the Windows
    # state-write change; it must not be derived from the currently installed code.
    prior = dict(PRE_BOOK_V2_CODE) | {
        'data_collector/daily_publications.py': '56bfcd4e8b87d7f86541c0f6a7bc3c459d04511678f0b564db6124a3107e05bc',
        'data_collector/scheduler.py': '1b69f4496ba69deb07608c0f18cd22778d6574b63b4664064448dfd5eb606583',
        'data_collector/publication_compat.py': '71611665a42ed9f9c583b1eb602777630bf9987db4cb0697150707f58470c166',
        'core/order_book.py': '014a68e1cb1be2ab2021c7201f510451cde701e1c2a765b354ef28c8d0483d72',
    }
    assert prior == POST_BOOK_V2_CODE
    assert all(len(value) == 64 and int(value, 16) >= 0 for value in prior.values())
    assert set(archive_contract(daily.VERSION, prior)) == set(prior)
    with pytest.raises(ValueError, match='incompatible'):
        archive_contract(daily.VERSION, POST_BOOK_V2_CODE | {'core/order_book.py': '0' * 64})


def test_exact_pre_date_circuit_bundle_remains_readable_but_unknown_vintages_do_not():
    prior = dict(POST_BOOK_V2_CODE) | {
        'data_collector/base.py': '174e5a147b90015a2fedb3c288dc9feed7e588a14f0ce679fdb1ca29afe03dc7',
        'data_collector/tsetmc.py': '12efdb0f985292f42d04acfed3d8b706c9f673fe73af860049bb66b1436050d3',
        'data_collector/scheduler.py': '168eb59e34377ac8fc35c8812994c77a8376fbe9394f33f660835a4ee1688431',
        'database/db.py': 'c88fc486f552b8075531ef61e2da1e91404f0a7b0cfc11cded2b6f9f013c88e4',
        'data_collector/source_cooldown.py': '163e43c2bd2f6bc2488c6242e6e489a206636ab7b0578141194c4d0df58e0bb1',
        'data_collector/publication_compat.py': 'c68102cfed8fff2f9bd0aec5c6f5ed86ebc3d3babaa99e335836aab9388ff695',
    }
    assert prior == PRE_DATE_CIRCUIT_V2_CODE
    assert set(archive_contract(daily.VERSION, prior)) == set(prior)
    with pytest.raises(ValueError, match='incompatible'):
        archive_contract(daily.VERSION, prior | {'database/db.py': '0' * 64})


def test_exact_pre_history_quality_bundle_is_readable_but_not_mutable():
    prior = dict(PRE_DATE_CIRCUIT_V2_CODE) | {
        'data_collector/daily_publications.py': '0f7747e26e126d847a136edad3e0b82bf2ee6905f511806a6dff81fa51e76283',
        'data_collector/publication_compat.py': 'cb1bb42b660b4276c542c3828c86b8fb718cbf7ab46e45eb826f865227ccb723',
    }
    assert prior == PRE_HISTORY_QUALITY_V2_CODE
    assert set(archive_contract(daily.VERSION, prior)) == set(prior)
    with pytest.raises(ValueError, match='incompatible'):
        archive_contract(daily.VERSION, prior | {'data_collector/scheduler.py': '0' * 64})


@pytest.mark.parametrize('config_hash,expected', [
    ('c840f631b01e467b2380906b559e90c0a816dd19571c820e698bc8cdb685ad9d', PRE_0750_V2_CODE),
    ('f078cdb3e7202668756ac8935341773fdd16ed559bf51f84e000f1e59d75ee91', PRE_0750_COMPAT_FIX_V2_CODE),
])
def test_exact_release_producers_replay_but_unknown_versions_and_code_fail_closed(config_hash, expected):
    recorded = dict(PRE_HISTORY_QUALITY_V2_CODE) | {
        'data_collector/scheduler.py': '79b2ae3cde82edccbf31ac350d594b39e85124f2b7b7e686550383c13fc3d844',
        'data_collector/publication_compat.py': '67c75d4c81d903ae40f23c5ad3681d6130b33811c94756a77202e7416d2f48bd',
        'config/default.yaml': config_hash,
    }
    assert recorded == expected
    assert set(archive_contract(daily.VERSION, recorded)) == set(recorded)
    for modified in (recorded | {'config/default.yaml': '0' * 64},
                     recorded | {'data_collector/scheduler.py': '0' * 64},
                     recorded | {'unexpected.py': '0' * 64}):
        with pytest.raises(ValueError, match='incompatible'):
            archive_contract(daily.VERSION, modified)
    with pytest.raises(ValueError, match='incompatible'):
        archive_contract('unknown', recorded)


@pytest.mark.parametrize('recorded', [PRE_BOOK_V2_CODE, POST_BOOK_V2_CODE])
def test_legacy_v2_unknown_current_cooldown_decoder_is_rejected(tmp_path, monkeypatch, recorded):
    for name in daily.CODE_FILES:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(settings.BASE_DIR / name, target)
    with (tmp_path / 'data_collector/source_cooldown.py').open('ab') as stream:
        stream.write(b'\n# unreviewed decoder change\n')
    monkeypatch.setattr(settings, 'BASE_DIR', tmp_path)
    with pytest.raises(ValueError, match='dependencies'):
        archive_contract(daily.VERSION, recorded)


@pytest.mark.parametrize('version,recorded', [(LEGACY_VERSION, LEGACY_CODE), (daily.VERSION, PRE_BOOK_V2_CODE)])
def test_legacy_decoder_change_is_not_blessed_by_the_compatibility_registry(monkeypatch, version, recorded):
    monkeypatch.setattr(daily,'META',daily.META+('untrusted',))
    with pytest.raises(ValueError,match='metadata'):archive_contract(version,recorded)


def _changed_transport_clock(value, attempted_at, completed_at):
    return None


def test_pre_book_v2_refuses_changed_transport_decoder(monkeypatch):
    monkeypatch.setattr(daily, 'checked_transport_clock', _changed_transport_clock)
    with pytest.raises(ValueError, match='decoding'):
        archive_contract(daily.VERSION, PRE_BOOK_V2_CODE)


def test_five_consecutive_failures_keep_every_unattempted_key_in_the_archive(tmp_path):
    calls=[]
    provider=SimpleNamespace(get_daily_publication_observation=lambda *args:(calls.append(args)or None))
    tasks=[task(code=str(i+1),sid=i+1)for i in range(12)]
    archive=daily.DailyPublicationArchive(tmp_path,tasks)
    records=archive.capture(provider)
    archive.finish({})
    assert len(calls)==5 and len(records)==12
    assert all(r['parsed']['status']=='source_unavailable'for r in records)
    assert all(r['provider_error_type']=='batch_stopped_consecutive_failures'for r in records[5:])
    assert daily.review_archive(archive.folder)['observations']==12


def test_interleaved_date_server_errors_stop_only_that_date(tmp_path):
    previous = '2024-01-02'

    class ProviderWithOneBrokenDate:
        request_profile = 'offline-test'
        last_transport_evidence = None

        def __init__(self):
            self.calls = []

        def get_daily_publication_observation(self, code, day, kind):
            self.calls.append((code, day, kind))
            self.last_transport_evidence = dict(
                version=SOURCE_VERSION, status='response_received', admitted=True,
                url=daily.publication_request(code, day, kind)[1], requested_at=NOW,
                received_at=NOW, http_status=500 if day == DAY else 200,
            )
            return None if day == DAY else observation(kind, code, day)

    provider = ProviderWithOneBrokenDate()
    tasks = [task(code=str(i), sid=i, day=day, kind='client')
             for i in range(1, 10) for day in (DAY, previous)]
    archive = daily.DailyPublicationArchive(tmp_path, tasks)
    records = archive.capture(provider)
    archive.finish({})

    assert sum(day == DAY for _, day, _ in provider.calls) == 5
    assert sum(day == previous for _, day, _ in provider.calls) == 9
    skipped = [r for r in records if r['request_profile'] == 'not-dispatched-partition-stop']
    assert len(skipped) == 4
    assert all(r['provider_error_type'] == 'partition_stopped_http_5xx' and r['transport'] is None
               and r['source'] is None and r['parsed']['status'] == 'source_unavailable' for r in skipped)
    assert daily.review_archive(archive.folder)['observations'] == len(tasks)


def test_success_resets_the_date_specific_server_failure_count(tmp_path):
    previous = '2024-01-02'

    class IntermittentProvider:
        last_transport_evidence = None

        def __init__(self):
            self.calls = []

        def get_daily_publication_observation(self, code, day, kind):
            self.calls.append((code, day, kind))
            failed = day == DAY and code != '5'
            self.last_transport_evidence = dict(
                version=SOURCE_VERSION, status='response_received', admitted=True,
                url=daily.publication_request(code, day, kind)[1], requested_at=NOW,
                received_at=NOW, http_status=500 if failed else 200,
            )
            return None if failed else observation(kind, code, day)

    provider = IntermittentProvider()
    tasks = [task(code=str(i), sid=i, day=day, kind='client')
             for i in range(1, 10) for day in (DAY, previous)]
    archive = daily.DailyPublicationArchive(tmp_path, tasks)
    records = archive.capture(provider)
    archive.finish({})
    assert len(provider.calls) == len(tasks)
    assert not any(r['request_profile'] == 'not-dispatched-partition-stop' for r in records)


@pytest.mark.parametrize('_attempt', range(5))
def test_parallel_date_circuit_bounds_in_flight_errors_and_keeps_other_dates(tmp_path, _attempt):
    previous = '2024-01-02'
    lock = threading.Lock()
    calls = []

    class ThreadProvider:
        request_profile = 'offline-test'
        last_transport_evidence = None

        def get_daily_publication_observation(self, code, day, kind):
            with lock:
                calls.append((code, day, kind))
            time.sleep(.02 if day == DAY else .005)
            self.last_transport_evidence = dict(
                version=SOURCE_VERSION, status='response_received', admitted=True,
                url=daily.publication_request(code, day, kind)[1], requested_at=NOW,
                received_at=NOW, http_status=500 if day == DAY else 200,
            )
            return None if day == DAY else observation(kind, code, day)

        def close(self):
            pass

    tasks = [task(code=str(i), sid=i, day=day, kind='client')
             for i in range(1, 21) for day in (DAY, previous)]
    archive = daily.DailyPublicationArchive(tmp_path, tasks, workers=3)
    records = archive.capture(None, provider_factory=ThreadProvider)
    archive.finish({})
    # Three workers can already have requests in flight when the fifth fails.
    assert 5 <= sum(day == DAY for _, day, _ in calls) <= 7
    assert sum(day == previous for _, day, _ in calls) == 20
    assert all(r['source'] is None and r['transport'] is None
               for r in records if r['request_profile'] == 'not-dispatched-partition-stop')
    assert daily.review_archive(archive.folder)['observations'] == len(tasks)


def test_a_source_limit_stops_remaining_tasks_even_when_retry_after_is_zero(tmp_path):
    class Limited:
        calls=0
        last_transport_evidence=None
        def get_daily_publication_observation(self,code,day,kind):
            self.calls+=1
            backoff=make_backoff(dict(url=f'https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceDaily/{code}/{day.replace("-","")}',
                requested_at=NOW,received_at=NOW,http_status=429,retry_after_seconds=0,retry_after_unsupported=False))
            self.last_transport_evidence=dict(version=SOURCE_VERSION,status='rate_limited',admitted=True,
                **{k:backoff['observation'][k]for k in ('url','requested_at','received_at','http_status')},backoff=backoff)
            return None
    provider=Limited()
    archive=daily.DailyPublicationArchive(tmp_path,[task(code=str(i+1),sid=i+1)for i in range(8)])
    records=archive.capture(provider)
    archive.finish({})
    assert provider.calls==1 and len(records)==8
    assert records[0]['transport']['http_status']==429
    assert all(r['provider_error_type']=='batch_stopped_source_limit'for r in records[1:])
    assert daily.review_archive(archive.folder)['observations']==8


def test_cache_reads_requested_date_and_legacy_flat_without_lifetime_archive_limit(tmp_path,monkeypatch):
    monkeypatch.setattr(settings,'DATA_DIR',tmp_path/'data')
    monkeypatch.setattr(settings,'DB_PATH',tmp_path/'account.db')
    root=settings.DATA_DIR/'private/daily-publications'
    # These are empty directories, not fabricated market observations.
    for year in range(1900,2000):
        (root/f'{year}-01-01'/('DAYPUB-'+'a'*32)).mkdir(parents=True)
    archive=daily.DailyPublicationArchive(root,[task()])
    archive.capture(Provider());archive.finish({})
    scope=[dict(ins_code=task()['ins_code'],instrument_type_code=300)]
    snapshot,bodies=cache.capture_cache(scope,DAY,observed_at=NOW)
    assert set(snapshot['archives'])=={archive.folder.name}
    assert len(cache.replay_cache(snapshot,bodies.__getitem__,scope,DAY,observed_at=NOW)[task()['ins_code']])==1


def test_legacy_pure_dependency_tampering_blocks_compatibility(tmp_path,monkeypatch):
    names=('core/daily_publications.py','core/price_quality.py','core/price_revision.py',
           'core/client_quality.py','core/text.py','core/timeutils.py')
    for name in names:
        target=tmp_path/name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((settings.BASE_DIR/name).read_bytes())
    (tmp_path/names[0]).write_text('changed decoder',encoding='utf-8')
    monkeypatch.setattr(settings,'BASE_DIR',tmp_path)
    with pytest.raises(ValueError,match='dependencies'):archive_contract(LEGACY_VERSION,LEGACY_CODE)


def test_v2_archived_transport_cannot_contain_arbitrary_headers(tmp_path):
    source=Provider()
    source.last_transport_evidence={'Authorization':'never copy a secret'}
    archive=daily.DailyPublicationArchive(tmp_path,[task()])
    records=archive.capture(source)
    archive.finish({})
    text=(archive.folder/'observation-00000.json').read_text(encoding='utf-8')
    assert 'never copy a secret'not in text and records[0]['provider_error_type']=='ValueError'
    assert records[0]['transport']is None


def test_removing_explicit_transport_unknown_is_not_a_valid_v2_record(tmp_path):
    value=daily.DailyPublicationArchive(tmp_path,[task()])
    value.capture(Provider());value.finish({})
    path=value.folder/'observation-00000.json'
    record=json.loads(path.read_text(encoding='utf-8'))
    record.pop('transport')
    path.write_text(json.dumps(record),encoding='utf-8')
    manifest_path=value.folder/'result.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['artifacts_sha256'][path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest),encoding='utf-8')
    with pytest.raises(ValueError):daily.review_archive(value.folder)
