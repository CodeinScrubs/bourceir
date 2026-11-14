"""Bounded, market-only role bundles for CPU learning and later compute workers.

Bundles are a reproducible development boundary, not a historical-universe or
source-availability certificate. A worker receives one role, never a database.
"""

import hashlib
import importlib.metadata
import json
import os
import re
import uuid
from dataclasses import asdict, dataclass
from functools import cached_property
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from config import settings
from core.price_quality import RAW_PRICES, price_quality
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from features.daily import FEATURE_SET_VERSION, compute_daily_features
from features.provenance import feature_environment
from research.search_ledger import canonical, identity

VERSION = 'causal-learning-bundle-v2'
PREDICTORS = ('ret_1d', 'ret_5d', 'vol_20d', 'range_pct', 'value_ratio_20d')
PRICE_COLUMNS = ('date', 'symbol', *RAW_PRICES)
ROLES = ('train', 'validation', 'test')
MAX_ROWS = 500_000
MAX_FILE_BYTES = 128 * 1024 * 1024


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def atomic_json(path, value):
    """Publish only finite JSON; a crash cannot leave a partial checkpoint."""
    path = Path(path)
    body = canonical(value).encode('utf-8')
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('Learning JSON exceeds its byte budget')
    def reject(value):
        raise ValueError('Nonfinite JSON: ' + value)
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError('Duplicate learning JSON key')
            out[key] = value
        return out
    return json.loads(path.read_bytes(), parse_constant=reject, object_pairs_hook=pairs)


def training_environment():
    base = feature_environment()
    lock = settings.BASE_DIR / 'requirements-training.lock.txt'
    content = lock.read_bytes().replace(b'\r\n', b'\n')
    packages = {}
    for name in ('gymnasium', 'scikit-learn', 'scipy', 'joblib', 'threadpoolctl',
                 'cloudpickle', 'farama-notifications', 'typing-extensions'):
        try:
            installed = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise ValueError('Install the locked optional CPU training profile first') from exc
        pins = re.findall(rf'^{re.escape(name)}==([^\s;]+)', content.decode(), re.MULTILINE)
        if installed not in pins:
            raise ValueError('Installed training package differs from the training lock: ' + name)
        packages[name] = installed
    return dict(base=base, training_lock_sha256=hashlib.sha256(content).hexdigest(), packages=packages)


def learning_code_identity():
    """Include execution/accounting and predictors, not just the fitting script."""
    files = {}
    for folder in ('backtest', 'core', 'features', 'research', 'config'):
        for path in sorted((settings.BASE_DIR / folder).rglob('*.py')):
            files[path.relative_to(settings.BASE_DIR).as_posix()] = sha256(path)
    for name in ('requirements.lock.txt', 'requirements-training.lock.txt', 'config/default.yaml'):
        files[name] = sha256(settings.BASE_DIR / name)
    return identity(files)


@dataclass(frozen=True)
class LearningPlan:
    symbols: tuple[str, ...]
    train_start: str
    train_end: str
    validation_start: str
    validation_end: str
    test_start: str
    test_end: str
    horizon: int = 2
    embargo_sessions: int = 3
    initial_capital_rial: int = 100_000_000

    def __post_init__(self):
        dates = (self.train_start, self.train_end, self.validation_start, self.validation_end,
                 self.test_start, self.test_end)
        if (not isinstance(self.symbols, tuple) or not 1 <= len(self.symbols) <= 16
                or len(set(self.symbols)) != len(self.symbols)
                or any(not isinstance(s, str) or not s or normalize_symbol(s) != s for s in self.symbols)
                or not all(is_canonical_iso_date(d) for d in dates)
                or not self.train_start < self.train_end <= self.validation_start < self.validation_end
                <= self.test_start < self.test_end <= settings.RESEARCH_CONFIG['holdout_start']
                or type(self.horizon) is not int or self.horizon not in (2, 3)
                or type(self.embargo_sessions) is not int or self.embargo_sessions < self.horizon + 1
                or type(self.initial_capital_rial) is not int or self.initial_capital_rial < 1):
            raise ValueError('Explicit ordered pre-holdout splits, ordinary symbols, horizon and capital are required')

    def bounds(self, role):
        if role not in ROLES:
            raise ValueError('Unknown learning role')
        return getattr(self, role + '_start'), getattr(self, role + '_end')


def _validate_prices(prices, symbols, calendar):
    if (set(prices) != set(PRICE_COLUMNS) or len(prices) > MAX_ROWS or prices.empty
            or prices.duplicated(['date', 'symbol']).any()
            or not prices.symbol.isin(symbols).all()
            or not prices.date.map(is_canonical_iso_date).all()
            or not prices.date.isin(calendar).all()):
        raise ValueError('Public raw price keys, schema, calendar or row budget differ')
    numeric, flags = price_quality(prices)
    if flags.any(axis=1).any():
        counts = {k: int(v) for k, v in flags.sum().items() if v}
        raise ValueError('Unusable raw bars before training: ' + canonical(counts))
    return prices.assign(**{c: numeric[c] for c in RAW_PRICES})


