"""Derived whole-candle radar inputs with frozen cache evidence and offline replay."""

import hashlib
import os
from collections import Counter
from io import BytesIO

import pandas as pd

from core.dated_history import FIELDS, VERSION, compose_history
from core.opportunity_radar import REASONS, digest, observe_history
from data_collector.publication_cache import _json, capture_cache, replay_cache
from ops.report_files import write_private_report

SESSION_SELECTION_VERSION = 'dated-composition-session-selection-v1'
COMPOSITION_FILES = ('dated-cache-source.json', 'dated-composition.json', 'derived-prices.parquet')

def _views(histories, scope, receipts, *, session_date, observed_at, registered_at):
    frames, provenance = {}, {}
    for item in scope:
        code = item['ins_code']
        frame = histories.get(code)
        if frame is not None and 'trade_count' not in frame:
            # The legacy normalized provider has no native count evidence.
            frame = frame.assign(trade_count=None)
        frames[code], provenance[code] = compose_history(frame, receipts[code], ins_code=code,
            instrument_type_code=item['instrument_type_code'], session_date=session_date,
            observed_at=observed_at, registered_at=registered_at)
    return frames, provenance


def prices_from_views(views, scope):
    frames = [frame.assign(symbol=item['symbol_fa'], ins_code=item['ins_code']) for item in scope
              if (frame := views.get(item['ins_code'])) is not None and not frame.empty]
    columns = ['date', *FIELDS, 'symbol', 'ins_code']
    return (pd.concat(frames, ignore_index=True).reindex(columns=columns) if frames
            else pd.DataFrame(columns=columns))


