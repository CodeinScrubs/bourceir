"""Partial candles support a separate, fail-closed descriptive sensitivity only."""

import copy
import hashlib
from datetime import datetime

import pytest
from streamlit.testing.v1 import AppTest

from config import settings
from core.market_return_coverage import missing_open_sensitivity
from core.timeutils import TEHRAN_TZ
from ops.market_return_coverage import _read_bound, load_sensitivity
from ops.report_files import write_private_report

AT = datetime(2026, 11, 2, 11, 0, tzinfo=TEHRAN_TZ)


def case():
    received = '2026-11-02T10:00:01+03:30'
    source = '2026-11-02T10:00:00+03:30'
    report = dict(version='market-review-v1', run_id='MARKET-test', session_date='2026-11-02',
                  observed_at='2026-11-02T10:01:00+03:30',
                  summary=dict(scope_count=2, traded_count=1, return_known_count=1,
                               return_unknown_count=0, positive=0, negative=1, unchanged=0),
                  instruments=[dict(symbol='آ', ins_code='1', status='traded', closing_return=-.01),
                               dict(symbol='ب', ins_code='2', status='invalid_candle',
                                    source_date='2026-11-02', received_at=received, provider_timestamp=source)])
    observation = dict(source='tsetmc', url='https://cdn.tsetmc.com/api/ClosingPrice/GetClosingPriceInfo/2',
                       ins_code='2', identity_attestation='request_url_only', date_status='matched',
                       received_at=received, provider_timestamp=source,
                       candle=dict(date='2026-11-02', open_raw=0, low_raw=90, high_raw=110, close_raw=100,
                                   close_final_raw=105, ref_price_raw=100, volume=10, value=1000, trade_count=5),
                       payload={'closingPriceInfo':dict(insCode='0', dEven=20261102, finalLastDate=20261102,
                           hEven=100000, priceFirst=0, priceMin=90, priceMax=110, pDrCotVal=100,
                           pClosing=105, priceYesterday=100, qTotTran5J=10, qTotCap=1000, zTotTran=5)})
    return report, {'2': observation}


def test_missing_open_sensitivity_is_separate_and_does_not_impute():
    report, observations = case()
    before = copy.deepcopy((report, observations))
    result = missing_open_sensitivity(report, observations, at=AT)
    assert (report, observations) == before
    assert result['baseline_known'] == 1 and result['baseline_positive'] == 0
    assert result['partial_returns'] == 1 and result['rows'][0]['closing_return'] == pytest.approx(.05)
    assert (result['sensitivity_known'], result['sensitivity_positive'], result['sensitivity_negative']) == (2, 1, 1)
    assert result['sensitivity_positive_fraction'] == .5
    assert not result['opening_imputed'] and not result['accepted_for_signals']
    assert not result['source_independently_verified'] and not result['simultaneous_snapshot_verified']


def test_response_identity_can_be_confirmed_by_the_same_native_payload():
    report, observations = case()
    observations['2']['identity_attestation'] = 'response_matches_request'
    observations['2']['payload']['closingPriceInfo']['insCode'] = '2'
    result = missing_open_sensitivity(report, observations, at=AT)
    assert result['partial_returns'] == 1
    assert result['rows'][0]['identity_attestation'] == 'response_matches_request'


def test_native_timestamp_after_receipt_stays_unknown_even_when_fields_match():
    report, observations = case()
    stamp = '2026-11-02T10:05:00+03:30'
    report['instruments'][1]['provider_timestamp'] = stamp
    observations['2']['provider_timestamp'] = stamp
    observations['2']['payload']['closingPriceInfo']['hEven'] = 100500
    result = missing_open_sensitivity(report, observations, at=AT)
    assert result['partial_returns'] == 0 and result['excluded']['unknown_or_conflicting_date'] == 1


@pytest.mark.parametrize('body', [b'{"x":1,"x":2}', b'{"x":1e400}', b'{"x":NaN}', b'[]'])
def test_hashed_archive_json_rejects_ambiguity_and_nonfinite_values(tmp_path, body):
    path = tmp_path/'observation.json'
    path.write_bytes(body)
    with pytest.raises(ValueError):
        _read_bound(path, hashlib.sha256(body).hexdigest())


def change_price(observation, key, value):
    names = {'open_raw':'priceFirst', 'low_raw':'priceMin', 'high_raw':'priceMax', 'close_raw':'pDrCotVal',
             'close_final_raw':'pClosing', 'ref_price_raw':'priceYesterday', 'volume':'qTotTran5J',
             'value':'qTotCap', 'trade_count':'zTotTran'}
    observation['candle'][key] = value
    observation['payload']['closingPriceInfo'][names[key]] = value


