"""Strict, experimental joins for external research observations.

This module never edits the feature store, trains a model or accepts a signal.
The canonical base owns decision times. Only explicitly declared numeric features
and content-bound observations available before those times can be joined.
"""
import hashlib
import json
import re
from datetime import datetime
from numbers import Real

import numpy as np
import pandas as pd

from config import settings
from core.text import normalize_symbol
from core.timeutils import TEHRAN_TZ, is_canonical_iso_date

VERSION = 'research-extension-panel-v1'
KEYS = ['date', 'symbol']
METADATA = {'date', 'symbol', 'feature', 'value', 'unit', 'observed_at', 'available_at',
            'source', 'source_sha256', 'module_version', 'quality_status'}
_NAME = re.compile(r'[a-z][a-z0-9_]{0,63}\Z')
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_FORBIDDEN = re.compile(r'(?:^|__)(?:label_|future_|forward_|target_|net_ret_|gross_ret_|'
                        r'entry_|exit_|planned_exit_|mfe_|mae_|adj_close|adjustment_factor)')


def _name(value):
    return isinstance(value, str) and _NAME.fullmatch(value) and not _FORBIDDEN.search(value)


def _time(value):
    """Missing/naive timestamps stay unknown; no timezone is inferred."""
    if not isinstance(value, (str, datetime)):
        return None
    try:
        stamp = pd.Timestamp(value)
        return stamp.tz_convert(TEHRAN_TZ) if not pd.isna(stamp) and stamp.tzinfo is not None else None
    except (ValueError, TypeError, OverflowError):
        return None


def _number(value):
    if isinstance(value, (bool, np.bool_)) or not isinstance(value, Real):
        return None
    try:
        return float(value) if np.isfinite(float(value)) else None
    except (OverflowError, ValueError):
        return None


def _keys(frame, before):
    if not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any() or not set(KEYS) <= set(frame):
        raise ValueError('جدول باید ستون‌های یکتای تاریخ و نماد داشته باشد')
    out = frame.copy()
    # Check the boundary before inspecting feature values, including unmatched rows.
    if any(not is_canonical_iso_date(v) or v >= before for v in out.date):
        raise ValueError('تاریخ نامعتبر یا داده پس از مرز مجاز پژوهش است')
    if any(not isinstance(v, str) or not normalize_symbol(v) for v in out.symbol):
        raise ValueError('هویت نماد باید صریح و معتبر باشد')
    out['symbol'] = out.symbol.map(normalize_symbol)
    return out


