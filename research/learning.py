"""Registered, bounded CPU learners; no broker access or automatic signals.

Q updates occur only after a fully audited episode. This property is required
of any future deep-RL adapter; a generic online learner is not a safe substitute.
"""

import argparse
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.evidence import audit_daily_replay
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from core.timeutils import now_tehran_iso
from research.search_ledger import finish_trials, identity, reserve_trials
from research.training_data import (
    PREDICTORS,
    LearningPlan,
    atomic_json,
    learning_code_identity,
    load_role,
    prepare_bundles,
    read_json,
    sha256,
    standardized,
    training_scope_admission,
)

VERSION = 'audited-cpu-learning-v1'
Q_PARAMETERS = dict(alpha=.2, gamma=1., epsilon=.2, bins=[-.5, .5])


class ComputeBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class LearningBudget:
    episodes: int = 4
    max_steps: int = 20000
    max_states: int = 20000
    max_seconds: int = 300
    threads: int = 1
    max_memory_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        limits = dict(episodes=1000, max_steps=1_000_000, max_states=100_000, max_seconds=3600,
                      threads=8, max_memory_bytes=2 * 1024 * 1024 * 1024)
        if any(type(getattr(self, k)) is not int or not 1 <= getattr(self, k) <= v for k, v in limits.items()):
            raise ValueError('Explicit positive bounded training resources are required')


def _admit(data, budget, method):
    if data.manifest.get('role') != 'train' or method not in ('q', 'ridge'):
        raise ValueError('Fitting can read only a train role with a declared learner')
    estimated = ((len(data.sessions) - 1) * budget.episodes if method == 'q' else
        len(data.sessions) * len(data.plan.symbols) * (data.plan.horizon + 3))
    memory = int(data.prices.memory_usage(deep=True).sum() + data.features.memory_usage(deep=True).sum())
    # Include conservative overhead for replay dictionaries, observations and the Q table.
    estimate_memory = memory * 12 + budget.max_states * (1024 + 16 * len(data.plan.symbols))
    if estimated > budget.max_steps or estimate_memory > budget.max_memory_bytes:
        raise ValueError('Training exceeds declared step/memory admission budget before fitting')
    return dict(estimated_max_steps=estimated, estimated_memory_bytes=estimate_memory,
                hard_process_memory_limit=False, source_admission=training_scope_admission(data))


def _spec(data, method, seed, budget):
    if type(seed) is not int or not 0 <= seed <= 2**32 - 1:
        raise ValueError('A fixed unsigned 32-bit seed is required')
    return dict(version=VERSION, method=method, seed=seed, budget=asdict(budget),
        bundle_id=data.manifest['bundle_id'], family_id=data.manifest['family_id'],
        environment=data.manifest['environment'], code_identity=data.manifest['code_identity'],
        plan=data.manifest['plan'], scaler=data.manifest['scaler'], predictors=list(PREDICTORS),
        fees=data.manifest['fees'], lock_min_move_pct=data.manifest['lock_min_move_pct'],
        reward='audited delta net-sell-fee NAV / initial capital; terminal cash required',
        parameters=Q_PARAMETERS if method == 'q' else dict(alpha=1., fit_intercept=True, daily_equal_weight=True),
        accepted_for_signals=False, profitability_proven=False)


def save_model(path, payload):
    atomic_json(path, dict(payload=payload, sha256=identity(payload)))


def load_model(path):
    envelope = read_json(path)
    payload = envelope.get('payload', {})
    if set(envelope) != {'payload', 'sha256'} or envelope['sha256'] != identity(payload):
        raise ValueError('Model/checkpoint checksum differs')
    spec = payload.get('spec', {})
    if spec.get('version') != VERSION or spec.get('method') not in ('q', 'ridge'):
        raise ValueError('Unsupported learning model')
    return payload


def q_state(observation, info, n_symbols):
    """Fixed small bins, not bins chosen from validation/test performance."""
    width = n_symbols * len(PREDICTORS)
    bins = np.digitize(observation[:width], Q_PARAMETERS['bins']).tolist()
    known = observation[width:width+n_symbols].astype(int).tolist()
    held = (observation[width+n_symbols:width+2*n_symbols] > 0).astype(int).tolist()
    return ','.join(str(int(v)) for v in [*bins, *known, *held, *info['action_mask'].tolist()])


