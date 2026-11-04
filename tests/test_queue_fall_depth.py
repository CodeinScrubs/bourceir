from copy import deepcopy

from core.queue_fall_depth import review_depth
from core.queue_fall_study import book_precheck, book_request, study_session
from tests.test_queue_fall_study import CODE, DAY, book_row, make_inputs, observation


def inputs_with_levels(levels):
    book, threshold, tape, daily = make_inputs(levels)
    return book, threshold, tape, daily, study_session(book, threshold, tape, daily)


def all_levels():
    return [dict(book_row(), number=i) for i in range(1, 6)]


def test_only_all_five_explicitly_empty_levels_remain_observed():
    book, threshold, _, _, original = inputs_with_levels(all_levels())
    reviewed = review_depth(book, threshold, CODE, DAY, original)
    assert reviewed['state'] == 'observed'
    assert reviewed['depth_review']['explicit_five_empty'] == 1
    assert reviewed['observed'][0]['same_revision_depth_levels'] == [1, 2, 3, 4, 5]
    assert original['observed'][0].get('same_revision_depth_levels') is None


def test_missing_depth_demotes_without_erasing_the_candidate():
    book, threshold, _, _, original = inputs_with_levels([book_row()])
    reviewed = review_depth(book, threshold, CODE, DAY, original)
    assert original['state'] == 'observed'
    assert reviewed['state'] == 'provisional'
    assert reviewed['pre_09_clock_provisional'] and not reviewed['pre_09_clock_observed']
    assert 'same_revision_depth_levels_missing' in reviewed['provisional'][-1]['issues']
    assert reviewed['provisional'][-1]['later_trade']['price_rial'] == 10290


def test_nonempty_deeper_ask_is_a_conflict_not_an_empty_book():
    levels = all_levels()
    levels[1].update(pMeOf=10500, qTitMeOf=20, zOrdMeOf=1)
    book, threshold, _, _, original = inputs_with_levels(levels)
    reviewed = review_depth(book, threshold, CODE, DAY, original)
    assert reviewed['state'] == 'provisional'
    assert reviewed['depth_review']['conflicting_levels'] == 1
    assert 'same_revision_ask_depth_conflict' in reviewed['provisional'][-1]['issues']


def test_conflicting_duplicate_level_stays_provisional():
    levels = all_levels()
    extra = deepcopy(levels[1])
    extra.update(pMeOf=10500, qTitMeOf=1, zOrdMeOf=1)
    book, threshold, _, _, original = inputs_with_levels(levels + [extra])
    assert review_depth(book, threshold, CODE, DAY, original)['state'] == 'provisional'


def test_upper_bid_in_deeper_level_cannot_be_a_negative_observation():
    first = book_row(price=10499)
    second = dict(book_row(price=10500), number=2)
    book, threshold, tape, daily = make_inputs([first, second])
    assert book_precheck(book, threshold, CODE, DAY)['state'] == 'no_upper_bid_observed'
    original = study_session(book, threshold, tape, daily)
    assert original['state'] == 'not_observed'
    reviewed = review_depth(book, threshold, CODE, DAY, original)
    assert reviewed['state'] == 'unknown'
    assert reviewed['depth_review']['deeper_upper_bid_needs_review']


def test_other_revision_does_not_supply_missing_levels():
    first = book_row()
    other = [dict(book_row(ref=2), number=i) for i in range(2, 6)]
    book, threshold, _, _, original = inputs_with_levels([first, *other])
    reviewed = review_depth(book, threshold, CODE, DAY, original)
    assert reviewed['state'] == 'provisional'
    assert reviewed['provisional'][-1]['same_revision_depth_levels'] == [1]


def test_malformed_deeper_bid_is_not_an_absence():
    first = book_row(price=10499)
    second = dict(book_row(), number=2, qTitMeDem=-1)
    book, threshold, tape, daily = make_inputs([first, second])
    original = study_session(book, threshold, tape, daily)
    assert original['state'] == 'not_observed'
    assert review_depth(book, threshold, CODE, DAY, original)['state'] == 'unknown'


def test_raw_source_hash_is_checked():
    book, threshold, _, _, original = inputs_with_levels(all_levels())
    damaged = observation(book_request(CODE, DAY), {'bestLimitsHistory': all_levels()})
    damaged['body_sha256'] = '0' * 64
    try:
        review_depth(damaged, threshold, CODE, DAY, original)
    except ValueError as exc:
        assert 'hash mismatch' in str(exc)
    else:
        raise AssertionError('A changed source hash must be rejected')
