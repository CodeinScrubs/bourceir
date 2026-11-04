import hashlib
import json
from copy import deepcopy

import pytest

from core.queue_fall_study import book_precheck, book_request, study_session, summarize_sessions
from core.quote_context import context_requests

CODE = '12345'
DAY = '2024-10-21'


def observation(url, payload):
    body = json.dumps(payload, separators=(',', ':')).encode()
    return dict(source='tsetmc', ins_code=CODE, url=url, content=body, response_bytes=len(body),
                body_sha256=hashlib.sha256(body).hexdigest(), http_status=200,
                requested_at='2024-10-22T10:00:00+03:30', received_at='2024-10-22T10:00:01+03:30')


def book_row(clock=85000, *, price=10500, ask=0, ask_quantity=0, ask_orders=0, ref=1):
    return dict(insCode=CODE, dEven=20241021, hEven=clock, refID=ref, number=1, pMeDem=price,
                qTitMeDem=100, zOrdMeDem=2, pMeOf=ask, qTitMeOf=ask_quantity, zOrdMeOf=ask_orders)


def make_inputs(rows=None, *, prices=(10500, 10290), clocks=(90000, 90100), threshold_rows=None):
    limits = threshold_rows or [dict(insCode=CODE, dEven=20241021, hEven=60000,
                                    psGelStaMin=9500, psGelStaMax=10500)]
    book = observation(book_request(CODE, DAY), {'bestLimitsHistory': rows or [book_row()]})
    threshold = observation(context_requests(CODE, DAY)[1], {'staticThreshold': limits})
    raw = [dict(insCode=CODE, dEven=20241021, hEven=clock, canceled=0, nTran=i + 1,
                pTran=price, qTitTran=10) for i, (clock, price) in enumerate(zip(clocks, prices))]
    tape = dict(source='tsetmc', price_unit='Rial', ins_code=CODE, requested_date=DAY,
                endpoint_is_grouped=False, captured_at='2024-10-22T10:01:00+03:30',
                source_url=f'https://cdn.tsetmc.com/api/Trade/GetTradeHistory/{CODE}/20241021/false',
                response={'tradeHistory': raw})
    daily = dict(date=DAY, ins_code=CODE, source='tsetmc', price_unit='Rial',
                 open_raw=prices[0], close_raw=prices[-1], low_raw=min(prices), high_raw=max(prices),
                 ref_price_raw=10000, volume=len(prices) * 10, value=sum(prices) * 10,
                 trade_count=len(prices))
    return book, threshold, tape, daily


def test_preopen_observation_and_later_two_percent_trade():
    result = study_session(*make_inputs())
    assert result['state'] == 'observed'
    assert result['pre_09_clock_observed'] is True
    assert result['post_09_clock_observed'] is False
    assert result['observed'][0]['later_trade']['price_rial'] == 10290
    assert result['observed'][0]['fall_percent'] == '2'
    assert result['trade_reconciliation']['complete']
    assert result['phase_verified'] is False
    assert result['execution_proven'] is False
    assert result['book_completeness_verified'] is False


@pytest.mark.parametrize(('ask', 'quantity', 'orders', 'issue'), [
    (10500, 30, 1, 'nonempty_or_inconsistent_ask_at_upper_bid'),
    (10510, 30, 1, 'ask_outside_daily_price_limits'),
    (0, 20, 0, 'nonempty_or_inconsistent_ask_at_upper_bid'),
])
def test_inconsistent_or_crossed_ask_is_provisional(ask, quantity, orders, issue):
    result = study_session(*make_inputs([book_row(ask=ask, ask_quantity=quantity, ask_orders=orders)]))
    assert result['state'] == 'provisional'
    assert issue in result['provisional'][0]['issues']
    assert not result['observed']


def test_trade_in_same_second_does_not_prove_order():
    result = study_session(*make_inputs([book_row(clock=90100)]))
    assert result['state'] == 'not_observed'
    assert result['no_event_proven'] is False


def test_one_unit_above_exact_two_percent_is_not_enough():
    result = study_session(*make_inputs(prices=(10500, 10291)))
    assert result['state'] == 'not_observed'


def test_pre_and_post_clock_buckets_and_one_symbol_day_count():
    result = study_session(*make_inputs([book_row(), book_row(clock=90030, ref=2)]))
    assert result['pre_09_clock_observed'] and result['post_09_clock_observed']
    ranked = summarize_sessions([dict(ins_code=CODE, date=DAY, symbol='نمونه', result=result)])
    assert ranked[0]['observed'] == 1
    assert ranked[0]['pre_09_clock'] == ranked[0]['post_09_clock'] == 1