def _q_values(table, state, n_actions):
    values = table.get(state, [0.] * n_actions)
    return np.asarray(values, dtype=float)


def fit_q(data, budget, *, seed, checkpoint, resume=None, stop_after=None):
    """Deterministic episode-boundary checkpoints, including exploration RNG.

    A wall-time/step/data failure discards the current episode before any weight
    update. Already committed episodes remain resumable and are not new evidence.
    """
    from research.rl_env import EntryTimingEnv
    _admit(data, budget, 'q')
    spec = _spec(data, 'q', seed, budget)
    state = dict(spec=spec, completed_episodes=0, steps=0, table={}, episodes=[], rng=None, status='partial')
    rng = np.random.default_rng(seed)
    if resume is not None:
        state = load_model(resume)
        if (state.get('spec') != spec or type(state.get('completed_episodes')) is not int
                or not 0 <= state['completed_episodes'] <= budget.episodes
                or len(state.get('episodes', [])) != state['completed_episodes']
                or type(state.get('steps')) is not int
                or state['steps'] != state['completed_episodes'] * (len(data.sessions) - 1)
                or not isinstance(state.get('table'), dict) or len(state['table']) > budget.max_states):
            raise ValueError('Resume checkpoint differs from the complete training contract')
        for values in state['table'].values():
            if len(values) != len(data.plan.symbols) + 1 or not np.isfinite(values).all():
                raise ValueError('Malformed Q checkpoint weights')
        rng.bit_generator.state = state['rng']
    if stop_after is not None and (type(stop_after) is not int or not 1 <= stop_after <= budget.episodes):
        raise ValueError('Planned stop must be a declared episode boundary')
    started = time.monotonic()
    env = EntryTimingEnv(data)
    try:
        for episode in range(state['completed_episodes'], budget.episodes):
            if time.monotonic() - started >= budget.max_seconds:
                raise ComputeBudgetExceeded('Wall time exhausted; last complete episode remains checkpointed')
            observation, info = env.reset(seed=seed + episode)
            transitions = []
            while True:
                if time.monotonic() - started >= budget.max_seconds:
                    raise ComputeBudgetExceeded('Wall time exhausted mid-episode; no partial Q updates accepted')
                current = q_state(observation, info, len(data.plan.symbols))
                available = np.flatnonzero(info['action_mask'])
                if rng.random() < Q_PARAMETERS['epsilon']:
                    action = int(rng.choice(available))
                else:
                    values = _q_values(state['table'], current, env.action_space.n)
                    best = available[values[available] == values[available].max()]
                    action = int(rng.choice(best))
                observation, reward, done, truncated, info = env.step(action)
                if truncated:
                    raise ValueError('Truncated episodes are not admissible Q targets')
                following = None if done else q_state(observation, info, len(data.plan.symbols))
                transitions.append((current, action, reward, following, info['action_mask'].tolist()))
                if done:
                    break
            # No update above this line. Terminal independent audit accepted every reward.
            candidate = {key: list(values) for key, values in state['table'].items()}
            for current, action, reward, following, mask in transitions:
                if current not in candidate:
                    if len(candidate) >= budget.max_states:
                        raise ComputeBudgetExceeded('Q state budget exhausted; episode not committed')
                    candidate[current] = [0.] * env.action_space.n
                future = 0. if following is None else max(
                    _q_values(candidate, following, env.action_space.n)[np.asarray(mask, dtype=bool)])
                old = candidate[current][action]
                candidate[current][action] = old + Q_PARAMETERS['alpha'] * (reward + Q_PARAMETERS['gamma'] * future - old)
            state['table'] = candidate
            state['completed_episodes'] = episode + 1
            state['steps'] += len(transitions)
            state['rng'] = rng.bit_generator.state
            state['episodes'].append(dict(episode=episode, modeled_cash_return=env.audit['modeled_cash_return'],
                maximum_observed_drawdown=env.audit['maximum_observed_drawdown'],
                fees_rial=env.audit['fees_rial'], buy_executions=env.audit['buy_executions'],
                sell_executions=env.audit['sell_executions']))
            state['status'] = 'trained' if episode + 1 == budget.episodes else 'partial'
            save_model(checkpoint, state)
            if stop_after is not None and episode + 1 >= stop_after:
                break
    finally:
        env.close()
    return state


