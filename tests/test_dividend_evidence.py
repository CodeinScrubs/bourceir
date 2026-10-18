"""فقط منبع ساختگی در فایل/دیتابیس موقت؛ رقم نامزد هرگز وجه قابل خرج نیست."""

import hashlib
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from streamlit.testing.v1 import AppTest

from config import settings
from core import dividend_evidence as evidence
from core.timeutils import now_tehran_iso
from data_collector.tsetmc import TsetmcProvider
from ops import dividend_evidence as archive


def row(amount='100', *, published='1402/04/30', meeting='1402/04/29', fiscal='1401/12/29'):
    return '@'.join((published, meeting, fiscal, '999', '888', '777', amount)) + ';'


def parse(text):
    return evidence.parse_dividend_source(text.encode('utf-8'), end_exclusive='2025-03-21')


def observation(code='123', symbol='آزمون', body=None):
    code, symbol, url = evidence.dividend_request(code, symbol)
    body = row().encode() if body is None else body
    return dict(source='tsetmc',ins_code=code,symbol=symbol,url=url,requested_at=now_tehran_iso(),
        received_at=now_tehran_iso(),content=body,response_bytes=len(body),body_sha256=hashlib.sha256(body).hexdigest())


@pytest.fixture
def scope(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path / 'data')
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'holdout_start', '2025-03-21')
    return [dict(ins_code='123', symbol_fa='آزمون', instrument_type_code=300)]


class Source:
    def get_dividend_observation(self, code, symbol):
        return observation(code, symbol)


def test_source_amount_is_only_a_candidate_and_last_column_is_not_eps_or_price_adjustment():
    parsed = parse(row())
    item, = parsed['rows']
    assert item['candidate_dps_amount'] == '100' and item['amount_unit'] is None
    assert item['stated_meeting_date'] == '2023-07-20'
    assert item['original_publication_time'] is None
    assert item['identity_attestation'] == 'request_symbol_only'
    assert not any(item[key] for key in ('amount_unit_verified','statement_dates_verified','per_share_meaning_verified','feature_eligible','entitlement_verified'))
    assert not parsed['economic_entitlements_verified'] and not parsed['settlement_verified']
    assert not parsed['source_completeness_verified'] and not parsed['accepted_for_signals']


@pytest.mark.parametrize('amount', ['', 'NaN', '-1', '1e3', 'true', '100 تومان', '1,00', '9' * 101])
def test_unknown_amount_never_uses_profit_columns_or_zero(amount):
    item, = parse(row(amount))['rows']
    assert item['candidate_dps_amount'] is None and item['amount_status'] == 'unknown'


@pytest.mark.parametrize('amount,expected,status', [('0','0','observed_zero'),('۰٫۰۰','0.00','observed_zero'),
    ('1,234.50','1234.50','observed_positive')])
def test_exact_decimal_and_observed_zero_are_preserved_without_unit_conversion(amount, expected, status):
    item, = parse(row(amount))['rows']
    assert item['candidate_dps_amount'] == expected and item['amount_status'] == status


@pytest.mark.parametrize('other,conflicting', [('100.00',False),('120',True),('',False)])
def test_duplicate_revisions_are_retained_not_summed_or_silently_replaced(other, conflicting):
    parsed = parse(row() + row(other, published='1402/05/01'))
    assert parsed['source_rows'] == parsed['development_rows'] == 2
    assert all(item['duplicate_meeting'] and item['conflicting_amounts'] is conflicting for item in parsed['rows'])


def test_duplicate_group_does_not_repeat_decimal_comparisons_for_every_row(monkeypatch):
    original = evidence.Decimal
    calls = []

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(evidence, 'Decimal', counted)
    count = 100
    parsed = parse(row('100.00') * count)
    assert len(parsed['rows']) == count
    assert all(item['duplicate_meeting'] and not item['conflicting_amounts'] for item in parsed['rows'])
    assert len(calls) <= 3 * count


@pytest.mark.parametrize('dates', [dict(published='1405/01/01'),dict(meeting='1405/01/01'),dict(fiscal='1405/12/29')])
def test_sealed_rows_are_gated_before_any_amount_parsing(monkeypatch, dates):
    monkeypatch.setattr(evidence, '_amount', lambda _:pytest.fail('Sealed financial value was parsed'))
    parsed = parse(row('never inspect this', **dates))
    assert parsed['withheld_rows'] == 1 and parsed['rows'] == []


def test_bad_dates_or_columns_remain_in_denominator_and_do_not_create_zero_dividend():
    parsed = parse(row(fiscal='1402/12/29') + row(published='') + 'one@two;' + row())
    assert parsed['source_rows'] == 4 and parsed['development_rows'] == 1
    assert len(parsed['quarantined_rows']) == 3
    assert {item['reason'] for item in parsed['quarantined_rows']} == {
        'date_order_conflict','unknown_or_invalid_date','unexpected_column_count'}
    assert parse('')['status'] == 'empty_response' and parse('')['rows'] == []


@pytest.mark.parametrize('body', [b'<html>captcha</html>',b'\x00',b'\xff',b'x' * (evidence.MAX_BYTES + 1)],
                         ids=['html','nul','encoding','oversized'])