def prepare_bundles(prices, calendar, registry, plan, destination, *, provenance):
    """Recompute causal features; export only allowlisted market columns.

    No existing feature file, adjusted price, account balance or outcome is copied.
    All declared roles are retained even if a learner later fails on one of them.
    """
    if not isinstance(plan, LearningPlan) or not isinstance(provenance, dict) or not provenance:
        raise ValueError('A fixed plan and explicit source limitations are required')
    days = list(calendar)
    if not days or days != sorted(set(days)) or not all(is_canonical_iso_date(d) for d in days):
        raise ValueError('A declared sorted session calendar is required')
    for left, right in ((plan.train_end, plan.validation_start), (plan.validation_end, plan.test_start)):
        if sum(left <= d < right for d in days) < plan.embargo_sessions:
            raise ValueError('Declared embargo is shorter than the session contract')
    if not {'symbol', 'instrument_type_code'} <= set(registry) or registry.symbol.duplicated().any():
        raise ValueError('One ordinary-share registry identity per symbol is required')
    members = registry.set_index('symbol').reindex(plan.symbols)
    if not members.instrument_type_code.isin((300, 303)).all():
        raise ValueError('Only ordinary shares with yVal 300/303 are admitted')
    # Projection happens before persistence; private/source-extra columns never leave the caller.
    source = prices.loc[prices.symbol.isin(plan.symbols) & prices.date.lt(plan.test_end), list(PRICE_COLUMNS)].copy()
    source = _validate_prices(source, plan.symbols, days).sort_values(['date', 'symbol']).reset_index(drop=True)
    if (source.date >= settings.RESEARCH_CONFIG['holdout_start']).any():
        raise ValueError('The general research holdout remains sealed')
    frames = []
    for symbol, group in source.groupby('symbol', sort=False):
        computed = compute_daily_features(group)
        frames.append(computed.assign(symbol=symbol)[['date', 'symbol', *PREDICTORS]])
    features = pd.concat(frames, ignore_index=True).sort_values(['date', 'symbol']).reset_index(drop=True)
    train = features.loc[features.date.ge(plan.train_start) & features.date.lt(plan.train_end), list(PREDICTORS)]
    valid = np.isfinite(train.to_numpy(dtype=float)).all(axis=1)
    if valid.sum() < 20:
        raise ValueError('At least 20 finite training rows, after historical warmup, are required')
    mean = train.loc[valid].mean().to_numpy(dtype=float)
    scale = train.loc[valid].std(ddof=0).to_numpy(dtype=float, copy=True)
    # A constant known predictor has zero centered value; this is not an unknown-value imputation.
    scale[scale == 0] = 1.
    scaler = dict(predictors=list(PREDICTORS), mean=mean.tolist(), scale=scale.tolist(),
                  fitted_role='train', fitted_rows=int(valid.sum()),
                  missing_training_rows=int((~valid).sum()), train_end_exclusive=plan.train_end)
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    common = dict(version=VERSION, plan=asdict(plan), predictors=list(PREDICTORS), scaler=scaler,
        feature_version=FEATURE_SET_VERSION, environment=training_environment(),
        code_identity=learning_code_identity(), fees=dict(settings.FEES),
        lock_min_move_pct=settings.BACKTEST_CONFIG['lock_min_move_pct'],
        holdout_start=settings.RESEARCH_CONFIG['holdout_start'],
        provenance=provenance, created_at=now_tehran_iso(),
        scope='development engineering; no independent performance or historical membership proof',
        accepted_for_signals=False, profitability_proven=False)
    metas = {}
    for role in ROLES:
        start, end = plan.bounds(role)
        role_days = [d for d in days if start <= d < end]
        if len(role_days) < plan.horizon + 5:
            raise ValueError('Every role needs a decision interval and a nontrading terminal exit tail')
        # Preserve the actual raw warmup, so a worker can independently rebuild
        # features and the train-only scaler. No future role is included.
        own = source.loc[source.date.lt(end)].copy().reset_index(drop=True)
        folder = root / role
        folder.mkdir()
        own.to_parquet(folder / 'prices.parquet', index=False)
        feature_rows = features.loc[features.date.ge(start) & features.date.lt(end)]
        feature_rows.to_parquet(folder / 'features.parquet', index=False)
        atomic_json(folder / 'sessions.json', role_days)
        hashes = {name: sha256(folder / name) for name in ('prices.parquet', 'features.parquet', 'sessions.json')}
        metas[role] = dict(common, role=role, rows=len(own), feature_rows=len(feature_rows), sessions=len(role_days), files=hashes)
    family = identity(dict(common=common, role_files={role: meta['files'] for role, meta in metas.items()}))
    for role, meta in metas.items():
        meta['family_id'] = family
        folder = root / role
        atomic_json(folder / 'manifest.json', dict(meta, bundle_id=identity(meta)))
    atomic_json(root / 'index.json', dict(version=VERSION, roles={role: read_json(root / role / 'manifest.json')['bundle_id']
        for role in ROLES}, plan=asdict(plan), accepted_for_signals=False))
    return root