def fit_ridge(data, budget, *, seed, checkpoint):
    """Fixed alpha, train-only preprocessing and mature audited one-attempt targets.

    The target is net return per attempt budget, including capacity and non-entry,
    not a claim that independent per-trade labels equal a portfolio return.
    """
    from sklearn.linear_model import Ridge
    from threadpoolctl import threadpool_limits
    _admit(data, budget, 'ridge')
    spec = _spec(data, 'ridge', seed, budget)
    config = PortfolioConfig(initial_capital_rial=data.plan.initial_capital_rial,
                             horizon=data.plan.horizon, max_positions=1)
    prepared = prepare_portfolio_prices(data.prices)
    started = time.monotonic()
    X, y, dates, labels = [], [], [], []
    missing = 0
    # Every target's entire path (and possible two-session exit delay) precedes train_end.
    for i in range(len(data.sessions) - data.plan.horizon - 2):
        day = data.sessions[i]
        values, valid = standardized(data, day)
        window = data.sessions[i:i + data.plan.horizon + 3]
        for j, symbol in enumerate(data.plan.symbols):
            if not valid[j]:
                missing += 1
                continue
            if time.monotonic() - started >= budget.max_seconds:
                raise ComputeBudgetExceeded('Ridge label replay budget exhausted before fitting')
            tape = pd.DataFrame([dict(date=day, symbol=symbol, rank=1)])
            end = (pd.Timestamp(window[-1]) + pd.Timedelta(days=1)).date().isoformat()
            result = replay_portfolio(prepared, tape, window, config, start=day, end=end,
                no_trade_valuation_observations=[], require_explicit_corporate_actions=True)
            audit = audit_daily_replay(result, prepared, tape, window, no_trade_valuation_observations=[])
            if audit['invariant_violations'] or audit['blockers'] or audit['modeled_cash_return'] is None:
                raise ValueError(f'Unknown supervised target {day}/{symbol}; no fit, no zero substitution')
            X.append(values[j])
            y.append(audit['modeled_cash_return'])
            dates.append(day)
            labels.append(dict(date=day, symbol=symbol, resolved_at=window[-1], target=y[-1],
                               buy_executions=audit['buy_executions'], fees_rial=audit['fees_rial']))
    if len(y) < 20 or len(set(dates)) < 10:
        raise ValueError('Insufficient finite mature training targets for the engineering learner')
    weights = 1 / pd.Series(dates).map(pd.Series(dates).value_counts()).to_numpy(dtype=float)
    weights *= len(weights) / weights.sum()
    with threadpool_limits(limits=budget.threads):
        model = Ridge(alpha=1., fit_intercept=True).fit(np.asarray(X), np.asarray(y), sample_weight=weights)
    state = dict(spec=spec, status='trained', coefficients=model.coef_.tolist(), intercept=float(model.intercept_),
                 target_rows=len(y), target_sessions=len(set(dates)), missing_feature_rows=missing,
                 purged_tail_sessions=data.plan.horizon + 2, labels=labels)
    save_model(checkpoint, state)
    return state


def check_model_for_role(model, data):
    spec = model.get('spec', {})
    if (model.get('status') != 'trained' or spec.get('version') != VERSION
            or spec.get('method') not in ('ridge', 'q') or spec.get('family_id') != data.manifest['family_id']
            or spec.get('environment') != data.manifest['environment']
            or spec.get('code_identity') != learning_code_identity()
            or spec.get('plan') != data.manifest['plan'] or spec.get('scaler') != data.manifest['scaler']
            or spec.get('predictors') != list(PREDICTORS)
            or spec.get('fees') != data.manifest['fees']
            or spec.get('lock_min_move_pct') != data.manifest['lock_min_move_pct']):
        raise ValueError('Model differs from the frozen evaluation data/code/scaler contract')
    if spec['method'] == 'ridge':
        if (spec.get('parameters') != dict(alpha=1., fit_intercept=True, daily_equal_weight=True)
                or len(model.get('coefficients', [])) != len(PREDICTORS)
                or not np.isfinite(model['coefficients']).all()
                or not np.isfinite(model.get('intercept', np.nan))):
            raise ValueError('Ridge model predictor order/weights differ')
    elif (spec.get('parameters') != Q_PARAMETERS
            or model.get('completed_episodes') != spec.get('budget', {}).get('episodes')
            or not isinstance(model.get('table'), dict)
            or any(len(v) != len(data.plan.symbols) + 1 or not np.isfinite(v).all() for v in model['table'].values())):
        raise ValueError('Q model completion/weights differ')


