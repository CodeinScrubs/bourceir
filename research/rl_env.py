"""Gymnasium entry-timing environment backed by the existing shared-cash replay.

One engine pass per episode, with an EOD callback bridge. Exit timing is fixed
at 2/3 sessions. This is not an intraday/order-queue simulator or a Python sandbox.
"""

import queue
import threading

import gymnasium as gym
import numpy as np
import pandas as pd
from gymnasium import spaces

from backtest.evidence import audit_daily_replay
from backtest.portfolio import PortfolioConfig, replay_portfolio
from backtest.prepared import prepare_portfolio_prices
from research.training_data import PREDICTORS, RoleData, standardized

VERSION = 'fixed-exit-entry-env-v1'


class InvalidEpisode(ValueError):
    """Unknown rewards are errors, never zero-return samples or harmless truncations."""


class _Cancelled(Exception):
    pass


class EntryTimingEnv(gym.Env):
    """Actions: 0 = no new entry; 1..N = one declared ordinary share.

    Use info['action_mask']; invalid/missing-feature/tail actions raise. Daily
    reward is change in the same net-sell-fee marked NAV / initial capital. All
    rewards remain provisional until terminal independent audit and liquidation.
    Consumers must discard the entire episode on InvalidEpisode.
    """

    metadata = {'render_modes': []}

    def __init__(self, data, *, stress=False, timeout_seconds=30.):
        super().__init__()
        if (not isinstance(data, RoleData) or type(stress) is not bool
                or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60):
            raise ValueError('A frozen role and bounded engine timeout are required')
        self.data, self.stress, self.timeout = data, stress, float(timeout_seconds)
        self.symbols = data.plan.symbols
        self._session_positions = {day: i for i, day in enumerate(data.sessions)}
        self.config = PortfolioConfig(initial_capital_rial=data.plan.initial_capital_rial,
            horizon=data.plan.horizon, max_positions=1, stress=stress)
        self.action_space = spaces.Discrete(len(self.symbols) + 1)
        size = len(self.symbols) * (len(PREDICTORS) + 2) + 4
        self.observation_space = spaces.Box(-np.inf, np.inf, shape=(size,), dtype=np.float64)
        self._prepared = prepare_portfolio_prices(data.prices)
        self._worker = None
        self._cancel = threading.Event()
        self._events, self._actions = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        self._state = None
        self._done = True
        self.result, self.audit, self.tape = None, None, []
        self._previous_equity = float(self.config.initial_capital_rial)

    def _publish(self, item):
        while not self._cancel.is_set():
            try:
                self._events.put(item, timeout=.1)
                return
            except queue.Full:
                continue
        raise _Cancelled()

    def _policy(self, state):
        self._publish(('decision', state))
        while not self._cancel.is_set():
            try:
                action = self._actions.get(timeout=.1)
                choices = [] if action == 0 else [self.symbols[action - 1]]
                if choices:
                    self.tape.append(dict(date=state['date'], symbol=choices[0], rank=1))
                return choices
            except queue.Empty:
                continue
        raise _Cancelled()

    def _run(self):
        try:
            result = replay_portfolio(self._prepared, pd.DataFrame(columns=['date', 'symbol', 'rank']),
                self.data.sessions, self.config, start=self.data.sessions[0],
                no_trade_valuation_observations=[], require_explicit_corporate_actions=True,
                decision_policy=self._policy)
            tape = pd.DataFrame(self.tape, columns=['date', 'symbol', 'rank'])
            audit = audit_daily_replay(result, self._prepared, tape, self.data.sessions,
                                       no_trade_valuation_observations=[])
            if (audit['invariant_violations'] or audit['blockers']
                    or audit['modeled_cash_return'] is None
                    or not np.isfinite(audit['modeled_cash_return'])):
                reasons = sorted(set(audit['blockers'] + audit['invariant_violations']))
                raise InvalidEpisode('Unusable reward path: ' + ', '.join(reasons))
            self._publish(('terminal', (result, audit)))
        except _Cancelled:
            return
        except BaseException as exc:
            try:
                self._publish(('error', exc))
            except _Cancelled:
                return

    def _receive(self):
        try:
            kind, value = self._events.get(timeout=self.timeout)
        except queue.Empty as exc:
            self.close()
            raise InvalidEpisode('Engine step timeout; no reward sample accepted') from exc
        if kind == 'error':
            self.close()
            raise InvalidEpisode('Engine failed; discard the whole episode: ' + str(value)) from value
        return kind, value

    def _view(self, state):
        day = state['date']
        if (state['stale_positions'] or state['stale_fractional_exposures']
                or not np.isfinite(state['equity_rial']) or not np.isfinite(state['cash_rial'])):
            raise InvalidEpisode('Current book valuation is unknown; discard the whole episode')
        values, valid = standardized(self.data, day)
        index = self._session_positions[day]
        mask = np.zeros(len(self.symbols) + 1, dtype=np.int8)
        mask[0] = 1
        # Fixed declared exit runway; no hindsight liquidation or late entry.
        can_enter = (index < len(self.data.sessions) - self.config.horizon - 2
                     and state['positions'] == 0 and not state['stopped_new_entries'])
        if can_enter:
            mask[1:] = valid.astype(np.int8)
        exposures = np.zeros(len(self.symbols), dtype=float)
        for holding in state['holdings']:
            exposures[self.symbols.index(holding['symbol'])] += (
                holding['quantity'] * holding['mark_rial'] / self.config.initial_capital_rial)
        observation = np.concatenate((values.ravel(), valid.astype(float), exposures,
            [state['cash_rial'] / self.config.initial_capital_rial, float(state['positions']),
             float(state['stopped_new_entries']), (len(self.data.sessions) - 1 - index) / len(self.data.sessions)]))
        if not np.isfinite(observation).all():
            raise InvalidEpisode('Observation is nonfinite')
        return observation, dict(date=day, action_mask=mask,
            missing_feature_symbols=[s for s, known in zip(self.symbols, valid) if not known],
            reward_provisional_until_terminal=True, accepted_for_signals=False)

    def reset(self, *, seed=None, options=None):
        self.close()
        super().reset(seed=seed)
        if options:
            raise ValueError('An episode cannot change the frozen data/capital contract through reset options')
        if seed is not None:
            self.action_space.seed(seed)
        self._cancel = threading.Event()
        self._events, self._actions = queue.Queue(maxsize=1), queue.Queue(maxsize=1)
        self._done = False
        self.result, self.audit, self.tape = None, None, []
        self._previous_equity = float(self.config.initial_capital_rial)
        self._worker = threading.Thread(target=self._run, daemon=True, name='brs-entry-replay')
        self._worker.start()
        kind, state = self._receive()
        if kind != 'decision':
            self.close()
            raise InvalidEpisode('Episode did not yield its first EOD decision')
        self._state = state
        try:
            return self._view(state)
        except BaseException:
            self.close()
            raise

    def step(self, action):
        if (self._done or self._state is None or isinstance(action, (bool, np.bool_))
                or not isinstance(action, (int, np.integer)) or not self.action_space.contains(action)):
            raise ValueError('Reset before stepping; an integer action in the declared space is required')
        _, info = self._view(self._state)
        if not info['action_mask'][action]:
            raise ValueError('Action is masked: unavailable features, occupied book, stop or terminal exit tail')
        self._actions.put(int(action), timeout=self.timeout)
        kind, value = self._receive()
        if kind == 'decision':
            self._state = value
            try:
                observation, info = self._view(value)
            except BaseException:
                self.close()
                raise
            equity = value['equity_rial']
            terminated = False
        else:
            self.result, self.audit = value
            equity = self.result.metrics['final_equity_rial']
            self._done = True
            observation = np.zeros(self.observation_space.shape, dtype=np.float64)
            info = dict(date=self.data.sessions[-1], action_mask=np.array([1] + [0] * len(self.symbols), dtype=np.int8),
                audit=self.audit, reward_provisional_until_terminal=False, accepted_for_signals=False)
            terminated = True
        reward = (equity - self._previous_equity) / self.config.initial_capital_rial
        self._previous_equity = equity
        if not np.isfinite(reward):
            self.close()
            raise InvalidEpisode('Reward is unknown')
        return observation, float(reward), terminated, False, info

    def close(self):
        self._cancel.set()
        if self._worker is not None:
            self._worker.join(timeout=3.)
            if self._worker.is_alive():
                raise RuntimeError('Engine worker did not stop; no additional episode may start')
            self._worker = None
        self._state, self._done = None, True
