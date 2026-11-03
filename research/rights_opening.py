"""Exploratory rights-opening accounting; no order execution or trading signal.

Caller must separately document the actual cash issue and source availability. All
historical prices remain raw; observed prints are proxies, not proof of fills.
"""

import re
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation, localcontext
from fractions import Fraction

from config import settings
from core.query_screener import trade_evidence
from core.timeutils import is_canonical_iso_date, now_tehran

VERSION = 'rights-opening-research-v2'
EPISODE_VERSION = 'rights-peak-episode-v2'


def _signed_number(value):
    if isinstance(value, bool):
        raise ValueError('Boolean is not a financial number')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError('A finite financial number is required') from exc
    if not result.is_finite():
        raise ValueError('A finite financial number is required')
    return result


def _number(value, *, positive=False):
    result = _signed_number(value)
    if result < 0 or positive and result == 0:
        raise ValueError('A nonnegative finite financial number is required')
    return result


def _exact_fraction(value):
    """Convert an already validated decimal to a bounded exact rational."""
    parts = value.as_tuple()
    if len(parts.digits) > 100 or abs(parts.exponent) > 100:
        raise ValueError('Financial number exceeds the supported exact precision')
    return Fraction(value)


def _ratio_display(value):
    """A stable display value; comparisons must use the exact ratio instead."""
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        return Decimal(value.numerator) / Decimal(value.denominator)


def _right_result(value):
    return {'right_parity_rial': _ratio_display(value),
            'right_parity_exact': {'numerator': value.numerator, 'denominator': value.denominator}}


def _fixed_theory_fraction(value):
    """Accept a complete theory result or an explicitly supplied decimal assumption."""
    if not isinstance(value, dict):
        return _exact_fraction(_signed_number(value)), 'explicit_decimal'
    ratio = value.get('right_parity_exact')
    if not isinstance(ratio, dict) or set(ratio) != {'numerator', 'denominator'}:
        raise ValueError('The complete exact right parity is required')
    numerator, denominator = ratio['numerator'], ratio['denominator']
    if (type(numerator) is not int or type(denominator) is not int or denominator <= 0
            or numerator.bit_length() > 4096 or denominator.bit_length() > 4096):
        raise ValueError('Invalid exact right parity')
    exact = Fraction(numerator, denominator)
    if _signed_number(value.get('right_parity_rial')) != _ratio_display(exact):
        raise ValueError('Displayed and exact right parity conflict')
    return exact, 'exact_ratio'


def theoretical_right(*, previous_close, capital_before, cash_increase,
                      total_increase, exercise_price, dividend):
    """Theoretical ex-rights share and single right from documented amounts.

    This is accounting parity, not a prediction or guaranteed realizable value.
    The caller must explicitly supply the dividend, including zero only when
    zero is supported or is clearly identified as a scenario assumption.
    """
    before = _number(previous_close, positive=True)
    capital = _number(capital_before, positive=True)
    cash = _number(cash_increase, positive=True)
    increase = _number(total_increase, positive=True)
    exercise = _number(exercise_price, positive=True)
    distribution = _number(dividend)
    if cash > increase or distribution >= before:
        raise ValueError('Inconsistent increase or dividend')
    before, capital, cash, increase, exercise, distribution = map(
        _exact_fraction, (before, capital, cash, increase, exercise, distribution))
    share = ((before - distribution) * capital + exercise * cash) / (capital + increase)
    return {'share_rial': _ratio_display(share), **_right_result(share - exercise),
            'historical_inputs_verified': False, 'profitability_proven': False}


def reference_right_parity(*, parent_reference, exercise_price):
    """Raw reference minus exercise price; source and event identity are caller evidence."""
    parent = _number(parent_reference, positive=True)
    exercise = _number(exercise_price, positive=True)
    parity = _exact_fraction(parent) - _exact_fraction(exercise)
    return {**_right_result(parity), 'reference_cause_verified': False,
            'tradability_verified': False, 'accepted_for_signals': False}