def run_evaluation(bundle, model_paths, destination, *, seed=17, ledger=None, allow_repeat=False):
    """Validation only. Test/holdout cannot silently become another tuning loop."""
    data = load_role(bundle, 'validation')
    models = [load_model(path) for path in model_paths]
    if not models or len(models) > 8 or len({identity(m) for m in models}) != len(models):
        raise ValueError('A bounded unique frozen model list is required before validation')
    for model in models:
        check_model_for_role(model, data)
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    choices = [(m['spec']['method'] + '-' + identity(m)[:12], m, None) for m in models]
    choices += [(b, None, b) for b in ('cash', 'random', 'momentum')]
    policies = [dict(name=name, policy=dict(model_sha256=identity(model) if model else None, baseline=baseline,
                    seed=seed)) for name, model, baseline in choices]
    contract = dict(version=VERSION, role_bundle_id=data.manifest['bundle_id'], role='validation',
        scenarios=['normal', 'stress'], code_identity=learning_code_identity(),
        evaluation_budget=dict(max_paths=2*len(choices), max_seconds_per_path=300),
        scope='development description, no model promotion or independent profitability inference')
    receipt = reserve_trials(contract, policies, run_id='LEARN-VALIDATE-' + uuid.uuid4().hex,
                             allow_repeat=allow_repeat, path=ledger)
    atomic_json(root / 'registration.json', dict(contract=contract, policies=policies, receipt=receipt))
    report = dict(version=VERSION, status='running', expected_paths=len(choices)*2, paths=[],
                  test_opened=False, holdout_opened=False, accepted_for_signals=False, profitability_proven=False)
    try:
        for name, model, baseline in choices:
            for stress in (False, True):
                try:
                    values = evaluate(data, model=model, baseline=baseline, stress=stress, seed=seed)
                    path = dict(name=name, scenario='stress' if stress else 'normal', status='complete', **values)
                except (ValueError, ComputeBudgetExceeded) as error:
                    path = dict(name=name, scenario='stress' if stress else 'normal', status='unknown',
                                reason=str(error), returns=None, audit=None, metrics=None)
                report['paths'].append(path)
                atomic_json(root / 'result.json', report)
        if contract['code_identity'] != learning_code_identity():
            raise ValueError('Evaluation source changed; reject all paths')
        report['unknown_paths'] = sum(row['status'] != 'complete' for row in report['paths'])
        report['status'] = 'inconclusive' if report['unknown_paths'] else 'complete_development_evaluation'
        atomic_json(root / 'result.json', report)
        finish_trials(receipt, status='complete', evidence=report, path=ledger)
    except BaseException as error:
        report.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', reason=str(error))
        atomic_json(root / 'result.json', report)
        finish_trials(receipt, status=report['status'], evidence=report, path=ledger)
        raise
    return report


