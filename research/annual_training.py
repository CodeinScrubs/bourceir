"""Fail-closed fold-matured training view for annual synthetic event labels.

All opportunities must reach their full 252-session endpoint before supervised
training, including early hits. Otherwise successes enter the sample earlier
than failures and produce outcome-dependent maturity selection bias.
"""

from datetime import date
from numbers import Real

import numpy as np
import pandas as pd

VERSION = 'annual-multibagger-training-asof-v1'
HORIZON_SESSIONS = 252
_TARGETS = {2.0: 'double', 3.0: 'triple'}
_KNOWN = {'hit', 'complete_no_hit'}
_UNKNOWN = {
    'missing_anchor', 'invalid_anchor', 'calendar_conflict', 'right_censored',
    'missing_observation', 'unusable_no_trade_evidence',
    'no_trade_reference_conflict', 'invalid_observation', 'nonfinite_path',
}
_COLUMNS = [
    'symbol', 'date', 'target', 'training_label', 'eligible_for_training',
    'training_state',
]
_REQUIRED = {
    'symbol', 'date', 'target', 'threshold', 'horizon_sessions', 'status',
    'outcome', 'resolved_at', 'first_hit_date', 'first_hit_session',
    'nominal_end_date', 'observed_through',
}


def _day(value):
    if type(value) is not str:
        raise ValueError('Canonical ISO date is required')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Canonical ISO date is required') from exc
    if parsed.isoformat() != value:
        raise ValueError('Canonical ISO date is required')
    return value


def _optional_day(value):
    if pd.isna(value):
        return None
    return _day(value)


