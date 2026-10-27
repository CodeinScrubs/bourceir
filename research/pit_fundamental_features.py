"""Fail-closed, read-only as-of selection for independently verified Codal revenue.

This is an eligibility gate, not a data authenticator, predictor or trading signal.
Its evidence flags are assertions by an independent upstream verifier.
"""

import re
from collections import defaultdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

VERSION = 'pit-fundamental-availability-v1'
_TEHRAN = ZoneInfo('Asia/Tehran')
_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
# Align source-native IDs with core.instrument_history.history_request and
# research.historical_universe; preserving the ASCII string is essential for joins.
_INSTRUMENT = re.compile(r'[1-9][0-9]{0,29}\Z')
_AMOUNT = re.compile(r'(?:0|[1-9][0-9]{0,23})\Z')
_TIME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:Z|[+-][0-9]{2}:[0-9]{2})\Z')
_REQUIRED_EVIDENCE = ('identity_verified', 'value_verified', 'historical_availability_verified',
                      'template_reconciled')


def _day(value):
    if type(value) is not str:
        raise ValueError('Canonical ISO date required')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError('Canonical ISO date required') from exc
    if parsed.isoformat() != value:
        raise ValueError('Canonical ISO date required')
    return parsed


def _stamp(value):
    if type(value) is not str or not _TIME.fullmatch(value):
        raise ValueError('Explicit seconds and timezone required')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError as exc:
        raise ValueError('Valid timezone-aware timestamp required') from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError('Explicit timezone required')
    return parsed.astimezone(timezone.utc)


def _instrument(value):
    if type(value) is not str or not _INSTRUMENT.fullmatch(value):
        raise ValueError('Stable numeric instrument ID required; a mutable symbol is insufficient')
    return value


def _check_disclosures(disclosures, cutoff):
    """Check embargoed dates before inspecting any financial values in provided rows."""
    inspected = []
    for record in disclosures:
        if not isinstance(record, dict):
            raise ValueError('Disclosure must be a record')
        instrument = _instrument(record.get('instrument_id'))
        period_end = _day(record.get('period_end'))
        published = _stamp(record.get('published_at'))
        first_usable = record.get('first_usable_at')
        available = _stamp(first_usable) if first_usable is not None else None
        if record.get('historical_availability_verified') is True and available is None:
            raise ValueError('Verified availability requires a timestamp')
        if (period_end >= cutoff or published.astimezone(_TEHRAN).date() >= cutoff
                or available is not None and available.astimezone(_TEHRAN).date() >= cutoff):
            raise ValueError('Sealed holdout disclosure cannot enter development research')
        if period_end > published.astimezone(_TEHRAN).date() or available is not None and available < published:
            raise ValueError('Period end or availability contradicts publication time')
        inspected.append((record, instrument, period_end, published, available))
    return inspected


