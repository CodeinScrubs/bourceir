"""Conservative discovery links between reviewed registration facts and right starts.

Symbol equality is a search aid only. It does not establish event identity, issue
entitlement, actual opening or closing, discount, or the hypothesis outcome.
"""

from datetime import date

from config import settings
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date


def candidate_links(inventory, notices):
    """Link every same-symbol candidate; keep ambiguity and duplicate provenance."""
    if not isinstance(inventory, dict) or not isinstance(notices, list):
        raise ValueError('Inventory and a list of reviewed notices are required')
    start, end = inventory.get('source_start'), inventory.get('end_date_exclusive')
    if not (is_canonical_iso_date(start) and is_canonical_iso_date(end) and start < end
            and end <= settings.RESEARCH_CONFIG['holdout_start']):
        raise ValueError('A canonical inventory window is required')
    candidates = inventory.get('candidate_rows')
    starts = inventory.get('candidate_starts')
    if not isinstance(candidates, list) or not isinstance(starts, list):
        raise ValueError('Inventory candidate rows and starts are required')
    by_code = {}
    for row in candidates:
        code, parents = row.get('ins_code'), row.get('parents')
        if (not isinstance(code, str) or not code.isascii() or not code.isdigit()
                or code.startswith('0') or code in by_code):
            raise ValueError('Unique canonical instrument identities are required')
        if not isinstance(parents, list) or not parents:
            raise ValueError('Explicit candidate parent metadata is required')
        symbols = []
        for parent in parents:
            symbol = parent.get('parent_symbol')
            if not isinstance(symbol, str) or not normalize_symbol(symbol):
                raise ValueError('Explicit parent symbol is required')
            symbols.append(normalize_symbol(symbol))
        by_code[code] = {'symbols': set(symbols), 'parent_count': len(parents)}
    by_symbol = {}
    seen_starts = set()
    for row in starts:
        code, day = row.get('ins_code'), row.get('date')
        if code not in by_code or not is_canonical_iso_date(day) or not start <= day < end:
            raise ValueError('Candidate starts must have a known identity and lie in the inventory window')
        if (code, day) in seen_starts or type(row.get('left_censored')) is not bool:
            raise ValueError('Candidate starts must be unique and explicitly censored')
        seen_starts.add((code, day))
        for symbol in by_code[code]['symbols']:
            by_symbol.setdefault(symbol, []).append({'ins_code': code, 'date': day,
                'left_censored': row['left_censored'], 'parent_count': by_code[code]['parent_count']})
    groups = {}
    for notice in notices:
        tracing, symbol, meeting = notice.get('tracing_no'), notice.get('symbol'), notice.get('meeting_date')
        study = notice.get('study_id')
        if (type(tracing) is not int or tracing <= 0 or not isinstance(symbol, str)
                or not normalize_symbol(symbol) or not is_canonical_iso_date(meeting)
                or not isinstance(study, str) or not study):
            raise ValueError('Reviewed notice identity, meeting date and study are required')
        if not start <= meeting < end:
            raise ValueError('Meeting outside the inventory window')
        key = (normalize_symbol(symbol), meeting)
        if tracing in groups and groups[tracing]['key'] != key:
            raise ValueError('Conflicting identity or meeting for the same tracing number')
        groups.setdefault(tracing, {'key': key, 'studies': set()})['studies'].add(study)
    out = []
    for tracing, group in sorted(groups.items()):
        symbol, meeting = group['key']
        matches = []
        for row in by_symbol.get(symbol, []):
            matches.append({**row, 'days_from_meeting': (date.fromisoformat(row['date'])
                                                         - date.fromisoformat(meeting)).days})
        matches.sort(key=lambda x: (x['date'], x['ins_code']))
        out.append({'tracing_no': tracing, 'symbol': symbol, 'meeting_date': meeting,
                    'source_study_ids': sorted(group['studies']), 'same_symbol_starts': matches})
    return {'notice_count': len(out), 'candidate_starts': len(seen_starts), 'notices': out,
            'links_establish_event_identity': False, 'historical_universe_complete': False,
            'x_total_capital_events': None, 'y_rights_events': None, 'z_discount_events': None,
            'a_failed_target_events': None, 'accepted_for_signals': False, 'profitability_proven': False}
