"""صف بررسی حداکثر پنج نماد؛ ترتیب توجه اپراتور، بدون پیش‌بینی سود."""

from core.feed_health import _number
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date


def inspection_queue(candidates, *, session_date, limit=5):
    if not is_canonical_iso_date(session_date) or type(limit) is not int or not 1 <= limit <= 5:
        raise ValueError('جلسه صریح و سقف یک تا پنج نماد لازم است')
    candidates = list(candidates)
    grouped, excluded = {}, []
    for row in candidates:
        value = _number(row.get('daily_value_rial'))
        symbol = normalize_symbol(row.get('symbol'))
        reason = ('unknown_symbol' if not symbol else 'other_session' if row.get('date') != session_date else
            'research_universe_not_verified' if row.get('research_universe_eligible') is not True else
            'unknown_traded_value' if value is None or value <= 0 else None)
        if reason:
            excluded.append(dict(symbol=symbol, hypothesis=row.get('hypothesis'), reason=reason))
            continue
        if symbol in grouped and grouped[symbol]['daily_value_rial'] != value:
            raise ValueError('ارزش روز یک نماد در دو فرضیه متناقض است')
        item = grouped.setdefault(symbol, dict(symbol=symbol, daily_value_rial=value, hypotheses=[],
            linked_manual_plan=False, blockers=[], accepted_for_signals=False))
        item['hypotheses'] = sorted(set(item['hypotheses']) | {row['hypothesis']})
        item['linked_manual_plan'] |= row.get('linked_manual_plan_id') is not None
        item['blockers'] = sorted(set(item['blockers']) | set(row.get('blockers', [])))
    ordered = sorted(grouped.values(), key=lambda row: (not row['linked_manual_plan'], -row['daily_value_rial'], row['symbol']))
    return dict(limit=limit, candidate_rows=len(candidates), unique_eligible_symbols=len(ordered),
        displayed=ordered[:limit], deferred_symbols=[row['symbol'] for row in ordered[limit:]], excluded=excluded,
        ordering='existing manual plan, observed traded value descending, normalized symbol; not predicted profit',
        all_candidates_retained=True, accepted_for_signals=False)