def merge_research_extension(base, observations, *, module, module_version, feature_units,
                             sources, module_code_sha256, end_exclusive):
    """Left join one research family without extending the sealed selection window.

    Observations are LONG form: one row per date/symbol/feature, plus METADATA.
    `sources` maps each declared SHA-256 to the exact preserved source bytes.
    Availability means actual strategy availability, not an effective/report date.
    Matching bytes and timestamps do not independently certify their semantics.
    Missing, bad, late and unmatched observations never become usable zeroes.
    """
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (not is_canonical_iso_date(end_exclusive) or end_exclusive > cutoff
            or not _name(module) or not isinstance(module_version, str) or not module_version.strip()
            or not isinstance(module_code_sha256, str) or not _HASH.fullmatch(module_code_sha256)):
        raise ValueError('مرز مهروموم، شناسه یا نسخه کد معتبر لازم است')
    if (not isinstance(feature_units, dict) or not feature_units
            or any(not _name(k) or not isinstance(v, str) or not v.strip() for k, v in feature_units.items())):
        raise ValueError('فهرست صریح فیچر و واحد لازم است؛ برچسب آینده فیچر نیست')
    if not isinstance(sources, dict):
        raise ValueError('بدنه منابع باید صریح باشد')
    for digest, body in sources.items():
        if (not isinstance(digest, str) or not isinstance(body, bytes)
                or hashlib.sha256(body).hexdigest() != digest):
            raise ValueError('اثر انگشت منبع با بایت‌های آن برابر نیست')
    out = _keys(base, end_exclusive)
    if 'decision_at' not in out or out.duplicated(KEYS).any():
        raise ValueError('پایه باید کلید یکتا و زمان تصمیم صریح داشته باشد')
    if any(_FORBIDDEN.search(str(c)) for c in out.columns):
        raise ValueError('پایه پژوهشی نباید برچسب نتیجه یا تعدیل آینده داشته باشد')
    decisions = out.decision_at.map(_time)
    if any(t is None or t.date().isoformat() != day for day, t in zip(out.date, decisions)):
        raise ValueError('زمان تصمیم پایه باید منطقه زمانی و تاریخ تهران منطبق داشته باشد')
    obs = _keys(observations, end_exclusive)
    if not METADATA <= set(obs) or set(obs) - METADATA - {'decision_at'}:
        raise ValueError('ستون‌های ورودی ماژول با قرارداد صریح برابر نیستند')
    if any(not isinstance(f, str) or f not in feature_units for f in obs.feature):
        raise ValueError('فیچر اعلام‌نشده وارد پژوهش نمی‌شود')
    if obs.duplicated([*KEYS, 'feature']).any():
        raise ValueError('نسخه یا کلید تکراری باید پیش از ادغام تطبیق داده شود')
    names = {f: f'{module}__{f}' for f in feature_units}
    added = [c for n in names.values() for c in (n, n+'__status')]
    if set(added) & set(out):
        raise ValueError('نام خروجی ماژول با ستون موجود برخورد دارد')
    for name in names.values():
        out[name] = np.nan
        out[name+'__status'] = 'no_observation'
    positions = {(d, s): i for i, (d, s) in enumerate(zip(out.date, out.symbol))}
    audit_rows, counts, outside = [], {}, 0
    for row in obs.to_dict('records'):
        key = row['date'], row['symbol']
        if key not in positions:
            outside += 1
            continue
        pos, feature = positions[key], row['feature']
        decision = decisions.iloc[pos]
        if 'decision_at' in row and _time(row['decision_at']) != decision:
            raise ValueError('ماژول اجازه تغییر زمان تصمیم پایه را ندارد')
        observed, available = _time(row['observed_at']), _time(row['available_at'])
        reason = 'eligible'
        if observed is None or available is None:
            reason = 'unknown_or_naive_timestamp'
        elif not observed <= available <= decision:
            reason = 'unavailable_at_decision'
        elif not isinstance(row['module_version'], str) or row['module_version'] != module_version:
            reason = 'module_version_mismatch'
        elif not isinstance(row['quality_status'], str) or row['quality_status'] != 'valid':
            reason = 'unverified_quality'
        elif not isinstance(row['source'], str) or not row['source'].strip():
            reason = 'missing_source_identity'
        elif not isinstance(row['source_sha256'], str) or row['source_sha256'] not in sources:
            reason = 'unbound_source_bytes'
        elif not isinstance(row['unit'], str) or row['unit'] != feature_units[feature]:
            reason = 'unit_mismatch'
        elif _number(row['value']) is None:
            reason = 'unknown_or_invalid_number'
        name = names[feature]
        out.iloc[pos, out.columns.get_loc(name+'__status')] = reason
        if reason == 'eligible':
            out.iloc[pos, out.columns.get_loc(name)] = _number(row['value'])
        counts[reason] = counts.get(reason, 0) + 1
        audit_rows.append(dict(date=key[0], symbol=key[1], feature=feature, status=reason,
            source=row['source'] if isinstance(row['source'], str) else None,
            source_sha256=row['source_sha256'] if isinstance(row['source_sha256'], str) else None,
            observed_at=observed.isoformat() if observed is not None else None,
            available_at=available.isoformat() if available is not None else None,
            decision_at=decision.isoformat()))
    decisions_hash = hashlib.sha256(json.dumps([
        dict(date=d, symbol=s, decision_at=t.isoformat()) for d, s, t in zip(out.date, out.symbol, decisions)
    ], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    manifest = dict(version=VERSION, module=module, module_version=module_version,
        module_code_sha256=module_code_sha256, feature_units=dict(feature_units),
        end_exclusive=end_exclusive, holdout_start=cutoff, base_rows=len(out),
        output_rows=len(out), observation_rows=len(obs), outside_base_rows=outside,
        status_counts=counts, source_sha256=sorted(sources), base_decisions_sha256=decisions_hash,
        observations=audit_rows, native_semantics_independently_verified=False,
        historical_universe_verified=False, accepted_for_signals=False,
        profitability_proven=False, feature_store_mutated=False)
    return out, manifest