@pytest.mark.parametrize('mutate', [
    lambda o: o.update(identity_attestation='conflicting_response'),
    lambda o: o.update(ins_code='3'),
    lambda o: o.update(url='https://example.com/2'),
    lambda o: o['payload']['closingPriceInfo'].update(insCode='3'),
    lambda o: o['payload']['closingPriceInfo'].update(pClosing=104),
    lambda o: o['payload']['closingPriceInfo'].update(finalLastDate=20261101),
    lambda o: o['payload']['closingPriceInfo'].update(dEven=202611020),
    lambda o: o['payload']['closingPriceInfo'].update(hEven=True),
    lambda o: o['payload']['closingPriceInfo'].update(hEven=1000000),
    lambda o: o.update(date_status='missing_or_conflicting'),
    lambda o: o.update(provider_timestamp='2026-11-02T10:05:00+03:30'),
    lambda o: o['candle'].update(date='2026-11-01'),
    lambda o: change_price(o, 'open_raw', False),
    lambda o: change_price(o, 'open_raw', None),
    lambda o: change_price(o, 'open_raw', -1),
    lambda o: change_price(o, 'open_raw', 100),
    lambda o: change_price(o, 'volume', 0),
    lambda o: change_price(o, 'trade_count', True),
    lambda o: change_price(o, 'ref_price_raw', 0),
    lambda o: change_price(o, 'close_final_raw', float('nan')),
    lambda o: change_price(o, 'close_raw', 111),
    lambda o: change_price(o, 'value', 2000),
])
def test_invalid_partial_inputs_remain_unknown(mutate):
    report, observations = case()
    mutate(observations['2'])
    result = missing_open_sensitivity(report, observations, at=AT)
    assert result['partial_returns'] == 0
    assert result['sensitivity_known'] == 1
    assert sum(result['excluded'].values()) == 1


def test_bad_baseline_and_future_or_naive_clocks_are_rejected():
    report, observations = case()
    report['summary']['positive'] = 1
    with pytest.raises(ValueError, match='reconcile'):
        missing_open_sensitivity(report, observations, at=AT)
    report, observations = case()
    with pytest.raises(ValueError, match='clock'):
        missing_open_sensitivity(report, observations, at=AT.replace(hour=9))
    with pytest.raises(ValueError, match='clock'):
        missing_open_sensitivity(report, observations, at=AT.replace(tzinfo=None))


@pytest.mark.parametrize('reanalysis', [False, True])
def test_archive_sensitivity_uses_same_hashed_bytes_and_preserves_missing(tmp_path, monkeypatch, reanalysis):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr('core.market_return_coverage.now_tehran', lambda: AT)
    root = settings.DATA_DIR/'private/market-reviews/MARKET-test'
    report, observations = case()
    if reanalysis:
        report['source_run_id'] = 'MARKET-source'
    report_path = write_private_report(root/'report.json', report)
    relative = ('source/' if reanalysis else '') + 'closing-2.json'
    observation_path = write_private_report(root/relative, observations['2'])
    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    manifest = dict(version='market-review-v1', files={'report.json':digest(report_path),
                                                        relative:digest(observation_path)})
    write_private_report(root/'manifest.json', manifest)
    result = load_sensitivity(root, report)
    assert result['partial_returns'] == 1
    observation_path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='bytes or size'):
        load_sensitivity(root, report)
    manifest['files'].pop(relative)
    write_private_report(root/'manifest.json', manifest)
    result = load_sensitivity(root, report)
    assert result['partial_returns'] == 0
    assert result['excluded']['missing_or_conflicting_identity'] == 1


@pytest.mark.parametrize('broken', [False, True])
def test_market_page_displays_sensitivity_or_preserves_main_report(tmp_path, monkeypatch, broken):
    monkeypatch.setattr(settings, 'DATA_DIR', tmp_path/'data')
    monkeypatch.setattr('dashboard.operator_status.operator_panel', lambda: None)
    monkeypatch.setattr('dashboard.market_review.now_tehran', lambda: AT)
    report, observations = case()
    report['status'] = 'partial'
    report['sectors'] = []
    report['summary'].update(status_counts={'traded':1, 'invalid_candle':1}, median_closing_return=-.01)
    folder = settings.DATA_DIR/'private/market-reviews/MARKET-test'
    write_private_report(folder/'report.json', report)
    monkeypatch.setattr('dashboard.market_review.load_market_review', lambda _: report)
    monkeypatch.setattr('dashboard.market_review.render_html', lambda _: '<html></html>')

    def sensitivity(_, current):
        if broken:
            raise ValueError('corrupt source')
        return missing_open_sensitivity(current, observations, at=AT)

    monkeypatch.setattr('dashboard.market_review.load_sensitivity', sensitivity)
    app = AppTest.from_string('from dashboard.market_review import page_market_review\n'
                              'page_market_review(lambda *args, **kwargs: None)').run()
    assert not app.exception
    assert any('پوشش ناقص' in item.value for item in app.warning)
    assert any(item.label == 'کندل معتبر' for item in app.metric)
    assert any('گشایش نامعلوم' in item.label for item in app.expander) is not broken
    assert any('تحلیل حساسیت گشایش نامعلوم قابل بازخوانی نیست' in item.value for item in app.warning) is broken