def asof_verified_monthly_revenue(decisions, disclosures, *, holdout_start):
    """One latest *verified* monthly revenue vintage per decision, otherwise explicit unknown.

    Decisions include: instrument_id, date, decision_at. Disclosures include:
    instrument_id, period_end, published_at, first_usable_at, source_sha256,
    tracing_no, amount_rial (canonical integer string), exact monthly-report
    metadata (kind, period months, consolidation, currency), and four evidence
    flags. first_usable_at must be independently justified, not inferred
    from the fiscal period or a later download. Publication at the exact decision
    instant is excluded conservatively. No output is approved for live signals.
    """
    cutoff = _day(holdout_start)
    checked_decisions = []
    seen = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            raise ValueError('Decision must be a record')
        instrument = _instrument(decision.get('instrument_id'))
        session = _day(decision.get('date'))
        instant = _stamp(decision.get('decision_at'))
        if session >= cutoff or instant.astimezone(_TEHRAN).date() != session:
            raise ValueError('Decision date is sealed or disagrees with Tehran decision time')
        if (instrument, session) in seen:
            raise ValueError('Duplicate decision date/instrument')
        seen.add((instrument, session))
        checked_decisions.append((instrument, session, instant))

    checked = _check_disclosures(disclosures, cutoff)
    by_instrument = defaultdict(list)
    source_keys = set()
    for record, instrument, period_end, published, available in checked:
        sha = record.get('source_sha256')
        amount = record.get('amount_rial')
        tracing_no = record.get('tracing_no')
        if type(sha) is not str or not _SHA256.fullmatch(sha):
            raise ValueError('Content SHA256 evidence required')
        if not (amount is None and record.get('value_verified') is False) and (
                type(amount) is not str or not _AMOUNT.fullmatch(amount)):
            raise ValueError('Verified Rial amount must be a canonical nonnegative integer string')
        if type(tracing_no) is not str or not re.fullmatch(r'[1-9][0-9]{0,17}\Z', tracing_no):
            raise ValueError('Codal tracing number required')
        if (record.get('report_kind') != 'monthly_revenue'
                or type(record.get('period_months')) is not int or record['period_months'] != 1
                or record.get('is_consolidated') is not False or record.get('currency') != 'IRR'):
            raise ValueError('Only reconciled standalone monthly revenue in Rial is supported')
        if any(type(record.get(flag)) is not bool for flag in _REQUIRED_EVIDENCE):
            raise ValueError('All evidence flags must be explicit booleans')
        key = (instrument, tracing_no, sha)
        if key in source_keys:
            raise ValueError('Duplicate source and tracing number')
        source_keys.add(key)
        by_instrument[instrument].append((record, period_end, published, available))

    output = []
    for instrument, session, instant in checked_decisions:
        evidence = by_instrument[instrument]
        # A report published after the decision must be indistinguishable from
        # no report at all. Even a nullable status can leak future coverage to
        # a model, so never base status on the full later-known source archive.
        published_asof = [item for item in evidence if item[1] <= session and item[2] < instant]
        historical = [item for item in published_asof
                      if item[3] is None or item[3] < instant]
        admitted = [item for item in historical if item[3] is not None
                    and all(item[0][flag] is True for flag in _REQUIRED_EVIDENCE)]
        base = dict(instrument_id=instrument, date=session.isoformat(), decision_at=instant.isoformat(),
                    version=VERSION, accepted_for_signals=False, probability_calibrated=False)
        if not admitted:
            status = ('historical_evidence_unverified' if historical else
                      'not_yet_available' if published_asof else 'no_source_observed')
            output.append(dict(base, status=status, amount_rial=None, source_sha256=None,
                               period_end=None, first_usable_at=None, tracing_no=None))
            continue
        # Availability is an admission gate, not a revision sequence. A later
        # correction can become usable before an older filing is verified.
        # Rank verified vintages by fiscal period and source publication time.
        rank = lambda item: (item[1], item[2])
        highest = max(map(rank, admitted))
        ties = [item for item in admitted if rank(item) == highest]
        if len(ties) != 1:
            raise ValueError('Ambiguous same-period, same-publication verified vintage')
        record, period_end, published, available = ties[0]
        # A published newer (or same-time competing) filing can invalidate an older
        # selection even when the newer filing is not yet verified/usable. Comparing
        # only eligible or already-usable filings would silently present stale
        # revenue while a competing pre-decision disclosure is already known.
        # Post-decision publications remain invisible to the entire as-of view.
        conflicting = any(
            item[1] > period_end or (
                item[1] == period_end and (
                    item[2] > published or item[2] == published and item[0] is not record
                )
            )
            for item in published_asof
        )
        if conflicting:
            output.append(dict(base, status='newer_vintage_unverified', amount_rial=None,
                               source_sha256=None, period_end=None, first_usable_at=None, tracing_no=None))
            continue
        output.append(dict(base, status='verified_as_of', amount_rial=record['amount_rial'],
                           source_sha256=record['source_sha256'], period_end=period_end.isoformat(),
                           first_usable_at=available.isoformat(), tracing_no=record['tracing_no']))
    return output
