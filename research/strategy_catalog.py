"""Finite, explicit adapters for existing rules; this is not a strategy search."""

from dataclasses import asdict, dataclass

from core.flow_filters import RESEARCH_FILTERS, V4_FILTERS
from research.baselines import STRATEGIES
from research.dip import DipRule
from research.dip_run import MAIN_RULES, SENSITIVITY_RULES
from research.surge_patterns import HORIZONS, HYPOTHESES

VERSION = 'strategy-catalog-v1'
SEEDS = (41, 97, 173)
HOLDING_HORIZONS = (1, 2, 3, 5)  # sessions AFTER entry; 1/2 mean two/three inclusive dates


@dataclass(frozen=True)
class StrategyCase:
    name: str
    family: str
    selector: str
    horizon: int
    dip_rule: DipRule | None = None
    allocation: float = 1.
    loss_fraction: float = .10
    sensitivity_only: bool = False


def fixed_cases():
    """Every current fixed DIP parameter and baseline selector is explicit.

    The two optimistic OHLC rules are retained as sensitivity bounds. They are
    never eligible for promotion. Surge cases retain their separate 30% entry
    budget and 4.5%-of-total-capital loss floor from the existing research.
    """
    cases = [StrategyCase('cash', 'control', 'cash', 1),
             StrategyCase('buy_hold', 'control', 'buy_hold', 1)]
    selectors = [s.name for s in STRATEGIES]
    controls = ['liquidity', *(f'random_{seed}' for seed in SEEDS)]
    for h in HOLDING_HORIZONS:
        cases.extend(StrategyCase(f'base:{s}:h{h}', 'baseline', s, h) for s in selectors)
        cases.extend(StrategyCase(f'control:{s}:h{h}', 'control', s, h) for s in controls)
        cases.extend(StrategyCase(f'flow:{s}:h{h}', 'flow', f'flow:{s}', h) for s in RESEARCH_FILTERS)
        cases.extend(StrategyCase(f'flow4:{s}:h{h}', 'flow4', f'flow4:{s}', h) for s in V4_FILTERS)
    for rule in MAIN_RULES + SENSITIVITY_RULES:
        cases.extend(StrategyCase(f'dip:{rule.name}:{s}', 'dip', s, rule.hold,
            dip_rule=rule, sensitivity_only=not rule.conservative) for s in [*selectors, *controls])
    for h in HORIZONS:
        cases.extend(StrategyCase(f'surge:{s.name}:inclusive{h}', 'surge', f'surge:{s.name}', h-1,
            allocation=.30, loss_fraction=.045) for s in HYPOTHESES)
    if len({c.name for c in cases}) != len(cases):
        raise ValueError('Duplicate registered strategy case')
    return tuple(cases)


def catalogue():
    """Distinguish a financial replay adapter from mathematical/regression tests.

    Fitted models have different outcome, sizing and training contracts. Their
    dedicated chronological workflows remain authoritative. No old forecast is
    silently reinterpreted as a score in this fixed-rule matrix.
    """
    separate = [
        ('temporal_baseline_selection', 'research/portfolio_run.py', 'tests/test_portfolio.py'),
        ('conditional_expectancy', 'research/expectancy_run.py', 'tests/test_expectancy.py'),
        ('context_expectancy', 'research/context_expectancy.py', 'tests/test_context_expectancy.py'),
        ('dip_expectancy', 'research/dip_expectancy_run.py', 'tests/test_dip_expectancy_run.py'),
        ('sized_dip_expectancy', 'research/sized_dip_run.py', 'tests/test_sized_dip_run.py'),
        ('two_stage_execution_return', 'research/two_stage_dip_run.py', 'tests/test_two_stage_dip_run.py'),
        ('frequency_probability', 'research/probability_run.py', 'tests/test_probability.py'),
        ('holding_horizon_selection', 'research/holding_horizon.py', 'tests/test_holding_horizon.py'),
        ('risk_floor_exit', 'research/risk_exit_study.py', 'tests/test_risk_exit_study.py'),
        ('anniversary_economic_accounting', 'research/anniversary_accounting.py', 'tests/test_anniversary_accounting.py'),
        ('frozen_aban07_rotation', 'research/aban_rotation.py', 'tests/test_aban_rotation.py'),
    ]
    diagnostics = [
        ('rights_opening_peak', 'research/rights_opening.py', 'tests/test_rights_opening_research.py'),
        ('rare_events', 'research/rare_events.py', 'tests/test_rare_events.py'),
        ('sell_wall', 'research/sell_wall.py', 'tests/test_sell_wall.py'),
        ('price_levels', 'research/price_level_audit.py', 'tests/test_price_levels.py'),
    ]
    return dict(version=VERSION, cases=[asdict(case) for case in fixed_cases()],
        separate_registered_workflows=[dict(family=n, source=s, tests=t,
            status='requires_own_registered_inputs_and_temporal_protocol') for n, s, t in separate],
        descriptive_studies=[dict(family=n, source=s, tests=t, status='not_a_complete_trading_policy')
                             for n, s, t in diagnostics],
        exhaustive_over_all_possible_algorithms=False, accepted_for_signals=False)
