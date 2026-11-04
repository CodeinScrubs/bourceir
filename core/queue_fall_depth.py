"""Conservative depth cross-check for the archived queue-fall research feed.

This is a second, descriptive interpretation of immutable v1 observations. A
missing level is uncertain; neither delta-feed reconstruction nor a fill is
assumed. It never changes the underlying capture or emits a trading signal.
"""

from copy import deepcopy

from core.queue_fall_study import (
    MAX_BOOK_BYTES,
    MAX_THRESHOLD_BYTES,
    _clock,
    _integer,
    _limits,
    _payload,
    book_request,
)
from core.quote_context import context_requests

VERSION = 'queue-fall-depth-review-v1'


def review_depth(book, threshold, code, day, original):
    """Only promote a witness when all five same-revision ask levels are empty.

    The v1 capture can contain only the earliest candidate in a group, so a
    demoted witness stays provisional even if a later complete witness might
    exist. The raw response remains available for a future full reconstruction.
    """
    result = deepcopy(original)
    audit = dict(version=VERSION, explicit_five_empty=0, missing_levels=0, conflicting_levels=0,
                 deeper_upper_bid_needs_review=False, delta_feed_reconstructed=False)
    result['depth_review'] = audit
    if original.get('state') not in ('observed', 'not_observed'):
        return result
    code = str(code)
    native = _payload(book, code, day, book_request(code, day), MAX_BOOK_BYTES)['bestLimitsHistory']
    limits = _limits(_payload(threshold, code, day, context_requests(code, day)[1], MAX_THRESHOLD_BYTES), code, day)
    uppers = {bounds[1] for _, bounds in limits}
    witnesses = {(entry['queue_time'], entry['ref_id']) for entry in original.get('observed', [])}
    revisions = {key: {} for key in witnesses}
    deeper_upper = False
    for row in native:
        level = _integer(row.get('number'), maximum=100)
        if not 1 <= level <= 5:
            continue
        if level > 1 and original.get('state') == 'not_observed':
            try:
                bid, quantity, orders = (_integer(row.get(field)) for field in ('pMeDem', 'qTitMeDem', 'zOrdMeDem'))
                deeper_upper |= bid in uppers and quantity > 0 and orders > 0
            except ValueError:
                deeper_upper = True  # Invalid depth cannot become an absence.
        if witnesses:
            try:
                revision = (_clock(row.get('hEven')), _integer(row.get('refID'), maximum=2**63 - 1))
            except ValueError:
                continue
            if revision not in revisions:
                continue
            try:
                ask = tuple(_integer(row.get(field)) for field in ('pMeOf', 'qTitMeOf', 'zOrdMeOf'))
            except ValueError:
                ask = None
            revisions[revision].setdefault(level, set()).add(ask)
    if original.get('state') == 'not_observed':
        if deeper_upper:
            audit['deeper_upper_bid_needs_review'] = True
            result['state'] = 'unknown'
            result.setdefault('issues', []).append('upper_bid_in_deeper_level_needs_review')
        return result
    confirmed, downgraded = [], []
    for witness in original.get('observed', []):
        by_level = revisions[(witness['queue_time'], witness['ref_id'])]
        missing = sorted(set(range(1, 6)) - set(by_level))
        conflict = any(len(values) != 1 or next(iter(values)) != (0, 0, 0) for values in by_level.values())
        if conflict:
            reason = 'same_revision_ask_depth_conflict'
            audit['conflicting_levels'] += 1
        elif missing:
            reason = 'same_revision_depth_levels_missing'
            audit['missing_levels'] += 1
        else:
            reason = None
            audit['explicit_five_empty'] += 1
        item = deepcopy(witness)
        item['same_revision_depth_levels'] = sorted(by_level)
        if reason:
            item.setdefault('issues', []).append(reason)
            downgraded.append(item)
        else:
            confirmed.append(item)
    result['observed'] = confirmed
    result['provisional'] = list(result.get('provisional', [])) + downgraded
    for bucket in ('pre_09_clock', 'post_09_clock'):
        result[bucket + '_observed'] = any(entry['bucket'] == bucket for entry in confirmed)
        result[bucket + '_provisional'] = any(entry['bucket'] == bucket for entry in result['provisional'])
    result['state'] = 'observed' if confirmed else 'provisional' if result['provisional'] else 'not_observed'
    return result