def collect_composition(folder, histories, scope, *, session_date, observed_at, registered_at):
    snapshot, bodies = capture_cache(scope, session_date, observed_at=observed_at)
    for name, body in bodies.items():
        if (folder/name).exists():
            if (folder/name).read_bytes() != body:
                raise ValueError('Different dated evidence cannot reuse an existing artifact name')
            continue
        with (folder/name).open('xb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    write_private_report(folder/'dated-cache-source.json', snapshot)
    receipts = replay_cache(snapshot, bodies.__getitem__, scope, session_date, observed_at=observed_at)
    views, provenance = _views(histories, scope, receipts, session_date=session_date,
                               observed_at=observed_at, registered_at=registered_at)
    record = dict(version=VERSION, observed_at=observed_at, session_date=session_date,
                  registered_at=registered_at, items=provenance, native_requests=0,
                  original_history_preserved=True, accepted_for_signals=False)
    write_private_report(folder/'dated-composition.json', record)
    prices_from_views(views, scope).to_parquet(folder/'derived-prices.parquet', index=False)
    return views, provenance


def _source_reader(folder, allowed_files):
    def source(name):
        if name not in allowed_files:
            raise ValueError('Dated evidence was not bound to the immutable cycle')
        path = (folder/name).resolve()
        if not path.is_relative_to(folder.resolve()) or path.stat().st_size > 64*1024*1024:
            raise ValueError('Dated evidence path or byte budget differs')
        with path.open('rb') as stream:
            body = stream.read(64*1024*1024+1)
        if len(body) > 64*1024*1024 or hashlib.sha256(body).hexdigest() != allowed_files[name]:
            raise ValueError('Dated evidence digest differs')
        return body
    return source


def replay_composition(folder, histories, scope, *, session_date, registered_at, allowed_files, artifact_prefix=''):
    """No live cache read: every consumed input must be in the cycle's hash inventory."""
    if artifact_prefix not in ('', 'requested-'):
        raise ValueError('Unknown dated composition artifact prefix')
    if not {artifact_prefix+name for name in COMPOSITION_FILES} <= set(allowed_files):
        raise ValueError('Missing source-bound dated composition artifacts')
    source = _source_reader(folder, allowed_files)
    snapshot = _json(source(artifact_prefix+'dated-cache-source.json'))
    recorded = _json(source(artifact_prefix+'dated-composition.json'))
    receipts = replay_cache(snapshot, source, scope, session_date, observed_at=snapshot['observed_at'])
    views, provenance = _views(histories, scope, receipts, session_date=session_date,
        observed_at=snapshot['observed_at'], registered_at=registered_at)
    expected = dict(version=VERSION, observed_at=snapshot['observed_at'], session_date=session_date,
        registered_at=registered_at, items=provenance, native_requests=0,
        original_history_preserved=True, accepted_for_signals=False)
    if digest(recorded) != digest(expected):
        raise ValueError('Dated composition provenance differs from its native receipts')
    actual = pd.read_parquet(BytesIO(source(artifact_prefix+'derived-prices.parquet')))
    desired = prices_from_views(views, scope)
    def canonical(frame):
        return frame.sort_values(['ins_code', 'date']).reset_index(drop=True)
    try:
        pd.testing.assert_frame_equal(canonical(actual), canonical(desired), check_exact=True, check_dtype=False)
    except AssertionError:
        raise ValueError('Dated composition view differs from whole-candle native replay') from None
    return views, provenance


def _session_selection(views, scope, *, requested_session, bootstrap, minimum_calendar_symbols):
    if type(bootstrap) is not bool or type(minimum_calendar_symbols) is not int or minimum_calendar_symbols < 1:
        raise ValueError('Explicit bootstrap and calendar threshold are required')
    prices = prices_from_views(views, scope)
    counts = (prices.loc[prices.volume.gt(0)].groupby('date').ins_code.nunique()
              if not prices.empty else pd.Series(dtype=int))
    calendar = sorted(counts.index[counts.ge(minimum_calendar_symbols)])
    has_requested = not prices.empty and bool(prices.date.eq(requested_session).any())
    selected = calendar[-1] if bootstrap and calendar and not has_requested else requested_session
    return dict(version=SESSION_SELECTION_VERSION, requested_session=requested_session, selected_session=selected,
        bootstrap=bootstrap, minimum_calendar_symbols=minimum_calendar_symbols, inferred_calendar=calendar,
        requested_session_present=has_requested, target_changed=selected != requested_session,
        native_requests=0, accepted_for_signals=False)


def collect_session_composition(folder, histories, scope, *, session_date, observed_at, registered_at,
                                bootstrap, minimum_calendar_symbols):
    """Reconcile the displayed baseline date, retaining the current-date selection evidence."""
    views, provenance = collect_composition(folder, histories, scope, session_date=session_date,
        observed_at=observed_at, registered_at=registered_at)
    selection = _session_selection(views, scope, requested_session=session_date, bootstrap=bootstrap,
        minimum_calendar_symbols=minimum_calendar_symbols)
    if selection['target_changed']:
        # Keep the original requested-date decision before deriving a different
        # date. Original response bodies stay immutable and shared by hash.
        for name in COMPOSITION_FILES:
            with (folder/('requested-'+name)).open('xb') as stream:
                stream.write((folder/name).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
        views, provenance = collect_composition(folder, histories, scope,
            session_date=selection['selected_session'], observed_at=observed_at, registered_at=registered_at)
    write_private_report(folder/'composition-session-selection.json', selection)
    return views, provenance, selection


def replay_session_selection(folder, histories, scope, *, session_date, registered_at, allowed_files,
                             bootstrap, minimum_calendar_symbols):
    source = _source_reader(folder, allowed_files)
    recorded = _json(source('composition-session-selection.json'))
    if recorded.get('version') != SESSION_SELECTION_VERSION:
        raise ValueError('Unknown dated session selection contract')
    views, _ = replay_composition(folder, histories, scope, session_date=session_date, registered_at=registered_at,
        allowed_files=allowed_files, artifact_prefix='requested-' if recorded.get('target_changed') else '')
    expected = _session_selection(views, scope, requested_session=session_date, bootstrap=bootstrap,
        minimum_calendar_symbols=minimum_calendar_symbols)
    if recorded != expected:
        raise ValueError('Dated composition session selection differs from frozen requested-date evidence')
    return expected


def composition_facts(frame, provenance, calendar, *, session_date):
    if provenance['session_date'] != session_date:
        raise ValueError('Displayed session differs from its dated publication provenance')
    facts = observe_history(frame, calendar, session_date=session_date)
    if provenance['status'] == 'conflicting_publications':
        facts.update(status='conflicting_publications', date=session_date, reasons={reason: None for reason in REASONS},
                     close_final_rial=None, daily_value_rial=None, pit_daily_return=None,
                     value_ratio_prior20=None, breakout_ratio_prior60=None)
    return dict(facts, price_publication_provenance=provenance,
                source_initialization_baseline=provenance['source_initialization_baseline'])


def composition_summary(provenance):
    return dict(version=VERSION, instruments=len(provenance),
                requested_session=next(iter(provenance.values()))['session_date'] if provenance else None,
                statuses=dict(sorted(Counter(p['status'] for p in provenance.values()).items())),
                initialization_baselines=sum(p['source_initialization_baseline'] for p in provenance.values()),
                native_requests=0, accepted_for_signals=False)
