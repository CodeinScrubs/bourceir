"""Bounded, three-valued conjunctions over existing causal daily features.

This is feature/filter plumbing, not an optimizer or a trading policy. No outcome
column, portfolio return, fitted probability or deployment verdict is computed.
Equivalent threshold expressions are simplified algebraically, never by matching
their masks on historical/test data.
"""

import argparse
import math
import uuid
from dataclasses import asdict, dataclass
from numbers import Real
from pathlib import Path

import numpy as np
import pandas as pd

from config import settings
from core.text import normalize_symbol
from core.timeutils import is_canonical_iso_date
from features.daily import DAILY_FEATURES
from research.search_ledger import finish_trials, identity, reserve_trials
from research.training_data import atomic_json, learning_code_identity, load_role, read_json

VERSION = 'three-valued-rule-space-v1'
UNKNOWN, FALSE, TRUE = -1, 0, 1
OPERATORS = ('>', '>=', '<', '<=', '==')


@dataclass(frozen=True, order=True)
class Predicate:
    feature: str
    operator: str
    threshold: float

    def __post_init__(self):
        if (self.feature not in DAILY_FEATURES or self.operator not in OPERATORS
                or isinstance(self.threshold, (bool, np.bool_)) or not isinstance(self.threshold, Real)
                or not math.isfinite(self.threshold)):
            raise ValueError('An allowlisted daily feature, comparison and finite numeric threshold are required')
        object.__setattr__(self, 'threshold', 0. if self.threshold == 0 else float(self.threshold))


def canonical_rule(predicates):
    """Keep strongest bounds per field; preserve unknown semantics on conflicts."""
    if (not isinstance(predicates, (list, tuple)) or not 1 <= len(predicates) <= 16
            or any(not isinstance(p, Predicate) for p in predicates)):
        raise ValueError('An explicit conjunction of 1..16 typed predicates is required')
    groups = {}
    for p in set(predicates):
        groups.setdefault(p.feature, []).append(p)
    result = []
    for feature in sorted(groups):
        values = groups[feature]
        eq = sorted(p for p in values if p.operator == '==')
        lower = [p for p in values if p.operator in ('>', '>=')]
        upper = [p for p in values if p.operator in ('<', '<=')]
        low = max(lower, key=lambda p: (p.threshold, p.operator == '>')) if lower else None
        high = min(upper, key=lambda p: (p.threshold, p.operator == '<=')) if upper else None
        if len(eq) > 1:
            # Contradictory equalities remain UNKNOWN when this feature is missing.
            result.extend([eq[0], eq[-1]])
        elif eq:
            point = eq[0]
            result.append(point)
            if low and (point.threshold < low.threshold or point.threshold == low.threshold and low.operator == '>'):
                result.append(low)
            if high and (point.threshold > high.threshold or point.threshold == high.threshold and high.operator == '<'):
                result.append(high)
        elif low and high and low.threshold == high.threshold and low.operator == '>=' and high.operator == '<=':
            result.append(Predicate(feature, '==', low.threshold))
        else:
            result.extend(p for p in (low, high) if p is not None)
    return tuple(sorted(result))


def rule_id(rule):
    return identity([asdict(p) for p in canonical_rule(rule)])


def conjunction_budget(predicate_count, *, min_terms=1, max_terms=3, max_candidates=10000):
    """Count before constructing combinations or touching any market outcomes."""
    if (type(predicate_count) is not int or not 1 <= predicate_count <= 10000
            or type(min_terms) is not int or type(max_terms) is not int
            or not 1 <= min_terms <= max_terms <= min(16, predicate_count)
            or type(max_candidates) is not int or not 1 <= max_candidates <= 1_000_000):
        raise ValueError('Invalid declared conjunction space or finite candidate budget')
    total = sum(math.comb(predicate_count, k) for k in range(min_terms, max_terms+1))
    if total > max_candidates:
        raise ValueError(f'Nominal conjunction count {total} exceeds budget {max_candidates} before materialization')
    return dict(nominal_candidates=total, max_candidates=max_candidates,
        logical_unique_count_known=False, statistical_independent_trials_verified=False, accepted_for_signals=False)


