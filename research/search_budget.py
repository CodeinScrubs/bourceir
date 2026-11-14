"""Outcome-free Monte Carlo resolution planning; not a power or profit estimate.

For the existing plus-one p-value and Bonferroni diagnostics the smallest
adjusted p-value is M/(B+1). Resolving that tail is necessary, not sufficient,
for evidence: dependence, model validity and the complete search still matter.
"""

import argparse
import json
from decimal import ROUND_CEILING, Decimal

VERSION = 'search-resolution-budget-v1'


def resampling_budget(*, replications, family_comparisons, confidence=.95):
    if type(replications) is not int or replications < 1:
        raise ValueError('A positive integer replication budget is required')
    if family_comparisons is not None and (type(family_comparisons) is not int or family_comparisons < 1):
        raise ValueError('The comparison family must be positive or explicitly unknown')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not .5 < confidence < 1:
        raise ValueError('Confidence must be finite and strictly between .5 and 1')
    alpha = Decimal(1) - Decimal(str(confidence))
    required, minimum, adequate = None, None, None
    if family_comparisons is not None:
        required = int((Decimal(family_comparisons) / alpha).to_integral_value(rounding=ROUND_CEILING)) - 1
        minimum = min(1., family_comparisons / (replications + 1))
        adequate = Decimal(family_comparisons) <= alpha * (replications + 1)
    return dict(version=VERSION, replications=replications, family_comparisons=family_comparisons,
        confidence=confidence, minimum_replications_for_resolution=required,
        minimum_attainable_family_p_value=minimum, family_tail_resolution_adequate=adequate,
        extra_replications_for_resolution=None if required is None else max(0, required - replications),
        family_count_verified=False, statistical_power_established=False, strategy_verdict=None,
        accepted_for_signals=False,
        limitations=['plus-one one-sided Monte Carlo p-value with declared Bonferroni family only',
            'resolution is not statistical power, selection correction verification, or a recommendation to spend this budget',
            'all tried variants, seeds, features, horizons and inspected comparisons must be disclosed'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--trials', type=int, required=True)
    parser.add_argument('--comparisons-per-trial', type=int, default=3)
    parser.add_argument('--replications', type=int, default=5000)
    parser.add_argument('--confidence', type=float, default=.95)
    args = parser.parse_args(argv)
    try:
        if args.trials < 1 or args.comparisons_per_trial < 1:
            raise ValueError('Positive trial and comparison counts are required')
        report = resampling_budget(replications=args.replications,
            family_comparisons=args.trials * args.comparisons_per_trial, confidence=args.confidence)
    except ValueError as exc:
        parser.error(str(exc))
    report.update(declared_trials=args.trials, comparisons_per_trial=args.comparisons_per_trial,
                  expensive_computation_started=False)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report['family_tail_resolution_adequate'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
