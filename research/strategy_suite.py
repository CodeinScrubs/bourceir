"""Registered, development-only shared-capital matrix for existing fixed rules.

Run with ``python brs.py strategy-suite --run``. Raw market inputs are captured
from a single read transaction. Features are rebuilt, never borrowed from a
possibly stale feature/label vintage. No winner or live signal is selected.
"""

import hashlib
import io
import json
import math
import os
import uuid
import zipfile
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.evidence import audit_daily_replay
from backtest.portfolio import PORTFOLIO_VERSION, PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from config import settings
from core.flow_filters import (
    RESEARCH_FILTERS,
    V4_FILTERS,
    filter_diagnostics,
    filter_diagnostics_v4,
    flow_metrics,
    flow_metrics_v4,
)
from core.price_quality import RAW_PRICES, numeric_prices, price_quality
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date, now_tehran_iso
from database.db import CLIENT_TYPE_COLUMNS, DatabaseManager
from database.flow_data import load_flow_inputs
from features.daily import FEATURE_SET_VERSION, compute_daily_features
from features.provenance import feature_environment
from research.baselines import STRATEGIES, decision_score_inputs, eligible_strategy_score, pick_top
from research.dip import DipRule
from research.labels import market_calendar
from research.run import _json_safe, _sha256, research_code_fingerprint
from research.search_ledger import DEFAULT_ATTEMPT_BUDGET, finish_trials, reserve_trials, validate_registration
from research.strategy_catalog import SEEDS, StrategyCase, catalogue, fixed_cases
from research.strategy_catalog import VERSION as CATALOG_VERSION
from research.surge_patterns import HYPOTHESES, build_pre_move_features, pattern_masks
from research.walkforward import preceding_price_observations, time_folds

VERSION = 'fixed-strategy-suite-v1'
RAW_COLUMNS = ['symbol', 'date', *RAW_PRICES, 'trade_count']
REGISTRY_COLUMNS = ['symbol_fa', 'ins_code', 'instrument_type_code', 'is_active']
SCENARIOS = ('normal', 'stress')
PREFLIGHT_VERSION = 'suite-compute-preflight-v1'
DEFAULT_MAX_REPLAYS = 10000


@dataclass(frozen=True)
class SuitePlan:
    start: str = '2017-03-25'
    end_exclusive: str = settings.RESEARCH_CONFIG['holdout_start']
    initial_capital_rial: float = 6_700_000_000.
    max_positions: int = 5
    min_daily_value_rial: float = settings.RESEARCH_CONFIG['min_daily_value_rial']
    min_train_sessions: int = 504
    test_sessions: int = 252
    participation: float = .05
    stress_participation: float = .01
    slippage_bps: float = 10.
    stress_slippage_bps: float = 25.

    def __post_init__(self):
        if (not is_canonical_iso_date(self.start) or not is_canonical_iso_date(self.end_exclusive)
                or not self.start < self.end_exclusive <= settings.RESEARCH_CONFIG['holdout_start']):
            raise ValueError('Only a canonical development interval before the sealed boundary is allowed')
        for name in ('max_positions', 'min_train_sessions', 'test_sessions'):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError('Positive integer positions and temporal windows are required')
        for name in ('initial_capital_rial', 'min_daily_value_rial', 'participation', 'stress_participation',
                     'slippage_bps', 'stress_slippage_bps'):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('Finite nonnegative plan numbers are required')
        if (self.initial_capital_rial <= 0 or not 0 < self.stress_participation <= self.participation <= .05
                or not self.slippage_bps <= self.stress_slippage_bps < 10000):
            raise ValueError('Capital, 5% capacity ceiling or stress assumptions are inconsistent')