def evaluate(data, *, model=None, baseline=None, stress=False, seed=17, max_seconds=300):
    """Frozen model and matched cash/random/momentum controls; no fitting."""
    from research.rl_env import EntryTimingEnv
    if (model is None) == (baseline is None) or baseline not in (None, 'cash', 'random', 'momentum'):
        raise ValueError('Choose exactly one frozen model or declared control')
    if model is not None:
        check_model_for_role(model, data)
    rng = np.random.default_rng(seed)
    env = EntryTimingEnv(data, stress=stress)
    returns, started = [], time.monotonic()
    try:
        observation, info = env.reset(seed=seed)
        while True:
            if time.monotonic() - started >= max_seconds:
                raise ComputeBudgetExceeded('Evaluation wall time exhausted; no completed path reported')
            available = np.flatnonzero(info['action_mask'])
            if baseline == 'cash':
                action = 0
            elif baseline == 'random':
                action = int(rng.choice(available))
            elif baseline == 'momentum':
                raw = data.features.loc[data.features.date.eq(info['date'])].set_index('symbol').ret_5d
                candidates = [(float(raw[s]), j + 1) for j, s in enumerate(data.plan.symbols) if info['action_mask'][j+1]]
                action = max(candidates)[1] if candidates else 0
            elif model['spec']['method'] == 'q':
                key = q_state(observation, info, len(data.plan.symbols))
                values = _q_values(model['table'], key, env.action_space.n)
                # A fixed cash-first tie rule, including unseen states; never optimism disguised as confidence.
                action = int(available[np.argmax(values[available])])
            else:
                values, _ = standardized(data, info['date'])
                predictions = values @ np.asarray(model['coefficients']) + model['intercept']
                candidates = [(0., 0)] + [(float(predictions[j]), j + 1) for j in range(len(predictions))
                    if info['action_mask'][j+1]]
                action = max(candidates, key=lambda p: (p[0], -p[1]))[1]
            observation, reward, done, truncated, info = env.step(action)
            returns.append(reward)
            if truncated:
                raise ValueError('Evaluation cannot accept a truncated episode')
            if done:
                break
        if not np.isclose(sum(returns), env.audit['modeled_cash_return'], rtol=1e-10, atol=1e-12):
            raise ValueError('Reward does not telescope to independently audited terminal cash')
        return dict(returns=returns, audit=env.audit, tape=list(env.tape),
                    metrics=env.result.metrics, accepted_for_signals=False)
    finally:
        env.close()


