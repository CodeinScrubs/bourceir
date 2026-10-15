"""بازبینی توصیفی شواهد توسعه؛ نتیجه مثبت، مجوز سیگنال نمی‌سازد."""

import math
from statistics import mean, median

REVIEW_VERSION = 'research-evidence-review-v1'


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def mode_summary(metrics):
    returns = [m['total_return'] for m in metrics if m['total_return'] is not None]
    known = [m for m in metrics if m['total_return'] is not None]
    all_known = len(returns) == len(metrics)
    stress_fail = None if not all_known else mean(returns) <= 0
    return dict(folds=len(metrics), known_folds=len(returns), unknown_folds=len(metrics) - len(returns),
                positive_folds=sum(r > 0 for r in returns), zero_folds=sum(r == 0 for r in returns),
                negative_folds=sum(r < 0 for r in returns),
                mean_known_fold_return=mean(returns) if returns else None,
                median_known_fold_return=median(returns) if returns else None,
                all_fold_mean=mean(returns) if all_known else None,
                nonpositive_all_fold_mean=stress_fail,
                worst_known_drawdown=max((m['max_drawdown'] for m in known), default=None),
                worst_known_loss_from_initial=max((m['max_loss_from_initial'] for m in known), default=None),
                loss_floor_breached_folds=sum(m['loss_floor_breached'] for m in known),
                unknown_loss_floor_folds=sum(m['total_return'] is None for m in metrics),
                folds_with_open_positions=sum(m['open_positions'] > 0 for m in metrics),
                folds_with_stale_marks=sum(m['stale_mark_days'] > 0 for m in metrics),
                no_execution_folds=sum(m['executions'] == 0 for m in metrics),
                corporate_action_events=sum(m['corporate_action_events'] for m in metrics),
                actual_corporate_entitlements_verified=all(m['actual_corporate_entitlements_verified'] for m in metrics))


def paired_comparison(results, left, right):
    rows = []
    for i, fold in enumerate(results):
        a, b = (fold['modes'][name]['metrics']['total_return'] for name in (left, right))
        rows.append(dict(fold=i, left=a, right=b, difference=None if a is None or b is None else a - b))
    differences = [r['difference'] for r in rows if r['difference'] is not None]
    return dict(left=left, right=right, total_folds=len(rows), paired_known_folds=len(differences),
                excluded_unknown_pairs=len(rows) - len(differences), rows=rows,
                mean_known_difference=mean(differences) if differences else None,
                median_known_difference=median(differences) if differences else None,
                positive_pairs=sum(v > 0 for v in differences), tied_pairs=sum(v == 0 for v in differences),
                negative_pairs=sum(v < 0 for v in differences),
                statistical_significance=None, comparable_risk_proven=False)


def summarize_evidence(payload, checked_results):
    """اعداد از منحنی محفوظ بازسازی شده‌اند؛ میانگین برش‌ها CAGR نیست."""
    protocol = payload['protocol']
    modes = list(checked_results[0]['modes'])
    summaries = {name: mode_summary([fold['modes'][name]['metrics'] for fold in checked_results]) for name in modes}
    candidates = ['selected', 'dip'] if protocol['version'] == 'expanding-portfolio-v3' else [
        name for name in modes if name not in ('past_baseline', 'past_baseline_stress')
        and not name.endswith(('-stress', '-random'))]
    reviews = {}
    for candidate in candidates:
        if candidate == 'selected':
            stress, baseline, baseline_stress, control = 'stress', 'baseline', None, 'random'
        elif candidate == 'dip':
            stress, baseline, baseline_stress, control = 'dip_stress', 'dip_vwap', None, 'dip_random'
        else:
            stress, baseline, baseline_stress, control = candidate + '-stress', 'past_baseline', 'past_baseline_stress', candidate + '-random'
        comparisons = {name: paired_comparison(checked_results, candidate, other) for name, other in
                       [('stress_sensitivity', stress), ('normal_baseline', baseline), ('random_control', control)]}
        if baseline_stress:
            comparisons['stressed_baseline'] = paired_comparison(checked_results, stress, baseline_stress)
        if candidate == 'dip':
            comparisons['selected_past_baseline'] = paired_comparison(checked_results, candidate, 'selected')
            comparisons['stressed_selected_past_baseline'] = paired_comparison(checked_results, stress, 'stress')
        normal, stressed = summaries[candidate], summaries[stress]
        blockers = ['development_data_already_seen', 'independent_future_test_missing',
                    'fills_and_queue_position_unverified', 'historical_universe_incomplete',
                    'historical_fee_schedule_unverified', 'multiple_testing_and_uncertainty_not_established',
                    'risk_matched_benchmark_not_established', 'one_random_seed_only']
        if normal['unknown_folds'] or stressed['unknown_folds']:
            blockers.append('unknown_fold_results')
        if normal['nonpositive_all_fold_mean']:
            blockers.append('nonpositive_normal_mean')
        if stressed['nonpositive_all_fold_mean']:
            blockers.append('nonpositive_stress_mean')
        if normal['loss_floor_breached_folds'] or stressed['loss_floor_breached_folds']:
            blockers.append('simulated_loss_floor_breached')
        if not normal['actual_corporate_entitlements_verified'] or not stressed['actual_corporate_entitlements_verified']:
            blockers.append('actual_corporate_entitlements_unverified')
        if normal['folds_with_open_positions'] or stressed['folds_with_open_positions']:
            blockers.append('fold_boundary_open_positions')
        if normal['folds_with_stale_marks'] or stressed['folds_with_stale_marks']:
            blockers.append('stale_marks_present')
        reviews[candidate] = dict(normal=normal, stress=stressed, comparisons=comparisons, blockers=blockers,
                                 eligible_for_profitability_claim=False, accepted_for_signals=False)
    return dict(version=REVIEW_VERSION, experiment_id=payload['experiment_id'], summaries=summaries,
                candidates=reviews, accepted_for_signals=False, portfolio_cagr=None,
                financial_assessment='development_diagnostics_only',
                evidence_scope='stored_daily_proxy_replay',
                limitations=['folds are independent accounts; means are not compound annual returns',
                             'unknown outcomes remain in counts and prevent an all-fold mean',
                             'positive paired differences do not establish significance or comparable risk',
                             'local hashes and database records are not external attestation',
                             'this review cannot approve a strategy or unlock the holdout'])