def _bytes(value):
    return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _write(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_bytes(_bytes(value))
    os.replace(temporary, path)


def _frame_hash(frame):
    return hashlib.sha256(_bytes(frame.to_dict('records'))).hexdigest()


def _decision_diagnostics(replay):
    frame = replay.decisions
    if frame.empty:
        return dict(status_counts={}, first_unknown=None)
    unknown = frame.loc[frame.status.str.contains(
        'unknown|invalid|conflicting|unaccounted|no_session|missing_|discontinuous_', regex=True)]
    first = unknown.iloc[0].to_dict() if not unknown.empty else None
    return dict(status_counts={str(k): int(v) for k, v in frame.status.value_counts().items()}, first_unknown=first)


def validate_inputs(prices, clients, registry, plan):
    """Validate identities and boundaries without inventing/repairing raw cells."""
    for frame, columns in ((prices, RAW_COLUMNS), (clients, ['symbol', 'date', *CLIENT_TYPE_COLUMNS])):
        if (not isinstance(frame, pd.DataFrame) or frame.columns.duplicated().any()
                or set(frame) != set(columns) or frame.duplicated(['symbol', 'date']).any()
                or any(not is_canonical_iso_date(d) or d >= plan.end_exclusive for d in frame.date.unique())
                or not frame.symbol.map(lambda s: isinstance(s, str) and bool(s) and normalize_symbol(s) == s).all()):
            raise ValueError('Unique normalized raw-market keys, allowlisted columns and development dates required')
    if prices.empty or not set(prices[['symbol', 'date']].itertuples(index=False, name=None)) == set(
            clients[['symbol', 'date']].itertuples(index=False, name=None)):
        raise ValueError('Client observations must preserve exactly the price keys, including missing values')
    if (set(registry) != set(REGISTRY_COLUMNS) or registry.symbol_fa.duplicated().any()
            or registry.ins_code.duplicated().any() or not registry.ins_code.map(
                lambda code: isinstance(code, str) and code.isascii() and code.isdigit()).all()
            or not registry.instrument_type_code.isin((300, 303)).all()
            or not set(prices.symbol) <= set(registry.symbol_fa)):
        raise ValueError('An ordinary-stock identity registry is required')


def snapshot_inputs(db, plan):
    """No account table or post-boundary outcome is queried."""
    own_transaction = not db.conn.in_transaction
    if own_transaction:
        db.conn.execute('BEGIN')
    try:
        registry = pd.DataFrame([{key: row[key] for key in REGISTRY_COLUMNS}
            for row in db.get_universe_symbols(include_inactive=True) if row['instrument_type_code'] in (300, 303)])
        # The collector's daily columns are Rial by contract. Verify the stored
        # unit as well; a silent Toman/Rial conversion would invalidate fills.
        bad_units = db.conn.execute("""SELECT COUNT(*) FROM daily_prices p JOIN symbols s ON s.id=p.symbol_id
            WHERE p.source='tsetmc' AND p.date<? AND s.instrument_type_code IN (300,303)
            AND (p.price_unit IS NULL OR p.price_unit!='Rial')""", (plan.end_exclusive,)).fetchone()[0]
        if bad_units:
            raise ValueError('Stored raw-market rows have unknown or non-Rial units')
        prices, clients = load_flow_inputs(db, before=plan.end_exclusive, include_inactive=True)
    finally:
        if own_transaction:
            db.conn.rollback()
    prices, clients = (frame.sort_values(['symbol', 'date']).reset_index(drop=True) for frame in (prices, clients))
    validate_inputs(prices, clients, registry, plan)
    return prices, clients, registry


def build_scores(prices, clients, calendar, plan):
    """Return causal scores and coverage; no outcome columns are accepted."""
    client_groups = {symbol: frame for symbol, frame in clients.groupby('symbol', sort=False)}
    features = pd.concat([compute_daily_features(frame, client_groups[symbol]).assign(symbol=symbol)
                          for symbol, frame in prices.groupby('symbol', sort=False)], ignore_index=True)
    panel = prices[['date', 'symbol', 'value']].merge(features, on=['date', 'symbol'], validate='one_to_one')
    panel = panel.loc[panel.date.ge(plan.start) & panel.value.ge(plan.min_daily_value_rial)].reset_index(drop=True)
    inputs = decision_score_inputs(panel)
    scores, coverage = {}, {}
    for strategy in STRATEGIES:
        scores[strategy.name] = eligible_strategy_score(inputs, strategy)
        from research.baselines import score_coverage
        coverage[strategy.name] = score_coverage(inputs, strategy)
    scores['liquidity'] = panel.value.copy()
    # Hash each decision identity independently. Adding future rows, changing
    # source order or shortening a run cannot change earlier random controls.
    for seed in SEEDS:
        name = f'random_{seed}'
        scores[name] = pd.Series([int.from_bytes(hashlib.sha256(f'{seed}|{d}|{s}'.encode()).digest()[:8], 'big') / 2**64
                                  for d, s in panel[['date', 'symbol']].itertuples(index=False, name=None)], index=panel.index)
    for prefix, names, metrics, diagnostic in (
            ('flow', RESEARCH_FILTERS, flow_metrics(prices, clients), filter_diagnostics),
            ('flow4', V4_FILTERS, flow_metrics_v4(prices, clients, calendar=calendar), filter_diagnostics_v4)):
        for name in names:
            result = diagnostic(metrics, name)
            data = panel[['date', 'symbol']].merge(result, on=['date', 'symbol'], how='left', validate='one_to_one')
            selector = f'{prefix}:{name}'
            scores[selector] = panel.value.where(data['match'].eq(True))
            coverage[selector] = dict(rows=len(data), matches=int(data['match'].eq(True).sum()),
                unknown=int(data.status.eq('نامعلوم').sum()), known_nonmatches=int(data.status.eq('رد شده').sum()))
    surge_features = build_pre_move_features(prices, prices[['date', 'symbol']], calendar)
    masks = pd.concat([surge_features[['date', 'symbol']], pattern_masks(surge_features)], axis=1)
    data = panel[['date', 'symbol']].merge(masks, on=['date', 'symbol'], how='left', validate='one_to_one')
    for hypothesis in HYPOTHESES:
        flag = data[hypothesis.name]
        selector = f'surge:{hypothesis.name}'
        scores[selector] = panel.value.where(flag.eq(True))
        coverage[selector] = dict(rows=len(data), matches=int(flag.eq(True).sum()), unknown=int(flag.isna().sum()),
                                  known_nonmatches=int(flag.eq(False).sum()))
    picks = {}
    for name, score in scores.items():
        selected = pick_top(panel, score, plan.max_positions)[['date', 'symbol']].copy()
        selected['rank'] = selected.groupby('date').cumcount()
        picks[name] = selected.reset_index(drop=True)
    # This comparator buys an equal-budget snapshot of all initially eligible
    # symbols, not the ex-post best survivors. Its position count is explicit.
    picks['buy_hold'] = panel[['date', 'symbol', 'value']].sort_values(['date', 'value', 'symbol'], ascending=[True, False, True])
    picks['cash'] = pd.DataFrame(columns=['date', 'symbol', 'rank'])
    return features, picks, coverage


def fold_picks(case, candidates, days):
    """Use one candidate tape for both execution scenarios, with a declared tail."""
    selected = candidates[case.selector]
    if case.selector == 'buy_hold':
        selected = selected.loc[selected.date.eq(days[0]), ['date', 'symbol']].copy()
        selected['rank'] = np.arange(len(selected))
        return selected, max(1, len(days)-2), max(1, len(selected))
    # Entry at t+1, exit at t+1+h. Do not create avoidable end censoring.
    stop = max(0, len(days)-case.horizon-1)
    allowed = set(days[:stop])
    return selected.loc[selected.date.isin(allowed)].copy(), case.horizon, None


def _configuration(case, plan, scenario, horizon, positions):
    stress = scenario == 'stress'
    return PortfolioConfig(initial_capital_rial=plan.initial_capital_rial, horizon=horizon,
        max_positions=positions or plan.max_positions,
        participation=plan.stress_participation if stress else plan.participation,
        slippage_bps=plan.stress_slippage_bps if stress else plan.slippage_bps,
        max_loss_pct=case.loss_fraction, stress=stress, allow_entry_day_exit=False)


def _freeze_code(path):
    sources = {settings.BASE_DIR / name for name in ('requirements.lock.txt', 'requirements.txt', 'pyproject.toml', 'brs.py')}
    for folder in ('core', 'features', 'database', 'config', 'research', 'backtest',
                   'ops', 'data_collector', 'ai', 'dashboard'):
        sources.update(p for p in (settings.BASE_DIR / folder).rglob('*') if p.suffix in {'.py', '.sql', '.yaml'})
    hashes = {}
    with zipfile.ZipFile(path, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(sources):
            if not source.resolve().is_relative_to(settings.BASE_DIR.resolve()):
                raise ValueError('Runtime source resolves outside the project')
            contents = source.read_bytes().replace(b'\r\n', b'\n')
            name = source.relative_to(settings.BASE_DIR).as_posix()
            archive.writestr(name, contents)
            hashes[name] = hashlib.sha256(contents).hexdigest()
    return hashes


def _sources_unchanged(expected):
    root = settings.BASE_DIR.resolve()
    for name, digest in expected.items():
        path = (root / name).resolve()
        if (Path(name).is_absolute() or '..' in Path(name).parts or not path.is_relative_to(root)
                or not path.is_file() or hashlib.sha256(path.read_bytes().replace(b'\r\n', b'\n')).hexdigest() != digest):
            return False
    return True


def summarize_records(records, cases, fold_count):
    """A missing fold never becomes zero or an optimistic valid-only average."""
    summaries = []
    for case in cases:
        row = dict(case=case.name, family=case.family, sensitivity_only=case.sensitivity_only, scenarios={})
        for scenario in SCENARIOS:
            own = [r for r in records if r['case'] == case.name and r['scenario'] == scenario]
            valid = [r['audit']['observed_path_model_return'] for r in own
                     if r.get('audit', {}).get('observed_path_model_return') is not None]
            cash = [r['audit']['modeled_cash_return'] for r in own if r.get('audit', {}).get('modeled_cash_return') is not None]
            row['scenarios'][scenario] = dict(expected_folds=fold_count, recorded_folds=len(own), known_folds=len(valid),
                unknown_folds=fold_count-len(valid), fully_cash_folds=len(cash),
                mean_fold_model_return=float(np.mean(valid)) if len(valid) == fold_count else None,
                individual_known_returns=valid, fees_rial=sum(r.get('audit', {}).get('fees_rial', 0) for r in own),
                blocker_counts=dict(Counter(reason for r in own for reason in r.get('audit', {}).get('blockers', []))),
                invariant_violation_count=sum(bool(r.get('audit', {}).get('invariant_violations')) for r in own))
        summaries.append(row)
    return summaries


def _suite_cases(case_names):
    registered = fixed_cases()
    if case_names is not None and (not case_names or len(set(case_names)) != len(case_names)
                                  or not set(case_names) <= {c.name for c in registered}):
        raise ValueError('An explicit nonempty subset of registered cases is required')
    return tuple(c for c in registered if case_names is None or c.name in case_names)


def _prepare_suite(db, plan, cases, max_replays):
    """Inspect one raw snapshot before features, archive creation or financial replay."""
    if type(max_replays) is not int or max_replays < 1:
        raise ValueError('An explicit positive integer replay budget is required')
    prices, clients, registry = snapshot_inputs(db, plan)
    return _inspect_suite_inputs(prices, clients, registry, plan, cases, max_replays)


def _inspect_suite_inputs(prices, clients, registry, plan, cases, max_replays):
    validate_inputs(prices, clients, registry, plan)
    _, flags = price_quality(prices)
    counts = numeric_prices(prices, ('trade_count',)).trade_count
    bad_counts = counts.isna() | counts.le(0) | counts.mod(1).ne(0)
    invalid = flags.any(axis=1) | bad_counts
    blockers = []
    if invalid.any():
        blockers.append('invalid_raw_observations')
    calendar = market_calendar(prices)
    if not set(prices.date.unique()) <= set(calendar):
        blockers.append('observations_outside_inferred_calendar')
    embargo = max(case.horizon for case in cases) + 1
    try:
        folds = time_folds(calendar[calendar >= plan.start], min_train_sessions=plan.min_train_sessions,
            test_sessions=plan.test_sessions, embargo_sessions=embargo, end_exclusive=plan.end_exclusive)
    except ValueError:
        folds = []
        blockers.append('insufficient_or_invalid_temporal_folds')
    replays = len(cases) * len(folds) * len(SCENARIOS)
    if replays > max_replays:
        blockers.append('replay_budget_exceeded')
    report = dict(version=PREFLIGHT_VERSION, status='blocked' if blockers else 'ready_for_exploratory_replay',
        blockers=blockers, case_count=len(cases), fold_count=len(folds), scenario_count=len(SCENARIOS),
        expected_replays=replays, max_replays=max_replays, raw_rows=len(prices),
        invalid_raw_rows=int(invalid.sum()), invalid_trade_count_rows=int(bad_counts.sum()),
        raw_quality_counts={key: int(value) for key, value in flags.sum().items()},
        invalid_examples=prices.loc[invalid, ['symbol', 'date']].head(10).to_dict('records'),
        folds=[asdict(fold) for fold in folds], plan=asdict(plan),
        case_spec_sha256=hashlib.sha256(_bytes([asdict(case) for case in cases])).hexdigest(),
        expensive_computation_started=False, strategy_verdict=None, holdout_loaded=False,
        earlier_trial_count_unknown=True, statistical_power_established=False,
        economic_readiness=False, accepted_for_signals=False,
        limitations=['numeric coherence is not source authenticity or point-in-time availability',
            'calendar is inferred; historical membership, fees, corporate entitlements and fills need separate evidence',
            'blocked input or budget is not evidence against a strategy',
            'replay count is a compute count, not the total number of hypotheses searched'])
    return prices, clients, registry, calendar, folds, report


def preflight_suite(db, *, plan=None, case_names=None, max_replays=DEFAULT_MAX_REPLAYS):
    """Read-only and outcome-free: no features, model fitting, returns or run directory."""
    return _prepare_suite(db, plan or SuitePlan(), _suite_cases(case_names), max_replays)[-1]


def _search_contract(protocol, calendar):
    return dict(version='suite-search-contract-v1', workflow_version=protocol['version'],
        plan=protocol['plan'], raw_sha256=protocol['raw_sha256'], calendar_sha256=_frame_hash(pd.DataFrame({'date': calendar})),
        code_sha256=protocol['code_sha256'], environment=protocol['environment'], folds=protocol['folds'],
        scenarios=protocol['scenarios'], fees=protocol['fees'], portfolio_version=protocol['portfolio_version'],
        feature_version=protocol['feature_version'], starting_portfolio='independent_cash_per_fold')


def run_suite(db, *, root=None, plan=None, case_names=None, progress=None, max_replays=DEFAULT_MAX_REPLAYS,
              allow_repeat=False, search_attempt_budget=DEFAULT_ATTEMPT_BUDGET):
    if type(allow_repeat) is not bool or type(search_attempt_budget) is not int or search_attempt_budget < 1:
        raise ValueError('Boolean repeat authorization and a positive integer search attempt budget are required')
    tracking = {}
    try:
        result = _run_suite(db, root=root, plan=plan, case_names=case_names, progress=progress, max_replays=max_replays,
            allow_repeat=allow_repeat, search_attempt_budget=search_attempt_budget, tracking=tracking)
    except BaseException as exc:
        if tracking.get('registration'):
            try:
                finish_trials(tracking['registration'], status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                    evidence=dict(error_type=type(exc).__name__, financial_verdict=None))
            except Exception as ledger_error:
                exc.add_note(f'Search termination could not be recorded: {type(ledger_error).__name__}; inspect unfinished attempts')
        raise
    finish_trials(tracking['registration'], status='complete', evidence=dict(
        manifest_sha256=_sha256(Path(result['folder']) / 'manifest.json'),
        financial_verdict=None, accepted_for_signals=False))
    return result


def _run_suite(db, *, root, plan, case_names, progress, max_replays, allow_repeat, search_attempt_budget, tracking):
    plan = plan or SuitePlan()
    cases = _suite_cases(case_names)
    parent = Path(root or settings.DATA_DIR / 'private' / 'strategy-suites').resolve()
    if not parent.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Suite output must be contained in data/private')
    notify = progress or (lambda event: None)
    code_sha = research_code_fingerprint()
    notify(dict(stage='preflight'))
    prices, clients, registry, calendar, folds, preflight = _prepare_suite(db, plan, cases, max_replays)
    if preflight['blockers']:
        raise ValueError('Suite preflight blocked before feature/replay compute: ' + ', '.join(preflight['blockers']))
    run_id = f'SUITE-{now_tehran_iso()[:10]}-{uuid.uuid4().hex}'
    folder = parent / run_id
    folder.mkdir(parents=True, exist_ok=False)
    _write(folder / 'state.json', dict(status='preparing', run_id=run_id, started_at=now_tehran_iso()))
    _write(folder / 'compute-preflight.json', preflight)
    raw_hashes = {}
    for name, frame in (('prices', prices), ('clients', clients), ('registry', registry)):
        path = folder / f'{name}.parquet'
        frame.to_parquet(path, index=False, compression='zstd')
        raw_hashes[path.name] = _sha256(path)
    _write(folder / 'calendar.json', calendar.tolist())
    _, flags = price_quality(prices)
    protocol = dict(version=VERSION, run_id=run_id, created_at=now_tehran_iso(), plan=asdict(plan),
        catalogue_version=CATALOG_VERSION, cases=[asdict(case) for case in cases],
        complete_fixed_catalogue=len(cases) == len(fixed_cases()), full_fixed_catalogue_size=len(fixed_cases()),
        compute_preflight_sha256=_sha256(folder / 'compute-preflight.json'),
        folds=[asdict(fold) for fold in folds], scenarios=list(SCENARIOS), fees=dict(settings.FEES),
        fees_are_current_config_not_verified_historical_tariffs=True,
        raw_sha256=raw_hashes, code_sha256=code_sha, source_files=_freeze_code(folder / 'code.zip'),
        environment=feature_environment(), portfolio_version=PORTFOLIO_VERSION, feature_version=FEATURE_SET_VERSION,
        raw_rows=len(prices), symbols=prices.symbol.nunique(), raw_first=prices.date.min(), raw_last=prices.date.max(),
        raw_quality_counts={key: int(value) for key, value in flags.sum().items()},
        current_registry_not_historical_membership=True, calendar_is_inferred_not_exchange_verified=True,
        feature_inputs='one raw price/client snapshot; causal features rebuilt from warmup; no labels or adjusted prices',
        holding_horizon='h sessions AFTER t+1 entry; inclusive duration h+1; t+1+h intended exit',
        folds_are_independent_cash_accounts=True, training='fixed rules only; no winner or hyperparameter fitting',
        incomplete_and_unknown_results_retained=True, same_day_proceeds_not_reused=True,
        frozen_aban07_release_modified=False, holdout_loaded=False, prospective_independence=False,
        promotion_enabled=False, trial_count_within_run=len(cases)*len(SCENARIOS),
        earlier_trial_count_unknown=True, multiple_testing_correction_claimed=False,
        separate_workflows=catalogue()['separate_registered_workflows'],
        limitations=['daily candles do not establish queue position or fill probability',
            'current membership and retrospective corrected data do not prove historical availability',
            'corporate actions use synthetic reference units; real dividends/rights/credits unverified',
            'surge cases retain different allocation/risk limits; returns are not matched-risk comparisons',
            'stress and normal share decisions; fills and later cash may differ',
            'no simulated same-day resale; intraday strategies require ordered trades and book evidence'])
    contract = _search_contract(protocol, calendar)
    policies = [dict(name=case.name, policy={k: v for k, v in asdict(case).items() if k != 'name'}) for case in cases]
    registration = reserve_trials(contract, policies, run_id=run_id, allow_repeat=allow_repeat,
        attempt_budget=search_attempt_budget)
    tracking['registration'] = registration
    protocol['local_search_registration'] = registration
    _write(folder / 'protocol.json', protocol)
    with DatabaseManager(folder / 'experiments.db') as registry_db:
        registry_db.init_db()
        if registry_db.log_experiment(experiment_id=run_id+'-PROTOCOL', strategy_name='fixed-strategy-suite-protocol',
                symbols_tested='ordinary-equity-market', parameters_json=_bytes(protocol).decode(), metrics_json='{}',
                start_date=plan.start, end_date=plan.end_exclusive, dataset_version=hashlib.sha256(_bytes(raw_hashes)).hexdigest(),
                features_version=FEATURE_SET_VERSION, model_version=VERSION) is None:
            raise RuntimeError('Could not register the protocol before evaluation')
        notify(dict(stage='features', rows=len(prices), cases=len(cases), folds=len(folds)))
        features, candidates, coverage = build_scores(prices, clients, calendar, plan)
        features.to_parquet(folder / 'features.parquet', index=False, compression='zstd')
        _write(folder / 'coverage.json', coverage)
        records = []
        with zipfile.ZipFile(folder / 'replays.zip', 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for i, fold in enumerate(folds):
                days = calendar[(calendar >= fold.test_start) & (calendar < fold.test_end_exclusive)].tolist()
                warmup = preceding_price_observations(prices, fold.test_start)
                raw = pd.concat([warmup, prices.loc[prices.date.ge(fold.test_start) & prices.date.lt(fold.test_end_exclusive)]], ignore_index=True)
                prepared = prepare_portfolio_prices(raw)
                for j, case in enumerate(cases):
                    selected, horizon, positions = fold_picks(case, candidates, days)
                    pick_sha = _frame_hash(selected)
                    for scenario in SCENARIOS:
                        configuration = _configuration(case, plan, scenario, horizon, positions)
                        replay = replay_portfolio(prepared, selected, calendar, configuration,
                            start=fold.test_start, end=fold.test_end_exclusive, dip_rule=case.dip_rule,
                            entry_allocation_fraction=case.allocation, loss_floor_exit=case.family == 'surge')
                        audit = audit_daily_replay(replay, prepared, selected, days)
                        contents = _bytes(dict(case=case.name, fold=i, scenario=scenario, metrics=replay.metrics,
                            assumptions=replay.assumptions, audit=audit, picks=selected.to_dict('records'),
                            equity=replay.equity.to_dict('records'), executions=replay.executions.to_dict('records'),
                            decisions=replay.decisions.to_dict('records')))
                        member = f'fold-{i:04d}/case-{j:04d}/{scenario}.json'
                        archive.writestr(member, contents)
                        records.append(dict(case=case.name, fold=i, scenario=scenario, member=member,
                            sha256=hashlib.sha256(contents).hexdigest(), picks_sha256=pick_sha, audit=audit,
                            decision_diagnostics=_json_safe(_decision_diagnostics(replay))))
                    if (j+1) % 25 == 0 or j+1 == len(cases):
                        notify(dict(stage='replay', fold=i+1, folds=len(folds), cases_done=j+1, cases=len(cases)))
                    _write(folder / 'state.json', dict(status='running', run_id=run_id, completed_replays=len(records)))
        if research_code_fingerprint() != code_sha or not _sources_unchanged(protocol['source_files']):
            raise RuntimeError('Research source changed during execution; this run is not complete')
        summary = summarize_records(records, cases, len(folds))
        payload = dict(version=VERSION, run_id=run_id, finished_at=now_tehran_iso(), records=records, summary=summary,
            expected_replays=len(cases)*len(folds)*len(SCENARIOS), recorded_replays=len(records),
            invariant_failures=sum(bool(r['audit']['invariant_violations']) for r in records),
            accepted_for_signals=False, economic_profit_verified=False, probability_of_future_profit=None)
        _write(folder / 'results.json', payload)
        completed = registry_db.log_experiment(experiment_id=run_id, strategy_name='fixed-strategy-suite',
            symbols_tested='ordinary-equity-market', parameters_json=_bytes(protocol).decode(),
            metrics_json=_bytes(dict(replays=len(records), invariant_failures=payload['invariant_failures'],
                accepted_for_signals=False)).decode(), start_date=plan.start, end_date=plan.end_exclusive,
            dataset_version=hashlib.sha256(_bytes(raw_hashes)).hexdigest(), features_version=FEATURE_SET_VERSION, model_version=VERSION)
        if completed is None:
            raise RuntimeError('Could not record the completed suite')
    _write(folder / 'state.json', dict(status='complete', run_id=run_id, completed_replays=len(records)))
    (folder / 'report-fa.md').write_text(format_report(protocol, payload), encoding='utf-8')
    _write(folder / 'manifest.json', {p.name: _sha256(p) for p in sorted(folder.iterdir()) if p.is_file()})
    return dict(folder=str(folder), **payload)


def format_report(protocol, result):
    lines = ['# ماتریس مشترک بکتست قواعد موجود', '', f"شناسه: {protocol['run_id']}", '',
        f"{len(protocol['cases'])} پیکربندی × {len(protocol['folds'])} برش زمانی × دو سناریوی اجرا؛ "
        f"{result['recorded_replays']} اجرای ثبت‌شده. خطای مستقل حساب/اجرا: {result['invariant_failures']}.", '',
        'این گزارش انتخاب بهترین روش، اثبات سودآوری یا مجوز سیگنال نیست. تمام نتایج نامعلوم در مخرج حفظ شده‌اند.',
        'افق h یعنی h جلسه پس از روز ورود؛ h=1 و h=2 به‌ترتیب دو و سه تاریخ معاملاتی با احتساب ورود هستند.',
        'هر برش با نقد مستقل آغاز می‌شود؛ CAGR از اتصال حساب‌های مستقل ساخته نشده است.', '',
        '| روش | برش معلوم عادی | برش معلوم فشار | میانگین عادی فقط اگر همه معلوم | میانگین فشار فقط اگر همه معلوم |',
        '|---|---:|---:|---:|---:|']
    for row in result['summary']:
        a, b = (row['scenarios'][s] for s in SCENARIOS)
        def pct(value):
            return 'نامعلوم' if value is None else f'{value:+.2%}'
        lines.append(f"| {row['case']}{' (حساسیت خوش‌بینانه)' if row['sensitivity_only'] else ''} | "
            f"{a['known_folds']}/{a['expected_folds']} | {b['known_folds']}/{b['expected_folds']} | "
            f"{pct(a['mean_fold_model_return'])} | {pct(b['mean_fold_model_return'])} |")
    lines.extend(['', 'محدودیت‌ها:', '', *[f'- {s}' for s in protocol['limitations']], '',
        'مدل‌های یادگیرنده و انتخاب‌گرهای زمانی، پروتکل و ورودی مستقل دارند و در این ماتریس به قاعده ثابت تبدیل نشده‌اند.',
        'tests مسیرهای اختصاصی آن‌ها را می‌آزماید؛ عبور آزمون کد با اجرای مالی تازه برابر نیست.',
        'نسخه ثابت ABAN07 و داده دوره مهروموم در این اجرا تغییر نکرده‌اند.', ''])
    return '\n'.join(lines)


def _restore_cases(protocol):
    result = []
    for case in protocol['cases']:
        values = dict(case)
        if values['dip_rule'] is not None:
            values['dip_rule'] = DipRule(**values['dip_rule'])
        result.append(StrategyCase(**values))
    return result


def review_suite(folder, *, reexecute=False, progress=None):
    """Read and verify the complete finite denominator and every replay member."""
    folder = Path(folder).resolve()
    if not folder.is_relative_to((settings.DATA_DIR / 'private').resolve()):
        raise ValueError('Review requires a private suite folder')
    manifest = json.loads((folder / 'manifest.json').read_bytes())
    if not isinstance(manifest, dict) or not manifest or any(Path(n).name != n for n in manifest):
        raise ValueError('Contained evidence manifest required')
    if set(manifest) != {p.name for p in folder.iterdir() if p.is_file() and p.name != 'manifest.json'}:
        raise ValueError('Evidence file set differs from the manifest')
    if any(_sha256(folder / name) != digest for name, digest in manifest.items()):
        raise ValueError('Suite evidence hash mismatch')
    protocol = json.loads((folder / 'protocol.json').read_bytes())
    result = json.loads((folder / 'results.json').read_bytes())
    if protocol['version'] != VERSION or result['version'] != VERSION or protocol['run_id'] != result['run_id']:
        raise ValueError('Unsupported or inconsistent suite identity')
    plan = SuitePlan(**protocol['plan'])
    state = json.loads((folder / 'state.json').read_bytes())
    if state['status'] != 'complete' or state['run_id'] != protocol['run_id']:
        raise ValueError('The run was not marked complete')
    if protocol['holdout_loaded'] is not False or protocol['promotion_enabled'] is not False:
        raise ValueError('The development/promotion boundary differs from the suite contract')
    if any(manifest.get(name) != digest for name, digest in protocol['raw_sha256'].items()):
        raise ValueError('Frozen raw input hashes differ from the preregistered protocol')
    with zipfile.ZipFile(folder / 'code.zip') as archive:
        if len(archive.namelist()) != len(protocol['source_files']) or set(archive.namelist()) != set(protocol['source_files']):
            raise ValueError('Frozen source set differs from the protocol')
        if any(hashlib.sha256(archive.read(name)).hexdigest() != digest for name, digest in protocol['source_files'].items()):
            raise ValueError('Frozen source hash mismatch')
    names = [c['name'] for c in protocol['cases']]
    expected = {(name, i, scenario) for name in names for i in range(len(protocol['folds'])) for scenario in SCENARIOS}
    records = result['records']
    if (len(names) != len(set(names)) or len(records) != len(expected)
            or {(r['case'], r['fold'], r['scenario']) for r in records} != expected
            or result['expected_replays'] != len(expected) or result['recorded_replays'] != len(expected)):
        raise ValueError('The full registered case/fold/scenario denominator is not preserved')
    cases = _restore_cases(protocol)
    if 'local_search_registration' in protocol:
        registration = protocol['local_search_registration']
        policies = [dict(name=case.name, policy={k: v for k, v in asdict(case).items() if k != 'name'}) for case in cases]
        if registration.get('run_id') != protocol['run_id']:
            raise ValueError('Search registration run differs from the study')
        validate_registration(_search_contract(protocol, json.loads((folder / 'calendar.json').read_bytes())),
                              policies, registration)
    if 'compute_preflight_sha256' in protocol:
        if manifest.get('compute-preflight.json') != protocol['compute_preflight_sha256']:
            raise ValueError('Compute preflight receipt differs from the protocol')
        admission = json.loads((folder / 'compute-preflight.json').read_bytes())
        if (admission.get('version') != PREFLIGHT_VERSION or admission.get('blockers') != []
                or admission.get('status') != 'ready_for_exploratory_replay'
                or admission.get('expected_replays') != len(expected)
                or type(admission.get('max_replays')) is not int or admission['max_replays'] < len(expected)
                or admission.get('plan') != protocol['plan'] or admission.get('folds') != protocol['folds']
                or admission.get('case_spec_sha256') != hashlib.sha256(_bytes([asdict(c) for c in cases])).hexdigest()):
            raise ValueError('Compute preflight does not match the registered replay matrix')
    if (summarize_records(records, cases, len(protocol['folds'])) != result['summary']
            or result['invariant_failures'] != sum(bool(r['audit']['invariant_violations']) for r in records)):
        raise ValueError('Published summary does not reproduce from the complete replay index')
    expected_members = {f'fold-{i:04d}/case-{j:04d}/{s}.json'
                        for i in range(len(protocol['folds'])) for j in range(len(names)) for s in SCENARIOS}
    with zipfile.ZipFile(folder / 'replays.zip') as archive:
        if len(archive.namelist()) != len(expected_members) or set(archive.namelist()) != expected_members:
            raise ValueError('Replay archive has missing, duplicate or extra members')
        for record in records:
            member = record['member']
            expected_name = f"fold-{record['fold']:04d}/case-{names.index(record['case']):04d}/{record['scenario']}.json"
            if member != expected_name or archive.getinfo(member).file_size > 32*1024*1024:
                raise ValueError('Replay member identity or size mismatch')
            contents = archive.read(member)
            if hashlib.sha256(contents).hexdigest() != record['sha256']:
                raise ValueError('Replay member hash mismatch')
            replay = json.loads(contents)
            if (any(replay[k] != record[k] for k in ('case', 'fold', 'scenario', 'audit'))
                    or hashlib.sha256(_bytes(replay['picks'])).hexdigest() != record['picks_sha256']):
                raise ValueError('Replay receipt differs from the index')
            counts = dict(Counter(item['status'] for item in replay['decisions']))
            if counts != record['decision_diagnostics']['status_counts']:
                raise ValueError('Decision status counts differ from the replay receipt')
    for name in names:
        for i in range(len(protocol['folds'])):
            hashes = {r['picks_sha256'] for r in records if r['case'] == name and r['fold'] == i}
            if len(hashes) != 1:
                raise ValueError('Normal/stress decisions are not identical')
    if reexecute:
        if (research_code_fingerprint() != protocol['code_sha256'] or feature_environment() != protocol['environment']
                or dict(settings.FEES) != protocol['fees'] or not _sources_unchanged(protocol['source_files'])):
            raise ValueError('Exact source, dependency environment and fee configuration are required for reexecution')
        frozen = {}
        for name in ('prices', 'clients', 'registry'):
            contents = (folder / f'{name}.parquet').read_bytes()
            if hashlib.sha256(contents).hexdigest() != protocol['raw_sha256'][f'{name}.parquet']:
                raise ValueError('Raw input changed during review')
            if name != 'registry':
                dates = pd.read_parquet(io.BytesIO(contents), columns=['date']).date
                if any(not is_canonical_iso_date(d) or d >= plan.end_exclusive for d in dates.unique()):
                    raise ValueError('Frozen input crosses its declared development boundary')
            frozen[name] = pd.read_parquet(io.BytesIO(contents))
        validate_inputs(frozen['prices'], frozen['clients'], frozen['registry'], plan)
        if 'compute_preflight_sha256' in protocol:
            rebuilt = _inspect_suite_inputs(frozen['prices'], frozen['clients'], frozen['registry'], plan, cases,
                admission['max_replays'])[-1]
            if rebuilt != admission:
                raise ValueError('Compute preflight does not reproduce from the frozen inputs')
        calendar = np.array(json.loads((folder / 'calendar.json').read_bytes()))
        if not np.array_equal(calendar, market_calendar(frozen['prices'])):
            raise ValueError('Frozen calendar no longer reproduces from raw prices')
        features, candidates, coverage = build_scores(frozen['prices'], frozen['clients'], calendar, plan)
        pd.testing.assert_frame_equal(features, pd.read_parquet(folder / 'features.parquet'), check_exact=True)
        if _json_safe(coverage) != json.loads((folder / 'coverage.json').read_bytes()):
            raise ValueError('Feature/selection coverage did not reproduce')
        with zipfile.ZipFile(folder / 'replays.zip') as archive:
            for i, fold in enumerate(protocol['folds']):
                start, end = fold['test_start'], fold['test_end_exclusive']
                days = calendar[(calendar >= start) & (calendar < end)].tolist()
                raw = frozen['prices']
                source = pd.concat([preceding_price_observations(raw, start),
                    raw.loc[raw.date.ge(start) & raw.date.lt(end)]], ignore_index=True)
                prepared = prepare_portfolio_prices(source)
                for j, case in enumerate(cases):
                    selected, horizon, positions = fold_picks(case, candidates, days)
                    for scenario in SCENARIOS:
                        replay = replay_portfolio(prepared, selected, calendar,
                            _configuration(case, plan, scenario, horizon, positions), start=start, end=end,
                            dip_rule=case.dip_rule, entry_allocation_fraction=case.allocation,
                            loss_floor_exit=case.family == 'surge')
                        actual = dict(case=case.name, fold=i, scenario=scenario, metrics=replay.metrics,
                            assumptions=replay.assumptions, audit=audit_daily_replay(replay, prepared, selected, days),
                            picks=selected.to_dict('records'), equity=replay.equity.to_dict('records'),
                            executions=replay.executions.to_dict('records'), decisions=replay.decisions.to_dict('records'))
                        if _bytes(actual) != archive.read(f'fold-{i:04d}/case-{j:04d}/{scenario}.json'):
                            raise ValueError('Financial replay did not reproduce exactly')
                    if progress and ((j+1) % 25 == 0 or j+1 == len(cases)):
                        progress(dict(stage='reexecute', fold=i+1, cases_done=j+1, cases=len(cases)))
        if research_code_fingerprint() != protocol['code_sha256'] or not _sources_unchanged(protocol['source_files']):
            raise ValueError('Source changed during financial reexecution; no verification receipt was issued')
    return dict(version=VERSION, status='verified_archive', replays=len(records), files_verified=len(manifest),
        invariant_failures=sum(bool(r['audit']['invariant_violations']) for r in records),
        financial_reexecution_performed=reexecute, source_authenticity_verified=False,
        historical_availability_verified=False, economic_profit_verified=False, accepted_for_signals=False)