def run_training(bundle, destination, *, method, budget, seed=17, resume=None, stop_after=None,
                 allow_repeat=False, ledger=None):
    data = load_role(bundle, 'train')
    admission = _admit(data, budget, method)
    spec = _spec(data, method, seed, budget)
    root = Path(destination).resolve()
    root.mkdir(parents=True, exist_ok=False)
    parent = None if resume is None else load_model(resume)
    if parent is not None and (method != 'q' or parent.get('spec') != spec or parent.get('status') != 'partial'):
        raise ValueError('Only a partial Q model under the identical full contract may be resumed')
    contract = dict(version=VERSION, training_bundle_id=data.manifest['bundle_id'], spec=spec,
                    admission=admission, resume_parent=None if parent is None else identity(parent))
    # Resume is a repetition of the same model contract, not another independent strategy.
    registration_contract = {k: v for k, v in contract.items() if k != 'resume_parent'}
    receipt = reserve_trials(registration_contract, [dict(name=method, policy=dict(method=method, seed=seed))],
        run_id='LEARN-' + uuid.uuid4().hex, allow_repeat=allow_repeat, path=ledger)
    atomic_json(root / 'registration.json', dict(contract=contract, receipt=receipt, started_at=now_tehran_iso()))
    started = time.monotonic()
    try:
        if method == 'q':
            state = fit_q(data, budget, seed=seed, checkpoint=root / 'model.json', resume=resume, stop_after=stop_after)
        else:
            if resume is not None or stop_after is not None:
                raise ValueError('Ridge fitting is atomic; only Q supports episode-boundary resume')
            state = fit_ridge(data, budget, seed=seed, checkpoint=root / 'model.json')
        if learning_code_identity() != spec['code_identity']:
            raise ValueError('Learning source changed during computation; reject this run')
        status = 'complete' if state['status'] == 'trained' else 'interrupted'
        summary = dict(status=status, method=method, model_sha256=identity(state),
            completed_episodes=state.get('completed_episodes'), target_rows=state.get('target_rows'),
            admission=admission, elapsed_seconds=time.monotonic()-started,
            profitability_proven=False, accepted_for_signals=False)
    except BaseException as error:
        status = 'interrupted' if isinstance(error, (KeyboardInterrupt, ComputeBudgetExceeded)) else 'failed'
        summary = dict(status=status, error_type=type(error).__name__, reason=str(error),
                       elapsed_seconds=time.monotonic()-started,
                       profitability_proven=False, accepted_for_signals=False)
        atomic_json(root / 'result.json', summary)
        finish_trials(receipt, status=status, evidence=summary, path=ledger)
        raise
    atomic_json(root / 'result.json', summary)
    finish_trials(receipt, status=status, evidence=summary, path=ledger)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prepare = sub.add_parser('prepare', help='Explicit market-only frozen source; no network/account database')
    for flag in ('prices', 'calendar', 'registry', 'plan', 'output', 'source-id'):
        prepare.add_argument('--' + flag, required=True)
    prepare.add_argument('--warmup-start', help='Declared raw-history boundary before training; never selected by returns')
    train = sub.add_parser('train')
    train.add_argument('--bundle', required=True)
    train.add_argument('--output', required=True)
    train.add_argument('--method', choices=('ridge', 'q'), required=True)
    train.add_argument('--seed', type=int, default=17)
    train.add_argument('--episodes', type=int, default=4)
    train.add_argument('--max-steps', type=int, default=20000)
    train.add_argument('--max-states', type=int, default=20000)
    train.add_argument('--max-seconds', type=int, default=300)
    train.add_argument('--threads', type=int, default=1)
    train.add_argument('--resume')
    train.add_argument('--stop-after', type=int)
    train.add_argument('--allow-repeat', action='store_true')
    check = sub.add_parser('check', help='Read-only CPU training admission; does not fit or reserve trials')
    check.add_argument('--bundle', required=True)
    check.add_argument('--episodes', type=int, default=4)
    evaluation = sub.add_parser('evaluate', help='Frozen validation comparison; does not open test or holdout')
    evaluation.add_argument('--bundle', required=True)
    evaluation.add_argument('--models', nargs='+', required=True)
    evaluation.add_argument('--output', required=True)
    evaluation.add_argument('--seed', type=int, default=17)
    evaluation.add_argument('--allow-repeat', action='store_true')
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            values = read_json(args.plan)
            values['symbols'] = tuple(values['symbols'])
            plan = LearningPlan(**values)
            if args.warmup_start is not None:
                from core.timeutils import is_canonical_iso_date
                if not is_canonical_iso_date(args.warmup_start) or args.warmup_start >= plan.train_start:
                    raise ValueError('Warmup boundary must be a canonical date before training')
            registry = pd.read_parquet(args.registry)
            if 'symbol_fa' in registry and 'symbol' not in registry:
                registry = registry.rename(columns={'symbol_fa': 'symbol'})
            filters = [('date', '<', plan.test_end), ('symbol', 'in', list(plan.symbols))]
            if args.warmup_start is not None:
                filters.append(('date', '>=', args.warmup_start))
            root = prepare_bundles(pd.read_parquet(args.prices, columns=list(('date', 'symbol',
                'open_raw', 'high_raw', 'low_raw', 'close_raw', 'close_final_raw', 'ref_price_raw', 'volume', 'value')),
                filters=filters),
                read_json(args.calendar), registry, plan, args.output, provenance=dict(source_id=args.source_id,
                    source_file_sha256={key: sha256(getattr(args, key)) for key in ('prices', 'calendar', 'registry', 'plan')},
                    warmup_start=args.warmup_start,
                    historical_membership_verified=False, original_publication_time_verified=False,
                    calendar_completeness_verified=False))
            print(root)
            return 0
        if args.command == 'evaluate':
            report = run_evaluation(args.bundle, args.models, args.output, seed=args.seed, allow_repeat=args.allow_repeat)
            print({k: v for k, v in report.items() if k != 'paths'})
            return 0 if report['status'] == 'complete_development_evaluation' else 2
        if args.command == 'check':
            data = load_role(args.bundle, 'train')
            budget = LearningBudget(episodes=args.episodes)
            print(dict(status='bounded_cpu_ready',
                admission={method: _admit(data, budget, method) for method in ('q', 'ridge')},
                bundle_id=data.manifest['bundle_id'], budget=asdict(budget),
                gpu_verified=False, distributed_training_verified=False,
                independent_profitability_verified=False, accepted_for_signals=False))
            return 0
        result = run_training(args.bundle, args.output, method=args.method, seed=args.seed,
            budget=LearningBudget(episodes=args.episodes, max_steps=args.max_steps, max_states=args.max_states,
                                  max_seconds=args.max_seconds, threads=args.threads),
            resume=args.resume, stop_after=args.stop_after, allow_repeat=args.allow_repeat)
        print(result)
        return 0 if result['status'] == 'complete' else 2
    except (ValueError, OSError, ComputeBudgetExceeded) as exc:
        parser.error(str(exc))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
