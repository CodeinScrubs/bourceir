"""Whole, evidenced dated candles in a derived history view; no price repair or I/O."""

import math
import re

import pandas as pd

from core.daily_publications import _clock, parse_publication
from core.timeutils import is_canonical_iso_date

VERSION = 'dated-history-composition-v1'
FIELDS = ('open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw',
          'ref_price_raw', 'volume', 'value', 'trade_count')
UNKNOWN_STATES = {'conflicting_publications'}


def _same_row(left, right):
    for field in FIELDS:
        a, b = left.get(field), right.get(field)
        if (isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float))
                or not isinstance(b, (int, float)) or not math.isfinite(a) or not math.isfinite(b)
                or a != b):
            return False
    return True


def compose_history(history, publications, *, ins_code, instrument_type_code, session_date,
                    observed_at, registered_at, max_source_age_seconds=None):
    """Validate bytes again, append only a missing whole day, quarantine disagreements.

    ``publications`` contains successful native price/zero receipts verified by the
    cache reader. Rejected/unavailable attempts remain in its separate snapshot;
    they supply no replacement values here. All agreeing vintages are retained.
    """
    if (not isinstance(ins_code, str) or not re.fullmatch(r'[1-9][0-9]{0,29}', ins_code)
            or type(instrument_type_code) is not int or instrument_type_code not in (300, 303)
            or not is_canonical_iso_date(session_date) or not isinstance(publications, list)):
        raise ValueError('Composition requires an explicit ordinary-stock identity and session')
    at, registered = _clock(observed_at), _clock(registered_at)
    if registered > at or session_date > at.date().isoformat():
        raise ValueError('Composition cannot precede registration or consume a future session')
    if max_source_age_seconds is not None and (
            type(max_source_age_seconds) is not int or max_source_age_seconds < 1):
        raise ValueError('An optional source-age policy must be an explicit positive integer')
    if history is not None and (not isinstance(history, pd.DataFrame)
            or history.columns.duplicated().any() or not {'date', *FIELDS} <= set(history)
            or history.date.duplicated().any()
            or any(not is_canonical_iso_date(day) or day > session_date for day in history.date)):
        raise ValueError('History composition cannot repair invalid or future input keys')
    view = None if history is None else history.copy(deep=True)
    present = history.loc[history.date.eq(session_date)] if history is not None else None
    native_row = present.iloc[0].to_dict() if present is not None and not present.empty else None
    references, rows, zero = [], [], False
    seen = set()
    for item in publications:
        if not isinstance(item, dict):
            raise ValueError('A composition publication must be a verified receipt')
        origin, file, digest = (item.get(key) for key in ('archive_id', 'file', 'record_sha256'))
        if (not isinstance(origin, str) or not re.fullmatch(r'DAYPUB-[0-9a-f]{32}', origin)
                or not isinstance(file, str) or not re.fullmatch(r'observation-[0-9]{5}\.json', file)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)
                or (origin, file) in seen):
            raise ValueError('Publication provenance is invalid or duplicated')
        seen.add((origin, file))
        archive_start, archive_end, attempt, complete = (_clock(item.get(key)) for key in
            ('archive_registered_at', 'archive_completed_at', 'attempted_at', 'completed_at'))
        if not archive_start <= attempt <= complete <= archive_end <= at:
            raise ValueError('A cache receipt was not available at the observation time')
        observation = item.get('observation')
        publication = parse_publication(observation, ins_code, session_date, 'price',
                                         requested_after=item['attempted_at'], received_by=item['completed_at'])
        if publication != item.get('parsed'):
            raise ValueError('Stored dated publication differs from its native bytes')
        received = _clock(observation['received_at'])
        age = (at-received).total_seconds()
        if max_source_age_seconds is not None and age > max_source_age_seconds:
            raise ValueError('Dated source exceeded the registered source-age policy')
        references.append(dict(archive_id=origin, file=file, record_sha256=digest,
            body_sha256=observation['body_sha256'], original_requested_at=observation['requested_at'],
            original_received_at=observation['received_at'], archive_available_at=item['archive_completed_at'],
            status=publication['status'], source_age_seconds=age))
        if publication['status'] == 'observed_zero_activity':
            zero = True
        elif publication['status'] == 'valid_price':
            rows.append(publication['row'])
        else:
            raise ValueError('Unsupported dated-publication state')
    status = 'history_only' if native_row is not None else 'cache_missing'
    if rows:
        candidate = rows[0]
        conflict = zero or any(not _same_row(candidate, other) for other in rows[1:])
        conflict |= native_row is not None and not _same_row(native_row, candidate)
        if conflict:
            status = 'conflicting_publications'
        elif native_row is not None:
            status = 'matching_publications'
        else:
            addition = pd.DataFrame([candidate], columns=['date', *FIELDS])
            view = addition if view is None or view.empty else pd.concat([view, addition], ignore_index=True)
            view = view.sort_values('date').reset_index(drop=True)
            status = 'appended_whole_dated_candle'
    elif zero:
        status = 'conflicting_publications' if native_row is not None else 'zero_activity_no_candle'
    if status == 'conflicting_publications' and view is not None:
        # Preserve the raw input, but the disputed day supplies no rolling values.
        view = view.loc[view.date.ne(session_date)].reset_index(drop=True)
    used = status in ('matching_publications', 'appended_whole_dated_candle')
    baseline = used and any(_clock(ref['original_received_at']) < registered for ref in references)
    return view, dict(version=VERSION, ins_code=ins_code, session_date=session_date, status=status,
        cache_consumed_at=observed_at, native_history_modified=False, references=references,
        source_initialization_baseline=bool(baseline), max_source_age_seconds=max_source_age_seconds,
        original_receipt_times_preserved=True, first_publication_time_verified=False,
        full_session_coverage_verified=False, independent_source_correctness_verified=False,
        accepted_for_signals=False, profitability_proven=False)