def _threshold(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        raise ValueError('Numeric research target required')
    number = float(value)
    if not np.isfinite(number) or number not in _TARGETS:
        raise ValueError('Only the pre-registered 2x and 3x targets are accepted')
    return number


def _outcome(value):
    if pd.isna(value):
        return None
    if not isinstance(value, (bool, np.bool_)):
        raise ValueError('Label must be a boolean or unknown, never a numeric/string proxy')
    return bool(value)


def _registered_calendar(calendar, holdout_start):
    """Validate the exact ordered decision-session list archived in the protocol."""
    if not isinstance(calendar, list) or not calendar:
        raise ValueError('A nonempty registered market-session list is required')
    sessions = [_day(value) for value in calendar]
    if sessions != sorted(set(sessions)) or sessions[-1] >= holdout_start:
        raise ValueError('Registered sessions must be unique, ordered, and pre-holdout')
    return sessions, {day: index for index, day in enumerate(sessions)}


def asof_annual_training_labels(outcomes, *, train_cutoff, holdout_start, calendar):
    """Expose labels only if their complete horizon ended before the train boundary.

    Consume only replay-verified, pre-holdout annual outcomes. A success on the
    first future day is not train-eligible until its *nominal 252-session end*.
    The cutoff is a Tehran date; same-day endpoints are excluded. The complete
    ordered calendar must come from the replay-verified registered study protocol;
    unverified nominal endpoints or hit-session offsets are rejected. Unknown
    labels retain their rows and never become false. No future-event fields are
    exposed.
    """
    train_cutoff, holdout_start = _day(train_cutoff), _day(holdout_start)
    sessions, positions = _registered_calendar(calendar, holdout_start)
    if train_cutoff >= holdout_start:
        raise ValueError('Training must not enter the sealed holdout period')
    if not isinstance(outcomes, pd.DataFrame) or outcomes.empty:
        raise ValueError('A nonempty replay-verified annual outcome frame is required')
    if outcomes.columns.duplicated().any() or not _REQUIRED <= set(outcomes.columns):
        raise ValueError('Unique annual outcome columns are required')

    records = []
    snapshot = set()
    paired = {name: set() for name in _TARGETS.values()}
    cross_target = {}
    for row in outcomes[sorted(_REQUIRED)].to_dict('records'):
        symbol, anchor = row['symbol'], _day(row['date'])
        if type(symbol) is not str or not symbol.strip():
            raise ValueError('Nonempty canonical symbol required')
        observed = _day(row['observed_through'])
        snapshot.add(observed)
        if anchor > observed or observed >= holdout_start:
            raise ValueError('A sealed or inconsistent outcome snapshot is not permitted')
        if sessions[-1] > observed:
            raise ValueError('Registered calendar includes sessions beyond the observed snapshot')
        if anchor not in positions:
            raise ValueError('Decision anchor is missing from the registered calendar')
        if type(row['horizon_sessions']) is not int and not isinstance(row['horizon_sessions'], np.integer):
            raise ValueError('Horizon must be an integer')
        if row['horizon_sessions'] != HORIZON_SESSIONS:
            raise ValueError('Only the pre-registered 252-session target is accepted')
        target = _threshold(row['threshold'])
        name = _TARGETS[target]
        if type(row['target']) is not str or row['target'] != name:
            raise ValueError('Target name and numeric threshold mismatch')
        key = (symbol, anchor)
        if key in paired[name]:
            raise ValueError('Duplicate symbol/date/target outcome')
        paired[name].add(key)

        status = row['status']
        if type(status) is not str or status not in (_KNOWN | _UNKNOWN):
            raise ValueError('Unknown event status is not a training outcome')
        truth = _outcome(row['outcome'])
        resolved = _optional_day(row['resolved_at'])
        first = _optional_day(row['first_hit_date'])
        end = _optional_day(row['nominal_end_date'])
        hit_session = row['first_hit_session']
        # An untrusted nominal_end_date can make a 252-session label appear mature
        # many months too early. Reconstruct it from the registered session index.
        end_position = positions[anchor] + HORIZON_SESSIONS
        expected_end = sessions[end_position] if end_position < len(sessions) else None
        if end != expected_end:
            raise ValueError('Nominal endpoint is not the registered 252nd future session')
        if status == 'hit':
            if truth is not True or resolved is None or first != resolved:
                raise ValueError('Hit must have a true outcome and matching first-hit resolution date')
            if (not isinstance(hit_session, Real) or isinstance(hit_session, (bool, np.bool_))
                    or not np.isfinite(hit_session) or int(hit_session) != hit_session
                    or not 1 <= int(hit_session) <= HORIZON_SESSIONS):
                raise ValueError('First hit must be a valid future session')
            hit_position = positions[anchor] + int(hit_session)
            if hit_position >= len(sessions) or sessions[hit_position] != first:
                raise ValueError('First hit date must match its registered future session')
            if not anchor < resolved <= observed or end is not None and resolved > end:
                raise ValueError('First hit must follow the anchor and precede its endpoint')
        elif status == 'complete_no_hit':
            if truth is not False or resolved is None or end != resolved:
                raise ValueError('No-hit must have a false outcome resolved at its full endpoint')
            if first is not None or not pd.isna(hit_session) or not anchor < resolved <= observed:
                raise ValueError('No-hit cannot contain a first hit or invalid endpoint')
        else:
            if truth is not None or resolved is not None or first is not None or not pd.isna(hit_session):
                raise ValueError('Unknown event cannot provide a resolved training target')
        cross_target.setdefault(key, {})[name] = (status, hit_session, end)

        # An eventual winner after the cutoff must not affect the training row.
        # Even an early success is masked until the *full* horizon matures.
        if anchor >= train_cutoff:
            continue
        eligible = (status in _KNOWN and end is not None
                    and end < train_cutoff and resolved < train_cutoff)
        records.append({
            'symbol': symbol,
            'date': anchor,
            'target': name,
            'training_label': truth if eligible else pd.NA,
            'eligible_for_training': bool(eligible),
            'training_state': (
                'resolved_hit' if status == 'hit' else 'resolved_no_hit'
            ) if eligible else 'unknown_asof',
        })

    if len(snapshot) != 1 or train_cutoff > next(iter(snapshot)):
        raise ValueError('As-of cutoff must not exceed the single observed snapshot')
    if paired['double'] != paired['triple']:
        raise ValueError('Both targets must cover exactly the same symbol/date cohort')
    for targets in cross_target.values():
        double, triple = targets['double'], targets['triple']
        if double[2] != triple[2]:
            raise ValueError('2x/3x outcomes must have the same horizon endpoint')
        if triple[0] == 'hit' and double[0] != 'hit':
            raise ValueError('3x cannot occur before a 2x event')
        if triple[0] == 'hit' and double[1] > triple[1]:
            raise ValueError('2x first hit cannot occur after a 3x first hit')
        # Both targets run over exactly the same observed price/reference path.
        # A complete 2x no-hit proves a 3x no-hit; an established 3x no-hit
        # proves the 2x outcome cannot still be unknown. Unknown path failures
        # cannot differ while neither threshold has been hit. Without these
        # checks, individually valid rows can create a fabricated training base rate.
        if double[0] == 'complete_no_hit' and triple[0] != 'complete_no_hit':
            raise ValueError('Complete 2x no-hit requires a complete 3x no-hit')
        if triple[0] == 'complete_no_hit' and double[0] in _UNKNOWN:
            raise ValueError('Complete 3x no-hit cannot coexist with an unknown 2x path')
        if (double[0] in _UNKNOWN and triple[0] in _UNKNOWN
                and double[0] != triple[0]):
            raise ValueError('Unresolved 2x/3x paths must have the same failure status')

    output = pd.DataFrame(records, columns=_COLUMNS)
    output['training_label'] = pd.array(output['training_label'], dtype='boolean')
    output['eligible_for_training'] = output['eligible_for_training'].astype(bool)
    return output.sort_values(['date', 'symbol', 'target']).reset_index(drop=True)