def _scope(retrospective_cutoff_exclusive):
    """An explicitly retrospective, frozen cutoff cannot restore a clean holdout."""
    if retrospective_cutoff_exclusive is None:
        return None
    cutoff = retrospective_cutoff_exclusive
    if (not is_canonical_iso_date(cutoff) or cutoff <= '1967-01-01'
            or cutoff > now_tehran().date().isoformat()):
        raise ValueError('A canonical retrospective cutoff no later than today is required')
    return {'mode': 'retrospective_rights_only', 'cutoff_exclusive': cutoff,
            'holdout_contaminated_for_rights_hypothesis': True,
            'independent_holdout_for_rights_hypothesis': False}


def _scope_matches(opening, retrospective_cutoff_exclusive):
    scope = _scope(retrospective_cutoff_exclusive)
    if opening.get('analysis_scope') != scope:
        raise ValueError('Opening and evaluation must use the same explicit research scope')
    return scope


def _day(day, *, retrospective_cutoff_exclusive=None):
    scope = _scope(retrospective_cutoff_exclusive)
    start = '1967-01-01' if scope else settings.RESEARCH_CONFIG['dev_start']
    end = scope['cutoff_exclusive'] if scope else settings.RESEARCH_CONFIG['holdout_start']
    if not is_canonical_iso_date(day) or not start <= day < end:
        raise ValueError('Research dates must be canonical and within the explicit research scope')
    return day


def _code(value):
    code = str(value) if type(value) in (str, int) else ''
    if not re.fullmatch(r'[1-9][0-9]{0,29}', code):
        raise ValueError('A positive canonical instrument identity is required')
    return code


def _clock_seconds(clock):
    return clock // 10000 * 3600 + clock // 100 % 100 * 60 + clock % 100


def _verified_active(observation, daily):
    # Reconciliation must not inherit a caller's low Decimal precision.
    with localcontext(Context(prec=100, rounding=ROUND_HALF_EVEN)):
        evidence = trade_evidence(observation, daily)
    if not evidence['complete'] or evidence['contradictory']:
        return None, evidence
    rows = observation['response']['tradeHistory']
    by_number = {int(row['nTran']): row for row in rows if row['canceled'] == 0}
    active = [by_number[key] for key in sorted(by_number)]
    try:
        total = sum((_exact_fraction(_number(row['pTran'], positive=True))
                     * _exact_fraction(_number(row['qTitTran'], positive=True)) for row in active), Fraction())
        expected = _exact_fraction(_number(daily.get('value'), positive=True))
    except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
        return None, evidence | {'complete': False, 'issues': [*evidence['issues'], str(exc)]}
    if total != expected:
        return None, evidence | {'complete': False, 'contradictory': True,
                                 'issues': [*evidence['issues'], 'Exact transaction value does not match the daily total']}
    return active, evidence