class RuleMatrix:
    """Immutable feature snapshot and reusable predicate masks; CPU only.

    AND uses TRUE iff all operands are known true, FALSE iff at least one is
    known false, and UNKNOWN otherwise. An absent feature is UNKNOWN, never a
    fabricated neutral number. Aliases of the same mathematical rule share work.
    """

    def __init__(self, features, named_rules, *, batch_size=64, max_rules=10000, max_bytes=64*1024*1024):
        if (not isinstance(features, pd.DataFrame) or features.empty or len(features) > 500000
                or features.columns.duplicated().any() or not {'date', 'symbol'} <= set(features)
                or not set(features) <= {'date', 'symbol', *DAILY_FEATURES}
                or features.duplicated(['date', 'symbol']).any()
                or not features.date.map(is_canonical_iso_date).all()
                or features.date.ge(settings.RESEARCH_CONFIG['holdout_start']).any()
                or not features.symbol.map(lambda s: isinstance(s, str) and bool(s) and normalize_symbol(s) == s).all()
                or type(max_rules) is not int or not 1 <= max_rules <= 100000
                or not isinstance(named_rules, list) or not 1 <= len(named_rules) <= max_rules
                or type(batch_size) is not int or not 1 <= batch_size <= 4096
                or type(max_bytes) is not int or not 1 <= max_bytes <= 512*1024*1024):
            raise ValueError('Bounded market-only feature keys, explicit rules and positive memory/batch limits are required')
        aliases, rules, names = {}, {}, set()
        for item in named_rules:
            if (not isinstance(item, dict) or set(item) != {'name', 'predicates'}
                    or not isinstance(item['name'], str) or not item['name'] or len(item['name']) > 256
                    or item['name'] in names):
                raise ValueError('Every rule needs a unique explicit name and typed predicates')
            names.add(item['name'])
            rule = canonical_rule(item['predicates'])
            key = rule_id(rule)
            rules[key] = rule
            aliases.setdefault(key, []).append(item['name'])
        atoms = sorted({p for rule in rules.values() for p in rule})
        estimated = int(features.memory_usage(deep=True).sum())*2 + len(features)*(2*len(atoms)+8*batch_size+32)
        if estimated > max_bytes:
            raise ValueError('Predicate/batch memory exceeds the declared budget before allocation')
        self.keys = features[['date', 'symbol']].sort_values(['date', 'symbol']).reset_index(drop=True)
        ordered = features.sort_values(['date', 'symbol']).reset_index(drop=True)
        self.batch_size, self.rules, self.aliases = batch_size, rules, aliases
        self._masks = {}
        for atom in atoms:
            column = (ordered[atom.feature] if atom.feature in ordered else pd.Series(np.nan, index=ordered.index))
            if column.map(lambda v: isinstance(v, (bool, np.bool_))).any():
                raise ValueError('Boolean values cannot silently become numeric feature measurements')
            values = pd.to_numeric(column, errors='raise').to_numpy(dtype=float, na_value=np.nan)
            finite = np.isfinite(values)
            compare = {'>': np.greater, '>=': np.greater_equal, '<': np.less, '<=': np.less_equal, '==': np.equal}[atom.operator]
            mask = np.where(finite, compare(values, atom.threshold).astype(np.int8), UNKNOWN).astype(np.int8)
            mask.flags.writeable = False
            self._masks[atom] = mask
        self.admission = dict(version=VERSION, rows=len(features), nominal_named_rules=len(named_rules),
            logical_unique_rules=len(rules), aliases_avoided=len(named_rules)-len(rules), unique_predicates=len(atoms),
            estimated_peak_bytes=estimated, declared_max_bytes=max_bytes, batch_size=batch_size,
            backend='numpy-cpu', empirical_mask_deduplication=False,
            performance_evaluated=False, statistical_independent_trials_verified=False, accepted_for_signals=False)

    def batches(self):
        items = list(self.rules.items())
        for offset in range(0, len(items), self.batch_size):
            part = items[offset:offset+self.batch_size]
            values = np.empty((len(self.keys), len(part)), dtype=np.int8)
            for j, (_, rule) in enumerate(part):
                false, unknown = np.zeros(len(self.keys), dtype=bool), np.zeros(len(self.keys), dtype=bool)
                for atom in rule:
                    mask = self._masks[atom]
                    false |= mask == FALSE
                    unknown |= mask == UNKNOWN
                values[:, j] = np.where(false, FALSE, np.where(unknown, UNKNOWN, TRUE))
            yield [key for key, _ in part], values

    def summaries(self):
        result = []
        for names, values in self.batches():
            for j, key in enumerate(names):
                result.append(dict(rule_id=key, aliases=self.aliases[key],
                    predicates=[asdict(p) for p in self.rules[key]], rows=len(values),
                    true_rows=int((values[:, j] == TRUE).sum()), false_rows=int((values[:, j] == FALSE).sum()),
                    unknown_rows=int((values[:, j] == UNKNOWN).sum()), accepted_for_signals=False))
        return result


def run_filter_review(bundle, rule_file, output, *, allow_repeat=False, ledger=None):
    """Register a train-only feature-frequency query before evaluating masks."""
    data = load_role(bundle, 'train')
    specs = read_json(rule_file)
    if not isinstance(specs, list) or len(specs) > 10000:
        raise ValueError('A bounded explicit JSON list of named rules is required')
    named = [dict(name=item['name'], predicates=[Predicate(**p) for p in item['predicates']]) for item in specs]
    # Constructor builds masks. Register the canonical policies BEFORE that work.
    unique = {}
    for item in named:
        rule = canonical_rule(item['predicates'])
        unique.setdefault(rule_id(rule), dict(name=item['name'], policy=dict(predicates=[asdict(p) for p in rule])))
    contract = dict(version=VERSION, bundle_id=data.manifest['bundle_id'], role='train',
        source_code_identity=learning_code_identity(), purpose='feature frequency; no returns, forecasts or model selection')
    receipt = reserve_trials(contract, list(unique.values()), run_id='RULES-'+uuid.uuid4().hex,
                             allow_repeat=allow_repeat, path=ledger)
    root = Path(output).resolve()
    try:
        root.mkdir(parents=True, exist_ok=False)
        atomic_json(root/'registration.json', dict(contract=contract, receipt=receipt))
        matrix = RuleMatrix(data.features, named)
        report = dict(status='descriptive_complete', admission=matrix.admission, rules=matrix.summaries(),
            performance_evaluated=False, accepted_for_signals=False, profitability_proven=False)
        atomic_json(root/'result.json', report)
        finish_trials(receipt, status='complete', evidence=report, path=ledger)
        return report
    except BaseException as error:
        finish_trials(receipt, status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed',
                      evidence=dict(error_type=type(error).__name__, reason=str(error)), path=ledger)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True)
    parser.add_argument('--rules', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--allow-repeat', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = run_filter_review(args.bundle, args.rules, args.output, allow_repeat=args.allow_repeat)
        print(report['admission'])
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