def test_threshold_changes_do_not_use_future_price_limit():
    limits = [dict(insCode=CODE, dEven=20241021, hEven=60000, psGelStaMin=9500, psGelStaMax=10500),
              dict(insCode=CODE, dEven=20241021, hEven=90000, psGelStaMin=9400, psGelStaMax=10300)]
    result = study_session(*make_inputs([book_row(clock=85959, price=10300)], threshold_rows=limits))
    assert result['state'] == 'not_observed'


def test_threshold_change_same_second_is_provisional():
    limits = [dict(insCode=CODE, dEven=20241021, hEven=60000, psGelStaMin=9500, psGelStaMax=10500),
              dict(insCode=CODE, dEven=20241021, hEven=85000, psGelStaMin=9400, psGelStaMax=10300)]
    result = study_session(*make_inputs(threshold_rows=limits))
    assert result['state'] == 'provisional'
    assert 'threshold_change_at_same_second' in result['provisional'][0]['issues']


def test_new_limit_in_the_same_second_is_not_discarded():
    limits = [dict(insCode=CODE, dEven=20241021, hEven=60000, psGelStaMin=9500, psGelStaMax=10500),
              dict(insCode=CODE, dEven=20241021, hEven=85000, psGelStaMin=9400, psGelStaMax=10300)]
    result = study_session(*make_inputs([book_row(price=10300)], prices=(10300, 10094),
                                        threshold_rows=limits))
    assert result['state'] == 'provisional'
    assert 'threshold_change_at_same_second' in result['provisional'][0]['issues']


@pytest.mark.parametrize('mutation', [
    lambda b, t, p, d: b.update(body_sha256='0' * 64),
    lambda b, t, p, d: b.update(url='https://example.org'),
    lambda b, t, p, d: b.update(received_at='2024-10-22T10:00:01'),
    lambda b, t, p, d: p.update(source_url='https://example.org'),
    lambda b, t, p, d: d.update(volume=21),
])
def test_mismatched_evidence_stays_unknown(mutation):
    inputs = make_inputs()
    mutation(*inputs)
    result = study_session(*inputs)
    assert result['state'] == 'unknown'
    assert result['issues']


def test_native_identity_mismatch_is_unknown():
    row = book_row()
    row['insCode'] = '999'
    result = study_session(*make_inputs([row]))
    assert result['state'] == 'unknown'


def test_payload_without_native_identity_discloses_request_binding():
    row = book_row()
    row.update(insCode=None, dEven=0)
    result = study_session(*make_inputs([row]))
    assert result['state'] == 'observed'
    assert not result['book_payload_identity_verified']
    assert not result['book_payload_date_verified']


def test_canceled_trade_is_not_a_fall_witness():
    book, threshold, tape, daily = make_inputs(prices=(10500, 10499))
    canceled = deepcopy(tape['response']['tradeHistory'][1])
    canceled.update(nTran=3, pTran=9000, hEven=90200, canceled=1)
    tape['response']['tradeHistory'].append(canceled)
    assert study_session(book, threshold, tape, daily)['state'] == 'not_observed'


def test_conflicting_same_revision_is_unknown():
    first, second = book_row(), book_row(ask=10500, ask_quantity=1, ask_orders=1)
    assert study_session(*make_inputs([first, second]))['state'] == 'unknown'


def test_duplicate_symbol_day_rejected():
    result = study_session(*make_inputs())
    row = dict(ins_code=CODE, date=DAY, symbol='نمونه', result=result)
    with pytest.raises(ValueError, match='Duplicate'):
        summarize_sessions([row, row])


def test_prefetch_screen_fetches_for_possible_bid_and_does_not_claim_absence():
    book, threshold, _, _ = make_inputs()
    assert book_precheck(book, threshold, CODE, DAY)['state'] == 'tape_needed'
    other_book, _, _, _ = make_inputs([book_row(price=10499)])
    result = book_precheck(other_book, threshold, CODE, DAY)
    assert result['state'] == 'no_upper_bid_observed'
    assert result['source_completeness_verified'] is False


def test_prefetch_screen_uses_all_same_day_limits_to_avoid_false_exclusion():
    limits = [dict(insCode=CODE, dEven=20241021, hEven=60000, psGelStaMin=9500, psGelStaMax=10500),
              dict(insCode=CODE, dEven=20241021, hEven=90000, psGelStaMin=9400, psGelStaMax=10300)]
    book, threshold, _, _ = make_inputs([book_row(clock=85959, price=10300)], threshold_rows=limits)
    assert book_precheck(book, threshold, CODE, DAY)['state'] == 'tape_needed'


def test_prefetch_screen_refuses_inconsistent_source_identity():
    row = book_row()
    row['insCode'] = '999'
    book, threshold, _, _ = make_inputs([row])
    assert book_precheck(book, threshold, CODE, DAY)['state'] == 'unknown'