def canceled_opening_candidate(daily, observation, *, retrospective_cutoff_exclusive=None):
    """Return a research-only opening candidate when a canceled first print is provable.

    No stored price is modified. The replacement is accepted only when the first
    active trade is later in sequence, the original first price is witnessed on
    an earlier canceled print, and all seven daily totals reconcile afterwards.
    """
    if not isinstance(daily, dict):
        raise ValueError('A dated daily row is required')
    day = _day(daily.get('date'), retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    code = _code(daily.get('ins_code'))
    out = {'version': VERSION, 'date': day, 'ins_code': code, 'status': 'unavailable',
           'original_daily': daily.copy(), 'candidate_daily': None, 'candidate_evidence': None,
           'canceled_first_trade_number': None, 'active_first_trade_number': None,
           'source_correctness_independently_verified': False, 'database_modified': False,
           'accepted_for_signals': False}
    if retrospective_cutoff_exclusive is not None:
        out['analysis_scope'] = _scope(retrospective_cutoff_exclusive)
    rows = (observation.get('response', {}).get('tradeHistory')
            if isinstance(observation, dict) and isinstance(observation.get('response'), dict) else None)
    if not isinstance(rows, list) or not rows:
        return out
    try:
        original = _number(daily.get('open_raw'), positive=True)
        active, canceled = [], []
        for row in rows:
            if not isinstance(row, dict) or type(row.get('canceled')) is not int or row['canceled'] not in (0, 1):
                raise ValueError('Explicit trade cancellation status is required')
            number = _number(row.get('nTran'), positive=True)
            price = _number(row.get('pTran'), positive=True)
            if number != int(number):
                raise ValueError('Trade sequence must be integral')
            (canceled if row['canceled'] else active).append((int(number), price))
        if not active:
            return out | {'status': 'no_active_trades'}
        active_number, active_price = min(active, key=lambda item: item[0])
        matching = [number for number, price in canceled if number < active_number and price == original]
        if not matching or original == active_price:
            return out | {'status': 'canceled_first_not_evidenced'}
        candidate_price = int(active_price) if active_price == active_price.to_integral_value() else float(active_price)
        if Decimal(str(candidate_price)) != active_price:
            raise ValueError('Candidate opening cannot be represented without changing its value')
        candidate = daily | {'open_raw': candidate_price}
        _, evidence = _verified_active(observation, candidate)
        if not evidence['complete'] or evidence['contradictory']:
            return out | {'status': 'candidate_not_reconciled', 'candidate_evidence': evidence}
        return out | {'status': 'candidate_reconciled_research_only', 'candidate_daily': candidate,
                      'candidate_evidence': evidence, 'canceled_first_trade_number': min(matching),
                      'active_first_trade_number': active_number}
    except (ValueError, TypeError, KeyError, InvalidOperation, OverflowError):
        return out | {'status': 'invalid_or_conflicting_source'}


def opening_observation(right_daily, right_observation, *, issue_confirmed,
                        notice_available_before_open, fixed_theory=None,
                        parent_daily=None, parent_observation=None,
                        exercise_price=None, max_parent_age_seconds=300,
                        retrospective_cutoff_exclusive=None):
    """Observe first print and next later-second print, without claiming an execution."""
    if not isinstance(right_daily, dict):
        raise ValueError('A dated right instrument is required')
    day = _day(right_daily.get('date'), retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    code = _code(right_daily.get('ins_code'))
    if type(issue_confirmed) is not bool or type(notice_available_before_open) is not bool:
        raise ValueError('Issue identity and notice availability must be explicit')
    if type(max_parent_age_seconds) is not int or not 1 <= max_parent_age_seconds <= 3600:
        raise ValueError('A bounded parent price age is required')
    result = {'version': VERSION, 'date': day, 'ins_code': code,
              'status': 'issue_unverified',
              'opening_rial': None, 'opening_clock': None, 'fixed_discount': None,
              'fixed_theory_representation': None,
              'floating_discount': None, 'floating_theory_rial': None,
              'parent_age_seconds': None, 'entry_proxy_rial': None, 'entry_clock': None,
              'entry_volume_observed': None, 'entry_still_below_fixed': None,
              'right_evidence': None, 'parent_evidence': None,
              'execution_verified': False, 'accepted_for_signals': False}
    if retrospective_cutoff_exclusive is not None:
        result['analysis_scope'] = _scope(retrospective_cutoff_exclusive)
    if not issue_confirmed or not notice_available_before_open:
        return result
    active, evidence = _verified_active(right_observation, right_daily)
    result['right_evidence'] = evidence
    if not active:
        return result | {'status': 'right_tape_unverified'}
    first = active[0]
    opening = _number(first['pTran'], positive=True)
    result.update(status='observed', opening_rial=str(opening), opening_clock=first['hEven'])
    if fixed_theory is not None:
        # A parent trading below the exercise cost can produce negative parity;
        # it is a meaningful non-discount, not invalid or a fabricated zero.
        fixed, representation = _fixed_theory_fraction(fixed_theory)
        result['fixed_theory_representation'] = representation
        result['fixed_discount'] = _exact_fraction(opening) < fixed
    else:
        fixed = None
    if exercise_price is not None and parent_daily is not None:
        if not isinstance(parent_daily, dict) or parent_daily.get('date') != day:
            raise ValueError('Separate same-day parent identity is required')
        if _code(parent_daily.get('ins_code')) == code:
            raise ValueError('Separate same-day parent identity is required')
        parent, parent_evidence = _verified_active(parent_observation, parent_daily)
        result['parent_evidence'] = parent_evidence
        prior = [row for row in parent or [] if row['hEven'] < first['hEven']]
        if prior:
            latest = prior[-1]
            age = _clock_seconds(first['hEven']) - _clock_seconds(latest['hEven'])
            result['parent_age_seconds'] = age
            if age <= max_parent_age_seconds:
                parity = (_exact_fraction(_number(latest['pTran'], positive=True))
                          - _exact_fraction(_number(exercise_price, positive=True)))
                result['floating_theory_rial'] = str(_ratio_display(parity))
                result['floating_discount'] = _exact_fraction(opening) < parity
    # Same-second order is not inferable from timestamps across systems; wait for a later second.
    after = next((row for row in active if row['hEven'] > first['hEven']), None)
    if after is not None:
        price = _number(after['pTran'], positive=True)
        result.update(entry_proxy_rial=str(price), entry_clock=after['hEven'],
                      entry_volume_observed=int(after['qTitTran']),
                      entry_still_below_fixed=_exact_fraction(price) < fixed if fixed is not None else None)
    return result


def horizon_observation(opening, *, market_dates, horizon, calendar_verified,
                        exit_daily, exit_observation, buy_fee, sell_fee,
                        retrospective_cutoff_exclusive=None):
    """Net first-print scenario after h observed sessions; unknowns are never zero returns."""
    if not isinstance(opening, dict):
        raise ValueError('Opening observation is required')
    scope = _scope_matches(opening, retrospective_cutoff_exclusive)
    entry_date = _day(opening.get('date'), retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    if opening.get('version') != VERSION:
        raise ValueError('A compatible opening observation is required')
    _code(opening.get('ins_code'))
    if type(horizon) is not int or horizon not in (5, 10) or type(calendar_verified) is not bool:
        raise ValueError('Five or ten sessions and an explicit calendar quality are required')
    if (not isinstance(market_dates, list) or not market_dates or
            any(not is_canonical_iso_date(day) for day in market_dates) or
            market_dates != sorted(set(market_dates)) or entry_date not in market_dates):
        raise ValueError('Sorted unique market dates including the entry day are required')
    if scope is not None:
        for day in market_dates:
            _day(day, retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    index = market_dates.index(entry_date) + horizon
    out = {'version': VERSION, 'horizon_sessions': horizon, 'calendar_verified': calendar_verified,
           'exit_date': None, 'exit_proxy_rial': None, 'net_return_pct': None,
           'status': 'missing_entry', 'execution_verified': False, 'accepted_for_signals': False}
    if scope is not None:
        out['analysis_scope'] = scope
    if opening.get('entry_proxy_rial') is None or opening.get('status') != 'observed':
        return out
    if index >= len(market_dates):
        return out | {'status': 'missing_calendar_horizon'}
    exit_date = _day(market_dates[index], retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    out['exit_date'] = exit_date
    if (not isinstance(exit_daily, dict) or exit_daily.get('date') != exit_date
            or str(exit_daily.get('ins_code')) != opening.get('ins_code')):
        return out | {'status': 'exit_daily_missing'}
    active, evidence = _verified_active(exit_observation, exit_daily)
    out['exit_evidence'] = evidence
    if not active:
        return out | {'status': 'exit_tape_unverified'}
    entry = _number(opening['entry_proxy_rial'], positive=True)
    exit_price = _number(active[0]['pTran'], positive=True)
    buy, sell = _number(buy_fee), _number(sell_fee)
    if buy >= 1 or sell >= 1:
        raise ValueError('Fee rates must be below 100 percent')
    net = (exit_price * (1 - sell) / (entry * (1 + buy)) - 1) * 100
    return out | {'status': 'modeled_price_scenario', 'exit_proxy_rial': str(exit_price),
                  'net_return_pct': str(net), 'exit_clock': active[0]['hEven'],
                  'exit_volume_observed': int(active[0]['qTitTran'])}


def peak_session_observation(opening, *, daily, observation, buy_fee, sell_fee,
                             retrospective_cutoff_exclusive=None):
    """Find observed 10% witnesses in one session, not a complete trading-window result.

    The user's gross threshold is relative to the first right print. A separate
    net threshold uses the later entry proxy and excludes same-second exits. One
    missing session can never be converted into a failure of the whole hypothesis.
    """
    if not isinstance(opening, dict) or opening.get('version') != VERSION:
        raise ValueError('A compatible opening observation is required')
    scope = _scope_matches(opening, retrospective_cutoff_exclusive)
    entry_day = _day(opening.get('date'), retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    code = _code(opening.get('ins_code'))
    if not isinstance(daily, dict):
        raise ValueError('A dated daily observation is required')
    day = _day(daily.get('date'), retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    if day < entry_day or _code(daily.get('ins_code')) != code:
        raise ValueError('The same instrument and a session on or after opening are required')
    buy, sell = _number(buy_fee), _number(sell_fee)
    if buy >= 1 or sell >= 1:
        raise ValueError('Fee rates must be below 100 percent')
    result = {'version': VERSION, 'date': day, 'status': 'opening_unverified',
              'gross_target_rial': None, 'net_target_rial': None,
              'first_gross_witness': None, 'first_gross_after_entry_witness': None,
              'first_net_after_entry_witness': None, 'peak_after_entry': None,
              'daily_evidence': None, 'execution_verified': False,
              'full_trading_window_verified': False, 'accepted_for_signals': False}
    if scope is not None:
        result['analysis_scope'] = scope
    if opening.get('status') != 'observed' or opening.get('opening_rial') is None:
        return result
    first_price = _number(opening['opening_rial'], positive=True)
    gross_target = _exact_fraction(first_price) * Fraction(11, 10)
    result['gross_target_rial'] = str(_ratio_display(gross_target))
    entry = (_number(opening['entry_proxy_rial'], positive=True)
             if opening.get('entry_proxy_rial') is not None else None)
    if entry is not None:
        net_target = (_exact_fraction(entry) * Fraction(11, 10) * (1 + _exact_fraction(buy))
                      / (1 - _exact_fraction(sell)))
        result['net_target_rial'] = str(_ratio_display(net_target))
    active, evidence = _verified_active(observation, daily)
    result['daily_evidence'] = evidence
    if not active:
        return result | {'status': 'session_unverified'}

    def describe(row):
        price = _number(row['pTran'], positive=True)
        return {'number': int(row['nTran']), 'clock': int(row['hEven']),
                'price_rial': str(price), 'volume': int(row['qTitTran'])}

    # For the original price-based hypothesis, all prints of the first session count.
    gross = next((row for row in active if
                  _exact_fraction(_number(row['pTran'], positive=True)) >= gross_target), None)
    if gross:
        result['first_gross_witness'] = describe(gross)
    after_entry = []
    if entry is not None:
        clock = _number(opening.get('entry_clock'))
        if clock != int(clock) or clock > 235959:
            raise ValueError('An explicit entry-proxy time is required')
        entry_clock = int(clock)
        if entry_clock // 10000 > 23 or entry_clock // 100 % 100 > 59 or entry_clock % 100 > 59:
            raise ValueError('Entry-proxy time must be valid HHMMSS')
        after_entry = [row for row in active if day > entry_day or row['hEven'] > entry_clock]
    later_gross = next((row for row in after_entry if
                        _exact_fraction(_number(row['pTran'], positive=True)) >= gross_target), None)
    later_net = next((row for row in after_entry if
                      _exact_fraction(_number(row['pTran'], positive=True)) >= net_target), None)
    if later_gross:
        result['first_gross_after_entry_witness'] = describe(later_gross)
    if later_net:
        result['first_net_after_entry_witness'] = describe(later_net)
    if after_entry:
        result['peak_after_entry'] = describe(max(after_entry, key=lambda row: _number(row['pTran'], positive=True)))
    return result | {'status': 'observed_session'}


def peak_episode_observation(opening, *, sessions, market_dates, window_end,
                             calendar_complete_verified, window_end_verified,
                             source_complete_verified, first_open_verified,
                             buy_fee, sell_fee, retrospective_cutoff_exclusive=None):
    """Combine dated evidence without treating an incomplete episode as a miss.

    Completeness flags are supplied evidence assessments, not independently
    verified by this function. A price witness against the supplied opening is
    conditional until both the true issue opening and end are verified. The
    later entry price and all exits remain hypothetical.
    """
    if not isinstance(opening, dict) or opening.get('version') != VERSION:
        raise ValueError('A compatible opening observation is required')
    scope = _scope_matches(opening, retrospective_cutoff_exclusive)
    start = _day(opening.get('date'), retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    code = _code(opening.get('ins_code'))
    end = _day(window_end, retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    if end < start:
        raise ValueError('Window end must not precede the opening')
    flags = (calendar_complete_verified, window_end_verified, source_complete_verified, first_open_verified)
    if any(type(flag) is not bool for flag in flags):
        raise ValueError('Independent coverage assessments must be explicit booleans')
    buy, sell = _number(buy_fee), _number(sell_fee)
    if buy >= 1 or sell >= 1:
        raise ValueError('Fee rates must be below 100 percent')
    if (not isinstance(market_dates, list) or not market_dates or len(market_dates) > 1000
            or any(not is_canonical_iso_date(day) for day in market_dates)
            or market_dates != sorted(set(market_dates)) or market_dates[0] != start
            or market_dates[-1] > end):
        raise ValueError('Ordered unique market dates covering the observed opening are required')
    for day in market_dates:
        _day(day, retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
    if not isinstance(sessions, list) or len(sessions) > 1000:
        raise ValueError('A bounded list of session evidence is required')
    by_date = {}
    for session in sessions:
        if not isinstance(session, dict) or set(session) != {'date', 'daily', 'observation'}:
            raise ValueError('Each session must explicitly provide date, daily and observation')
        day = _day(session['date'], retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
        if day not in market_dates or day in by_date:
            raise ValueError('Unknown or duplicate session date')
        by_date[day] = session

    results = []
    opening_reconciled = False
    for day in market_dates:
        session = by_date.get(day)
        if session is None or session['daily'] is None:
            results.append({'date': day, 'status': 'missing_daily'})
            continue
        daily, observation = session['daily'], session['observation']
        if (not isinstance(daily, dict) or daily.get('date') != day or
                str(daily.get('ins_code')) != code or daily.get('source') != 'tsetmc' or
                daily.get('price_unit') != 'Rial'):
            results.append({'date': day, 'status': 'daily_identity_or_source_invalid'})
            continue
        try:
            volume = _number(daily.get('volume'))
            if volume == 0:
                if (observation is not None or _number(daily.get('value')) != 0 or
                        _number(daily.get('trade_count')) != 0):
                    raise ValueError('A zero-volume day has conflicting evidence')
                results.append({'date': day, 'status': 'observed_zero_volume'})
                continue
            result = peak_session_observation(opening, daily=daily, observation=observation,
                                              buy_fee=buy, sell_fee=sell,
                                              retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
            if day == start and result['status'] == 'observed_session':
                recalculated = opening_observation(daily, observation, issue_confirmed=True,
                                                   notice_available_before_open=True,
                                                   retrospective_cutoff_exclusive=retrospective_cutoff_exclusive)
                keys = ('opening_rial', 'opening_clock', 'entry_proxy_rial', 'entry_clock',
                        'entry_volume_observed')
                def same_value(left, right):
                    return left is None and right is None or (
                        left is not None and right is not None and _number(left) == _number(right))

                opening_reconciled = all(same_value(opening.get(key), recalculated.get(key)) for key in keys)
                if not opening_reconciled:
                    result = {'date': day, 'status': 'opening_tape_conflict'}
            results.append(result)
        except (TypeError, ValueError, InvalidOperation):
            results.append({'date': day, 'status': 'invalid_session'})

    complete = (opening_reconciled and all(flags) and all(result['status'] in ('observed_session', 'observed_zero_volume')
                                    for result in results))
    fields = ('first_gross_witness', 'first_gross_after_entry_witness', 'first_net_after_entry_witness')
    witnesses = {field: next(({'date': item['date'], **item[field]} for item in results
                               if item.get(field) is not None), None) for field in fields}
    if not opening_reconciled:
        witnesses = dict.fromkeys(fields)
    entry_available = opening.get('status') == 'observed' and opening.get('entry_proxy_rial') is not None
    def conditional_value(field):
        return True if opening_reconciled and witnesses[field] is not None else False if complete and entry_available else None

    conditional_gross = (True if opening_reconciled and witnesses[fields[0]] is not None
                         else False if complete else None)
    conditional_after_gross = conditional_value(fields[1])
    conditional_after_net = conditional_value(fields[2])
    # Reconciliation proves only the supplied day's first print.  It cannot
    # identify the first day of the issue or prove that a later witness falls
    # inside its actual trading window; both require independent assessments.
    boundaries_verified = first_open_verified and window_end_verified
    gross_value = conditional_gross if boundaries_verified else None
    after_gross = conditional_after_gross if boundaries_verified else None
    after_net = conditional_after_net if boundaries_verified else None

    peaks = [{'date': item['date'], **item['peak_after_entry']} for item in results
             if item.get('peak_after_entry') is not None]
    peak = (max(peaks, key=lambda item: _number(item['price_rial'], positive=True))
            if peaks and opening_reconciled else None)
    report = {'version': VERSION, 'episode_contract_version': EPISODE_VERSION,
            'start': start, 'window_end': end, 'ins_code': code,
            'source_complete_verified': source_complete_verified,
            'calendar_complete_verified': calendar_complete_verified, 'window_end_verified': window_end_verified,
            'first_open_verified': first_open_verified, 'opening_reconciled': opening_reconciled,
            'episode_complete_by_supplied_evidence': complete,
            'expected_sessions': len(market_dates), 'observed_sessions': sum(
                item['status'] in ('observed_session', 'observed_zero_volume') for item in results),
            'unknown_session_dates': [item['date'] for item in results if
                                      item['status'] not in ('observed_session', 'observed_zero_volume')],
            'episode_boundaries_verified_by_caller': boundaries_verified,
            'gross_target_reached': gross_value, 'gross_target_after_entry_reached': after_gross,
            'net_target_after_entry_reached': after_net,
            'conditional_gross_target_reached': conditional_gross,
            'conditional_gross_target_after_entry_reached': conditional_after_gross,
            'conditional_net_target_after_entry_reached': conditional_after_net, 'witnesses': witnesses,
            'peak_after_entry_observed': peak, 'sessions': results,
            'execution_verified': False, 'accepted_for_signals': False, 'profitability_proven': False}
    if scope is not None:
        report['analysis_scope'] = scope
    return report