@dataclass(frozen=True)
class RoleData:
    manifest: dict
    prices: pd.DataFrame
    features: pd.DataFrame
    sessions: tuple[str, ...]

    @property
    def plan(self):
        values = dict(self.manifest['plan'])
        values['symbols'] = tuple(values['symbols'])
        return LearningPlan(**values)

    @cached_property
    def feature_lookup(self):
        """One immutable numeric snapshot; steps never rescan the full feature frame."""
        index = pd.MultiIndex.from_frame(self.features[['date', 'symbol']])
        raw = self.features.loc[:, PREDICTORS].to_numpy(dtype=float, copy=True)
        raw.flags.writeable = False
        return index, raw


def load_role(folder, expected_role):
    """Hash-check one role; fitting callers must request exactly 'train'."""
    folder = Path(folder).resolve()
    meta = read_json(folder / 'manifest.json')
    if (meta.get('version') != VERSION or meta.get('role') != expected_role or expected_role not in ROLES
            or meta.get('predictors') != list(PREDICTORS)
            or meta.get('feature_version') != FEATURE_SET_VERSION
            or meta.get('environment') != training_environment()
            or meta.get('code_identity') != learning_code_identity()
            or meta.get('fees') != dict(settings.FEES)
            or meta.get('lock_min_move_pct') != settings.BACKTEST_CONFIG['lock_min_move_pct']
            or meta.get('holdout_start') != settings.RESEARCH_CONFIG['holdout_start']
            or meta.get('bundle_id') != identity({k: v for k, v in meta.items() if k != 'bundle_id'})
            or set(meta.get('files', {})) != {'prices.parquet', 'features.parquet', 'sessions.json'}):
        raise ValueError('Learning bundle role, code, schema or environment contract differs')
    for name, digest in meta['files'].items():
        path = folder / name
        if path.stat().st_size > MAX_FILE_BYTES or sha256(path) != digest:
            raise ValueError('Learning input exceeds byte budget or its frozen hash differs')
        if name.endswith('.parquet'):
            parquet = pq.ParquetFile(path)
            if (parquet.metadata.num_rows > MAX_ROWS
                    or sum(parquet.metadata.row_group(i).total_byte_size
                           for i in range(parquet.metadata.num_row_groups)) > MAX_FILE_BYTES):
                raise ValueError('Learning Parquet expansion exceeds the row/uncompressed-byte budget')
    prices, features = (pd.read_parquet(folder / name) for name in ('prices.parquet', 'features.parquet'))
    days = read_json(folder / 'sessions.json')
    if any(sha256(folder / name) != digest for name, digest in meta['files'].items()):
        raise ValueError('Learning inputs changed while the frozen role was being read')
    result = RoleData(meta, prices, features, tuple(days))
    plan = result.plan
    start, end = plan.bounds(expected_role)
    if (not days or days != sorted(set(days)) or not all(is_canonical_iso_date(d) and start <= d < end for d in days)
            or len(days) != meta['sessions'] or len(prices) != meta['rows'] or len(features) != meta['feature_rows']
            or len(features) > MAX_ROWS or set(features) != {'date', 'symbol', *PREDICTORS}
            or features.duplicated(['date', 'symbol']).any() or not features.date.isin(days).all()
            or not features.symbol.isin(plan.symbols).all()):
        raise ValueError('Learning role contains conflicting keys or future/out-of-role observations')
    allowed_days = sorted(set(days) | set(prices.loc[prices.date.lt(start), 'date']))
    _validate_prices(prices, plan.symbols, allowed_days)
    if prices.date.ge(end).any():
        raise ValueError('Raw feature warmup may cross only the past boundary, never the future boundary')
    scaler = meta.get('scaler', {})
    if (scaler.get('fitted_role') != 'train' or scaler.get('predictors') != list(PREDICTORS)
            or scaler.get('train_end_exclusive') != plan.train_end
            or len(scaler.get('mean', [])) != len(PREDICTORS) or len(scaler.get('scale', [])) != len(PREDICTORS)
            or not np.isfinite(scaler['mean']).all() or not np.isfinite(scaler['scale']).all()
            or np.any(np.asarray(scaler['scale']) <= 0)):
        raise ValueError('Train-only normalizer is invalid')
    verify_role_features(result)
    return result


