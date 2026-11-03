"""Prospective factual attention flags, not predictions, rankings or trade signals."""

import hashlib
import json
from datetime import datetime

import numpy as np
import pandas as pd

from core import market_rules
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'opportunity-radar-v1'
REASONS = {'breakout': 'عبور پایانی از سقف۶۰جلسهٔ گذشته',
           'value_surge': 'ارزش معاملات حداقل۲برابر میانگین۲۰جلسهٔ قبل، با روز مثبت',
           'locked_up': 'معامله تک‌قیمتی مثبت؛ قرینهٔ صف و بدون تأیید امکان خرید'}
RAW = ['open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value']


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def stamp(value):
    try:
        time = datetime.fromisoformat(value)
        if time.tzinfo is not None:
            return time.astimezone(TEHRAN_TZ)
    except (TypeError, ValueError):
        pass
    raise ValueError('Explicit timezone-aware observation time required')


def scope_rows(rows):
    """Keep stopped/inactive ordinary shares. Absence is not delisting."""
    by_code = {}
    for item in rows:
        symbol, code = normalize_symbol(item.get('symbol_fa')), str(item.get('ins_code') or '')
        kind = item.get('instrument_type_code')
        if isinstance(kind, bool) or kind not in (300, 303) or not symbol or symbol[-1:].isdigit() or not code.isdigit():
            continue
        clean = {key: item.get(key) for key in ('symbol_fa', 'name_fa', 'ins_code', 'instrument_type_code', 'sector_code')}
        clean.update(symbol_fa=symbol, ins_code=code)
        if code in by_code and by_code[code]['symbol_fa'] != symbol:
            clean['previous_symbols'] = sorted(set(by_code[code].get('previous_symbols', [])) | {by_code[code]['symbol_fa']})
        elif code in by_code and by_code[code].get('previous_symbols'):
            clean['previous_symbols'] = by_code[code]['previous_symbols']
        if item.get('previous_symbols'):
            clean['previous_symbols'] = sorted(set(clean.get('previous_symbols', [])) | set(item['previous_symbols']))
        by_code[code] = clean
    if len(by_code) > 1000:
        raise ValueError('Scope exceeds fixed budget; no silent truncation allowed')
    names={}
    for row in by_code.values():
        names.setdefault(row['symbol_fa'],[]).append(row)
    for matches in names.values():
        if len(matches)>1:
            for row in matches:
                row['symbol_identity_ambiguous']=True
    return sorted(by_code.values(), key=lambda x: x['ins_code'])


def observe_history(frame, calendar, *, session_date):
    """Apply three previously studied definitions without fitting or using outcomes.

    The caller registers collection now, archives current inputs and never backdates
    an attention event to a historical candle or a notice publication date.
    """
    days = list(calendar)
    if (not is_canonical_iso_date(session_date) or not days or days != sorted(set(days))
            or any(not is_canonical_iso_date(d) for d in days) or session_date != days[-1]):
        raise ValueError('Explicit sorted calendar ending at the requested session required')
    output = dict(date=session_date, status='unavailable', close_final_rial=None, daily_value_rial=None,
        pit_daily_return=None, value_ratio_prior20=None, breakout_ratio_prior60=None,
        reasons={key: None for key in REASONS}, expected_session_calendar_verified=False,
        probability_of_surge=None, accepted_for_signals=False, economic_return=None)
    if frame is None or frame.empty:
        return output
    if (frame.columns.duplicated().any() or not {'date', *RAW} <= set(frame)
            or any(not is_canonical_iso_date(d) or d > session_date for d in frame.date)
            or frame.date.duplicated().any()):
        return dict(output, status='invalid_history')
    own = frame.sort_values('date').copy()
    output['last_observed_date'] = str(own.date.iloc[-1])
    p = own.set_index('date').reindex(days)
    for column in RAW:
        source = p[column].mask(p[column].map(lambda v: isinstance(v, (bool, np.bool_))))
        p[column] = pd.to_numeric(source, errors='coerce').replace([np.inf, -np.inf], np.nan)
    valid = (p[RAW].notna().all(axis=1) & p.volume.gt(0) & p.volume.mod(1).eq(0)
        & p.value.gt(0) & p.low_raw.gt(0) & p.high_raw.ge(p.low_raw)
        & p.open_raw.between(p.low_raw, p.high_raw) & p.close_raw.between(p.low_raw, p.high_raw)
        & p.close_final_raw.gt(0) & p.ref_price_raw.gt(0) & market_rules.daily_vwap(p).notna())
    for day in own.loc[~own.date.isin(days), 'date']:
        index = np.searchsorted(days, day)
        if index < len(days):
            valid.iloc[index] = False
    if session_date not in set(own.date):
        return dict(output, status='stale_session')
    if not valid.iloc[-1]:
        return dict(output, status='invalid_current_bar')
    returns = market_rules.pit_daily_returns(p).where(valid)
    if own.date.iloc[0] in p.index:
        returns.loc[own.date.iloc[0]] = np.nan
    logs = np.log1p(returns)
    index = np.exp(logs.fillna(0.).cumsum())
    complete61 = valid.rolling(61, min_periods=61).sum().eq(61)
    breakout = (index/index.shift(1).rolling(60, min_periods=60).max()-1).where(complete61).iloc[-1]
    values = p.value.where(valid)
    previous = values.shift(1).rolling(20, min_periods=20).mean().iloc[-1]
    ratio = values.iloc[-1]/previous if np.isfinite(previous) and previous > 0 else np.nan
    daily = returns.iloc[-1]
    activity = (False if (np.isfinite(ratio) and ratio < 2) or (np.isfinite(daily) and daily <= 0) else
                True if np.isfinite(ratio) and ratio >= 2 and np.isfinite(daily) and daily > 0 else None)
    states = dict(breakout=bool(breakout > 0) if np.isfinite(breakout) else None,
                  value_surge=activity, locked_up=bool(market_rules.lock_flags(p).locked_up.iloc[-1]))
    def finite(value):
        return float(value) if np.isfinite(value) else None
    return dict(output, status='observed', close_final_rial=float(p.close_final_raw.iloc[-1]),
        daily_value_rial=float(p.value.iloc[-1]), pit_daily_return=finite(daily),
        value_ratio_prior20=finite(ratio), breakout_ratio_prior60=finite(breakout), reasons=states)