def test_untrusted_or_oversized_body_is_not_dividend_evidence(body):
    with pytest.raises(ValueError):
        evidence.parse_dividend_source(body, end_exclusive='2025-03-21')


@pytest.mark.parametrize('code,symbol', [('0','آزمون'),(True,'آزمون'),('۱۲۳','آزمون'),('123','نماد2'),
    ('123','../file'),('123',None),('123',''),('1/2','آزمون')])
def test_bad_request_is_rejected_before_network(code, symbol):
    with pytest.raises(ValueError):
        TsetmcProvider(session=SimpleNamespace(get=lambda *_a,**_k:pytest.fail('network'))).get_dividend_observation(code,symbol)


class Response:
    def __init__(self, body=None, status=200, length=None, url=None):
        self.body = row().encode() if body is None else body
        self.status_code, self.closed, self.supplied_url = status, False, url
        self.headers = {} if length is None else {'Content-Length':length}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError('fixture')

    def iter_content(self, chunk_size):
        yield self.body

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response, self.calls = response, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.response.url = self.response.supplied_url or url
        return self.response


def test_provider_preserves_exact_bytes_and_never_follows_redirects_or_saves_headers():
    response = Response(body=b'\xef\xbb\xbf' + row().encode())
    session = Session(response)
    actual = TsetmcProvider(session=session).get_dividend_observation('123','آزمون')
    assert actual['content'] == response.body and actual['body_sha256'] == hashlib.sha256(response.body).hexdigest()
    assert response.closed and not session.calls[0][1]['allow_redirects']
    assert 'headers' not in actual and 'cookies' not in actual


@pytest.mark.parametrize('changes', [dict(status=302),dict(status=206),dict(status=403),dict(body=b'<html>verify</html>'),
    dict(body=b'\xff'),dict(body=b'x' * (evidence.MAX_BYTES + 1)),dict(length=str(evidence.MAX_BYTES + 1)),dict(url='https://other.invalid/')])
def test_bad_response_returns_none_and_closes_without_mock_or_downgrade(changes):
    response = Response(**changes)
    assert TsetmcProvider(session=Session(response)).get_dividend_observation('123','آزمون') is None
    assert response.closed


def test_archive_registers_before_request_keeps_empty_and_failed_attempts_and_replays_offline(scope):
    class Mixed:
        calls = 0
        def get_dividend_observation(self, code, symbol):
            folder = next((settings.DATA_DIR / 'private/dividend-evidence').glob('DIVIDENDS-*'))
            assert archive.read_private_report(folder / 'plan.json')['requests']
            self.calls += 1
            return observation(code,symbol) if self.calls == 1 else observation(code,symbol,b'') if self.calls == 2 else None
    instruments = scope + [dict(ins_code='456',symbol_fa='نمونه',instrument_type_code=303),dict(ins_code='789',symbol_fa='دیگر',instrument_type_code=300)]
    result = archive.capture_dividend_evidence(Mixed(),instruments)
    loaded = archive.load_dividend_evidence(result['folder'])
    assert loaded['artifacts_verified'] and loaded['status_counts'] == {'observed_unverified':1,'empty_response':1,'unavailable':1}
    assert len(loaded['cases']) == 3 and not loaded['database_writes']
    assert not loaded['feature_eligible'] and not loaded['settlement_verified']


@pytest.mark.parametrize('kind', ['body','candidate','promotion','omission','identity','clock','code','unknown_shape'])
def test_rehashed_tampering_cannot_upgrade_or_omit_evidence(scope, kind):
    result = archive.capture_dividend_evidence(Source(),scope)
    folder = Path(result['folder'])
    manifest = archive.read_private_report(folder / 'manifest.json')
    if kind == 'body':
        (folder / 'body-000.bin').write_bytes(row('999').encode())
        manifest['observations']['body-000.bin'] = archive._sha(folder / 'body-000.bin')
    elif kind in ('candidate','promotion','omission'):
        saved = archive.read_private_report(folder / 'result.json')
        if kind == 'candidate': saved['cases'][0]['assessment']['rows'][0]['candidate_dps_amount'] = '999'
        if kind == 'promotion': saved['economic_entitlements_verified'] = True
        if kind == 'omission': saved['cases'] = []
        archive.write_private_report(folder / 'result.json',saved)
        manifest['result_sha256'] = archive._sha(folder / 'result.json')
    elif kind in ('identity','clock','unknown_shape'):
        saved = archive.read_private_report(folder / 'observation-000.json')
        if kind == 'identity': saved['observation']['symbol'] = 'نمونه'
        if kind == 'clock': saved['observation']['received_at'] = '2000-01-01T00:00:00+03:30'
        if kind == 'unknown_shape': saved['observation'] = 'invalid'
        archive.write_private_report(folder / 'observation-000.json',saved)
        manifest['observations']['observation-000.json'] = archive._sha(folder / 'observation-000.json')
    else:
        (folder / 'code/core/dividend_evidence.py').unlink()
    archive.write_private_report(folder / 'manifest.json',manifest)
    with pytest.raises((ValueError,OSError)):
        archive.load_dividend_evidence(folder)