def verify_role_features(data):
    """Rebuild every predictor and the normalizer from the role's raw past.

    A hash binds bytes; it does not prove that cached features match those raw
    inputs. Version 2 carries the warmup needed for this independent rebuild.
    """
    frames = []
    for symbol, group in data.prices.groupby('symbol', sort=False):
        computed = compute_daily_features(group).assign(symbol=symbol)
        frames.append(computed[['date', 'symbol', *PREDICTORS]])
    if not frames:
        raise ValueError('No raw feature history; an empty scope cannot pass admission')
    rebuilt = pd.concat(frames, ignore_index=True).sort_values(['date', 'symbol']).reset_index(drop=True)
    wanted = rebuilt.loc[rebuilt.date.isin(data.sessions)].reset_index(drop=True)
    actual = data.features.sort_values(['date', 'symbol']).reset_index(drop=True)
    if (not wanted[['date', 'symbol']].equals(actual[['date', 'symbol']])
            or not np.allclose(wanted.loc[:, PREDICTORS].to_numpy(dtype=float),
                               actual.loc[:, PREDICTORS].to_numpy(dtype=float),
                               rtol=1e-12, atol=1e-14, equal_nan=True)):
        raise ValueError('Cached learning features differ from the frozen raw inputs')
    train = rebuilt.loc[rebuilt.date.ge(data.plan.train_start) & rebuilt.date.lt(data.plan.train_end), list(PREDICTORS)]
    finite = np.isfinite(train.to_numpy(dtype=float)).all(axis=1)
    if finite.sum() < 20:
        raise ValueError('Raw warmup cannot reproduce sufficient train-only normalizer inputs')
    mean = train.loc[finite].mean().to_numpy(dtype=float)
    scale = train.loc[finite].std(ddof=0).to_numpy(dtype=float, copy=True)
    scale[scale == 0] = 1.
    scaler = data.manifest['scaler']
    if (not np.allclose(mean, scaler['mean'], rtol=1e-12, atol=1e-14)
            or not np.allclose(scale, scaler['scale'], rtol=1e-12, atol=1e-14)
            or scaler['fitted_rows'] != int(finite.sum())
            or scaler['missing_training_rows'] != int((~finite).sum())):
        raise ValueError('Normalizer does not reproduce from the training raw history alone')
    return dict(version='raw-feature-rebuild-v1', raw_rows=len(data.prices), checked_feature_rows=len(actual),
                train_scaler_rows=int(finite.sum()), source_authenticity_verified=False)


def training_scope_admission(data):
    """Fail before fitting on known gaps or unsupported reference transitions.

    This strict learning profile has no explicit dividend/rights book. It does
    not call a gap a halt, delete a company, or declare its strategy a loser.
    Runtime audits still decide whether each actual policy path is usable.
    """
    if data.manifest.get('role') != 'train' or not data.sessions or not data.plan.symbols:
        raise ValueError('A nonempty train-only scope is required before fitting')
    frame = data.prices.set_index(['date', 'symbol'])
    keys = pd.MultiIndex.from_product([data.sessions, data.plan.symbols], names=['date', 'symbol'])
    missing = keys.difference(frame.index)
    if len(missing):
        raise ValueError(f'Missing training bars ({len(missing)}); no halt or zero reward may be assumed')
    changes = []
    first_carry = data.sessions[min(2, len(data.sessions)-1)]
    for symbol, rows in data.prices.groupby('symbol', sort=False):
        rows = rows.sort_values('date')
        changed = rows.ref_price_raw.ne(rows.close_final_raw.shift(1)) & rows.date.ge(first_carry)
        changes.extend((day, symbol) for day in rows.loc[changed, 'date'])
    if changes:
        raise ValueError(f'Unsupported training reference transitions ({len(changes)}); explicit corporate accounting is required')
    return dict(version='learning-source-admission-v1', required_training_bars=len(keys),
        missing_training_bars=0, unsupported_reference_transitions=0,
        all_possible_execution_paths_verified=False, economic_source_completeness_verified=False)


def standardized(data, day):
    index, values = data.feature_lookup
    positions = index.get_indexer([(day, symbol) for symbol in data.plan.symbols])
    raw = np.full((len(positions), len(PREDICTORS)), np.nan, dtype=float)
    found = positions >= 0
    raw[found] = values[positions[found]]
    valid = found & np.isfinite(raw).all(axis=1)
    scaler = data.manifest['scaler']
    out = (raw - np.asarray(scaler['mean'])) / np.asarray(scaler['scale'])
    # Explicit separate validity mask makes zero an unavailable observation encoding,
    # never a fabricated measured feature or a permitted entry signal.
    out[~valid] = 0.
    if not np.isfinite(out).all():
        raise ValueError('Nonfinite standardized known predictor')
    return out, valid