def notice_records(report, scope, *, received_at):
    """Headlines are neutral review requests. A title never establishes profit growth."""
    at = stamp(received_at)
    matches={}
    for row in scope:
        for name in [row['symbol_fa'],*row.get('previous_symbols',[])]:
            matches.setdefault(name,{})[row['ins_code']]=row
    by_name={name:next(iter(rows.values())) for name,rows in matches.items() if len(rows)==1}
    result, excluded = [], 0
    for item in report.get('letters', []) if isinstance(report, dict) else []:
        symbol = normalize_symbol(item.get('symbol_fa'))
        if symbol not in by_name:
            excluded += 1
            continue
        trace = item.get('tracing_no')
        try:
            published = stamp(item.get('published_at'))
        except ValueError:
            published = None
        url = item.get('url')
        if (type(trace) is not int or trace <= 0 or not isinstance(url, str)
                or not url.startswith(('https://codal.ir/', 'https://www.codal.ir/'))):
            excluded += 1
            continue
        clean = dict(symbol=symbol, ins_code=by_name[symbol]['ins_code'], tracing_no=trace,
            title=str(item.get('title') or ''), published_at=item.get('published_at'), source_url=url,
            received_at=received_at, publication_time_usable=published is not None and published <= at,
            document_reviewed=False, financial_improvement_verified=False)
        clean['notice_key'] = digest({k:clean[k] for k in
            ('symbol','ins_code','tracing_no','title','published_at','source_url')})
        result.append(clean)
    return result, excluded


def attention_events(previous, rows, notices, *, registered_at, observed_at, price_scan):
    """Preserve first detection, baseline origin and unresolved gaps across restarts."""
    start, at = stamp(registered_at), stamp(observed_at)
    if at < start:
        raise ValueError('Observation cannot precede activation')
    state = json.loads(json.dumps(previous))
    episodes, notice_keys = state.setdefault('episodes', {}), set(state.get('notice_keys', []))
    events = []
    if price_scan:
        for row in rows:
            for reason, current in row['reasons'].items():
                key = row['ins_code']+':'+reason
                old = episodes.get(key)
                if current is None:
                    if old:
                        old['gap_since_last_observation'] = True
                    continue
                if type(current) is not bool:
                    raise ValueError('Attention flag must be true, false or unknown')
                if current:
                    if old is None or old['active'] is False:
                        baseline = (row['date'] < start.date().isoformat()
                                    or row.get('source_initialization_baseline') is True)
                        old = dict(symbol=row['symbol'], ins_code=row['ins_code'], reason=reason,
                            first_seen_at=observed_at, first_source_date=row['date'], baseline=baseline,
                            first_ever_seen_at=old.get('first_ever_seen_at', old['first_seen_at']) if old else observed_at,
                            active=True, gap_since_last_observation=False)
                        episodes[key] = old
                        events.append(dict(old, kind='baseline_observation' if baseline else 'new_attention'))
                    elif old.get('gap_since_last_observation'):
                        events.append(dict(old, kind='attention_observed_after_gap', resumed_at=observed_at,
                            baseline=old.get('baseline',False) or row.get('source_initialization_baseline') is True))
                    old.update(last_seen_at=observed_at, last_source_date=row['date'], active=True,
                               gap_since_last_observation=False)
                elif old:
                    old.update(active=False, last_observed_false_at=observed_at, gap_since_last_observation=False)
    for row in notices:
        if row['notice_key'] not in notice_keys:
            try:
                published = stamp(row['published_at'])
            except ValueError:
                published = None
            baseline = not row['publication_time_usable'] or published is None or published < start
            events.append(dict(row, kind='baseline_notice' if baseline else 'new_notice', baseline=baseline,
                               first_seen_at=observed_at))
            notice_keys.add(row['notice_key'])
    state['notice_keys'] = sorted(notice_keys)
    return state, events