def test_interruption_retains_preregistered_attempt_and_does_not_silently_restart(scope):
    class Interrupted:
        def get_dividend_observation(self,*_):
            raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        archive.capture_dividend_evidence(Interrupted(),scope)
    folder, = (settings.DATA_DIR / 'private/dividend-evidence').glob('DIVIDENDS-*')
    assert archive.read_private_report(folder / 'status.json')['status'] == 'failed_or_interrupted'
    assert (folder / 'plan.json').exists() and not (folder / 'manifest.json').exists()
    archive.capture_dividend_evidence(Source(),scope)
    assert len(list(folder.parent.glob('DIVIDENDS-*'))) == 2


@pytest.mark.parametrize('kind',['empty','many','outside','duplicate_id','duplicate_symbol','unknown_kind'])
def test_scope_is_checked_before_network_or_archive_creation(scope, kind):
    instruments = [dict(scope[0])]
    if kind == 'empty': instruments = []
    if kind == 'many': instruments *= 11
    if kind == 'outside': instruments[0]['instrument_type_code'] = 305
    if kind == 'unknown_kind': instruments[0]['instrument_type_code'] = None
    if kind == 'duplicate_id': instruments.append(dict(scope[0],symbol_fa='نمونه'))
    if kind == 'duplicate_symbol': instruments.append(dict(scope[0],ins_code='456'))
    source = SimpleNamespace(get_dividend_observation=lambda *_:pytest.fail('Out-of-scope network'))
    with pytest.raises(ValueError):
        archive.capture_dividend_evidence(source,instruments)
    assert not (settings.DATA_DIR / 'private/dividend-evidence').exists()


def test_archive_cannot_be_written_outside_private_root(scope, tmp_path):
    with pytest.raises(ValueError):
        archive.capture_dividend_evidence(Source(),scope,root=tmp_path / 'public')
    assert not (tmp_path / 'public').exists()


def test_code_change_during_capture_stops_publication_and_preserves_attempt(scope, tmp_path, monkeypatch):
    base = tmp_path / 'code-base'
    for name in archive.CODE_FILES:
        target = base / name
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes((settings.BASE_DIR / name).read_bytes())
    monkeypatch.setattr(settings,'BASE_DIR',base)
    class Changed(Source):
        def get_dividend_observation(self,*args):
            (base / 'core/dividend_evidence.py').write_text('# changed test copy\n',encoding='utf-8')
            return super().get_dividend_observation(*args)
    with pytest.raises(ValueError,match='کد'):
        archive.capture_dividend_evidence(Changed(),scope)
    folder, = (settings.DATA_DIR / 'private/dividend-evidence').glob('DIVIDENDS-*')
    assert (folder / 'body-000.bin').exists() and not (folder / 'manifest.json').exists()


def test_review_cannot_expand_current_sealed_boundary(scope, monkeypatch):
    result = archive.capture_dividend_evidence(Source(),scope)
    monkeypatch.setitem(settings.RESEARCH_CONFIG,'holdout_start','2024-01-01')
    with pytest.raises(ValueError):
        archive.load_dividend_evidence(result['folder'])


def test_ui_is_read_only_keeps_unknown_units_and_shows_no_profit_or_cash_credit(scope, monkeypatch):
    archive.capture_dividend_evidence(Source(),scope)
    monkeypatch.setattr(TsetmcProvider,'get_dividend_observation',lambda *_:pytest.fail('UI network'))
    app = AppTest.from_string('from dashboard.dividend_evidence import show_dividend_evidence\nshow_dividend_evidence()').run()
    assert not app.exception and len(app.dataframe) == 2 and not app.metric
    assert any('واحد' in warning.value and 'وصول' in warning.value for warning in app.warning)
    assert app.dataframe[1].value['رقم نامزد؛ واحد تأییدنشده'].iloc[0] == '100'


def test_cli_only_reads_registry_never_account_or_writes_market_tables(scope, db, monkeypatch, capsys):
    import brs
    from database import db as module
    db.upsert_symbol(scope[0])
    monkeypatch.setattr(settings,'DB_PATH',db.db_path)
    original = module.DatabaseManager
    def readonly(*args, **kwargs):
        assert kwargs == {'read_only':True}
        opened = original(*args, **kwargs)
        opened.conn.set_authorizer(lambda action,table,*_:sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_READ and table != 'symbols' else sqlite3.SQLITE_OK)
        return opened
    monkeypatch.setattr(module,'DatabaseManager',readonly)
    monkeypatch.setattr('data_collector.tsetmc.TsetmcProvider',Source)
    before = db.conn.total_changes
    assert brs.cmd_dividend_source(SimpleNamespace(symbols='آزمون')) == 0
    folder = json.loads(capsys.readouterr().out)['folder']
    monkeypatch.setattr(module,'DatabaseManager',lambda *_a,**_k:pytest.fail('Offline review opened DB'))
    assert brs.cmd_dividend_source_review(SimpleNamespace(study=folder)) == 0
    assert db.conn.total_changes == before
