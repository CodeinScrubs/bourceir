"""Synthetic native observations exercise the historical study without a market write."""

import hashlib
import json

import pytest

from config import settings
from core.price_revision import daily_request
from core.queue_fall_study import book_request, trade_observation, trade_request
from core.quote_context import context_requests
from research.sell_wall import study_sell_wall

CODE = '1234567'
DAY = '2024-01-02'
NATIVE_DAY = 20240102
RECEIVED = '2024-01-03T10:00:00+03:30'


def _observation(url, payload):
    content = json.dumps(payload).encode()
    return dict(source='tsetmc', ins_code=CODE, url=url, http_status=200,
                requested_at=RECEIVED, received_at=RECEIVED, response_bytes=len(content), content=content,
                body_sha256=hashlib.sha256(content).hexdigest())


def _book_row(clock, revision, ask, ask_shares, *, bid=0, level=1, **fields):
    return dict(insCode=CODE, dEven=NATIVE_DAY, hEven=clock, refID=revision, number=level,
                pMeDem=bid, qTitMeDem=1000 if bid else 0, zOrdMeDem=1 if bid else 0,
                pMeOf=ask, qTitMeOf=ask_shares, zOrdMeOf=1 if ask else 0) | fields


def _depth(clock, revision, ask, ask_shares, *, bid=0):
    return [_book_row(clock, revision, ask, ask_shares, bid=bid), *
            [_book_row(clock, revision, 0, 0, level=level) for level in range(2, 6)]]


def _bound(stamp=80000, lower=95, upper=105):
    return dict(insCode=CODE, dEven=NATIVE_DAY, hEven=stamp, psGelStaMin=lower, psGelStaMax=upper)


def _run(book, prints, *, limits=None, mismatch=False):
    trades = [dict(nTran=i, hEven=clock, pTran=price, qTitTran=size, canceled=0, insCode=CODE, dEven=NATIVE_DAY)
              for i, (clock, price, size) in enumerate(prints, 1)]
    prices = [price for _, price, _ in prints]
    daily_values = dict(insCode=CODE, dEven=NATIVE_DAY, priceFirst=prices[0], priceMax=max(prices),
        priceMin=min(prices), pDrCotVal=prices[-1], pClosing=prices[-1], qTotTran5J=sum(n for _, _, n in prints),
        zTotTran=len(trades), qTotCap=sum(p*n for _, p, n in prints)+int(mismatch))
    daily = dict(source='tsetmc', price_unit='Rial', ins_code=CODE, requested_date=DAY, received_at=RECEIVED,
                 url=daily_request(CODE, DAY)[1], payload={'closingPriceDaily': daily_values})
    book = _observation(book_request(CODE, DAY), {'bestLimitsHistory': book})
    threshold = _observation(context_requests(CODE, DAY)[1], {'staticThreshold': limits or [_bound()]})
    tape = trade_observation(_observation(trade_request(CODE, DAY), {'tradeHistory': trades}), CODE, DAY)
    return study_sell_wall(book, threshold, tape, daily, CODE, DAY)


def test_depletion_uses_strictly_between_floor_prints_and_disjoint_decision_windows():
    book = (_depth(91000, 1, 95, 5_500_000) + _depth(91010, 2, 96, 12000, bid=95)
            + _depth(91020, 3, 98, 400, bid=97))
    tape = [(91000, 95, 7_000_000), (91004, 95, 800_000), (91008, 95, 300_000), (91010, 95, 2_000_000),
            (91014, 96, 1000), (91015, 96, 100), (91021, 98, 300), (91025, 97, 700), (91115, 99, 50)]
    result = _run(book, tape)
    assert result['status'] == 'depletion_observed'
    assert set(result['daily']['deltas'].values()) == {0}
    event = result['event']
    assert event['elapsed_seconds'] == 10 and event['time'] == '09:10:10'
    assert event['observed_floor_trade_shares'] == 1_100_000 and event['min_floor_volume_met']
    assert event['depletion_beyond_floor_trade_volume_shares'] == 4_400_000
    assert event['five_levels_at_anchor'] and event['five_levels_at_event']
    assert event['event_ask_rial'] == 96 and event['remaining_floor_ask_shares'] == 0
    first, second = event['windows']
    assert (first['start'], first['end_exclusive'], first['observed_shares']) == ('09:10:15', '09:11:15', 1100)
    assert first['first_visible_ask'] == dict(time='09:10:20', price_rial=98, displayed_shares=400)
    assert second['observed_shares'] == 750 and second['first_visible_ask'] is None
    assert not result['execution_verified'] and not result['phase_verified']


@pytest.mark.parametrize('interrupt', ['same_second_disagreement', 'depth_contradiction', 'limit_revision', 'late'])
def test_no_chain_across_ambiguous_or_expired_anchors(interrupt):
    book = _depth(91000, 1, 95, 5_000_000) + _depth(91010, 3, 96, 1000, bid=95)
    limits = [_bound()]
    if interrupt == 'same_second_disagreement':
        book += [_book_row(91005, 2, 95, 1000), _book_row(91005, 4, 96, 1000)]
    elif interrupt == 'depth_contradiction':
        book += [_book_row(91005, 2, 95, 900_000), _book_row(91005, 2, 0, 0, bid=95, level=2)]
    elif interrupt == 'limit_revision':
        limits.append(_bound(91008, 95, 106))
    else:
        book[5:] = _depth(91201, 3, 96, 1000, bid=95)
    out = _run(book, [(91001, 95, 1_500_000), (91202, 96, 12000)], limits=limits)
    assert out['status'] == 'depletion_not_observed' and out['wall_observed'] and out['event'] is None


def test_price_below_fee_or_no_trades_later_does_not_change_observed_event():
    book = [_book_row(91000, 1, 95, 5_000_000), _book_row(91010, 2, 95, 1_000_000)]
    result = _run(book, [(91004, 95, 900_000)])
    event = result['event']
    assert result['status'] == 'depletion_observed'
    assert event['remaining_floor_ask_shares'] == 1_000_000 and not event['min_floor_volume_met']
    assert not event['five_levels_at_anchor'] and not event['five_levels_at_event']
    assert event['windows'][0]['vwap_rial'] is None
    revised = _run(book + _depth(112000, 3, 98, 800, bid=97), [(91004, 95, 900_000), (112005, 98, 200)])
    assert revised['event'] == event


def test_nonzero_daily_delta_preserves_unavailable_event_and_holdout_is_rejected():
    book = _depth(91000, 1, 95, 5_000_000) + _depth(91010, 2, 96, 2000, bid=95)
    result = _run(book, [(91005, 95, 1_000_000), (91020, 96, 500)], mismatch=True)
    assert result['status'] == 'daily_mismatch' and result['event'] is None and result['wall_observed'] is None
    with pytest.raises(ValueError, match='pre-holdout'):
        study_sell_wall(None, None, None, None, CODE, settings.RESEARCH_CONFIG['holdout_start'])
