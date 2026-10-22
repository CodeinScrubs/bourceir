"""Research extension contracts: synthetic inputs only, no DB/network/holdout."""
import hashlib
import json

import numpy as np
import pandas as pd
import pytest
from pandas.testing import assert_frame_equal

from research.extension_panel import merge_research_extension

BODY = b'test-only preserved observation'
DIGEST = hashlib.sha256(BODY).hexdigest()


def base(**changes):
    return pd.DataFrame([dict(date='2024-01-02', symbol='نمونه',
        decision_at='2024-01-02T09:00:00+03:30', ret_1d=.01) | changes], index=[77])


def observation(**changes):
    return dict(date='2024-01-02', symbol='نمونه', feature='metric', value=0., unit='Rial',
        observed_at='2024-01-02T08:58:00+03:30', available_at='2024-01-02T08:59:00+03:30',
        source='test', source_sha256=DIGEST, module_version='test-v1', quality_status='valid') | changes


def merge(rows=None, canonical=None, **changes):
    args = dict(module='test', module_version='test-v1', feature_units={'metric':'Rial'},
        sources={DIGEST:BODY}, module_code_sha256=DIGEST, end_exclusive='2025-03-21') | changes
    return merge_research_extension(base() if canonical is None else canonical,
        pd.DataFrame([observation()] if rows is None else rows, dtype=object), **args)


def test_zero_is_observed_without_mutating_inputs_or_index():
    canonical, obs = base(), pd.DataFrame([observation()])
    before, before_obs = canonical.copy(), obs.copy()
    output, audit = merge_research_extension(canonical, obs, module='test', module_version='test-v1',
        feature_units={'metric':'Rial'}, sources={DIGEST:BODY}, module_code_sha256=DIGEST,
        end_exclusive='2025-03-21')
    assert output.loc[77, 'test__metric'] == 0.
    assert output.loc[77, 'test__metric__status'] == 'eligible'
    assert_frame_equal(canonical, before)
    assert_frame_equal(obs, before_obs)
    assert audit['output_rows'] == audit['base_rows'] == 1
    assert not audit['accepted_for_signals'] and not audit['profitability_proven']
    assert not audit['native_semantics_independently_verified']


def test_module_cannot_move_canonical_decision_into_future():
    with pytest.raises(ValueError, match='زمان تصمیم'):
        merge([observation(decision_at='2024-01-02T11:00:00+03:30',
                           available_at='2024-01-02T10:00:00+03:30')])


@pytest.mark.parametrize('changes,reason', [
    ({'available_at':'2024-01-02T09:00:00.000001+03:30'}, 'unavailable_at_decision'),
    ({'observed_at':'2024-01-02T09:00:00+03:30'}, 'unavailable_at_decision'),
    ({'available_at':'2024-01-02T08:59:00'}, 'unknown_or_naive_timestamp'),
    ({'available_at':None}, 'unknown_or_naive_timestamp'),
    ({'observed_at':pd.NaT}, 'unknown_or_naive_timestamp'),
    ({'module_version':pd.NA}, 'module_version_mismatch'),
    ({'module_version':'newer'}, 'module_version_mismatch'),
    ({'quality_status':'VERIFIED'}, 'unverified_quality'),
    ({'quality_status':pd.NA}, 'unverified_quality'),
    ({'source':None}, 'missing_source_identity'),
    ({'source':pd.NA}, 'missing_source_identity'),
    ({'source_sha256':'0'*64}, 'unbound_source_bytes'),
    ({'source_sha256':pd.NA}, 'unbound_source_bytes'),
    ({'unit':'Toman'}, 'unit_mismatch'),
    ({'unit':pd.NA}, 'unit_mismatch'),
    ({'value':None}, 'unknown_or_invalid_number'),
    ({'value':float('inf')}, 'unknown_or_invalid_number'),
    ({'value':float('-inf')}, 'unknown_or_invalid_number'),
    ({'value':True}, 'unknown_or_invalid_number'),
    ({'value':np.bool_(True)}, 'unknown_or_invalid_number'),
    ({'value':'0'}, 'unknown_or_invalid_number'),
    ({'value':10**400}, 'unknown_or_invalid_number'),
])
def test_bad_late_or_unknown_data_are_visible_and_not_zero(changes, reason):
    out, audit = merge([observation(**changes)])
    assert pd.isna(out.loc[77, 'test__metric'])
    assert out.loc[77, 'test__metric__status'] == reason
    assert audit['status_counts'] == {reason:1}
    json.dumps(audit, allow_nan=False)


def test_unknown_observation_stays_in_base_denominator():
    missing = base(symbol='دوم')
    out, audit = merge(canonical=pd.concat([base(), missing], ignore_index=True))
    assert len(out) == 2 and audit['base_rows'] == 2
    assert out.loc[1, 'test__metric__status'] == 'no_observation'
    assert pd.isna(out.loc[1, 'test__metric'])


def test_source_bytes_must_match_declared_hash():
    with pytest.raises(ValueError, match='بایت'):
        merge(sources={DIGEST:b'different revision'})


@pytest.mark.parametrize('name', ['label_positive_h5','adj_close','net_ret_5d','future_price','x__label_success'])
def test_outcomes_and_backward_adjustments_are_not_features(name):
    with pytest.raises(ValueError, match='فیچر'):
        merge([observation(feature=name)], feature_units={name:'ratio'})


def test_undeclared_columns_and_features_are_not_silently_imported():
    with pytest.raises(ValueError, match='ستون'):
        merge([observation(label_positive_h5=1)])
    with pytest.raises(ValueError, match='اعلام'):
        merge([observation(feature='unregistered')])


def test_duplicate_normalized_keys_are_ambiguous():
    with pytest.raises(ValueError, match='تکراری'):
        merge([observation(symbol='آزمايش'), observation(symbol='آزمایش')])
    b = pd.concat([base(symbol='آزمايش'), base(symbol='آزمایش')], ignore_index=True)
    with pytest.raises(ValueError, match='یکتا'):
        merge(canonical=b)


@pytest.mark.parametrize('canonical', [base(decision_at='2024-01-02T09:00:00'),
    base(decision_at='2024-01-01T19:00:00Z'), base(symbol=None), base(date='2024-02-30'),
    base(date='2026-10-20'), base(label_positive_h5=1), base(test__metric=1)])
def test_bad_base_boundaries_identity_or_columns_fail(canonical):
    with pytest.raises(ValueError):
        merge(canonical=canonical)


def test_sealed_module_rows_rejected_even_when_unmatched():
    with pytest.raises(ValueError, match='مرز'):
        merge([observation(date='2026-10-20', symbol='ناشناخته')])
    with pytest.raises(ValueError, match='مهروموم'):
        merge(end_exclusive='2026-01-01')


def test_future_samples_do_not_change_existing_prefix():
    # Both dates remain inside development; the new row has a different decision.
    first, _ = merge()
    b = pd.concat([base(), base(date='2024-01-03', decision_at='2024-01-03T09:00:00+03:30')], ignore_index=True)
    out, _ = merge([observation(), observation(date='2024-01-03', value=999,
        observed_at='2024-01-03T08:58:00+03:30', available_at='2024-01-03T08:59:00+03:30')], canonical=b)
    assert_frame_equal(first.reset_index(drop=True), out.iloc[:1].reset_index(drop=True))


def test_timezone_equivalence_and_normalized_symbol_match():
    out, _ = merge([observation(symbol='آزمايش', available_at='2024-01-02T05:29:00Z')],
                   canonical=base(symbol='آزمایش'))
    assert out.loc[77, 'test__metric__status'] == 'eligible'


def test_new_unmatched_symbol_does_not_expand_universe():
    out, audit = merge([observation(), observation(symbol='خارج')])
    assert len(out) == 1 and audit['outside_base_rows'] == 1


def test_generated_status_column_cannot_also_be_a_feature():
    rows = [observation(), observation(feature='metric__status', value=42.)]
    with pytest.raises(ValueError, match='برخورد'):
        merge(rows, feature_units={'metric':'Rial', 'metric__status':'Rial'})


def test_manifest_changes_when_the_observed_feature_value_changes():
    _, first = merge([observation(value=1.)])
    _, second = merge([observation(value=2.)])
    assert first != second


def test_manifest_changes_when_a_base_feature_changes():
    _, first = merge(canonical=base(ret_1d=.01))
    _, second = merge(canonical=base(ret_1d=.02))
    assert first != second


def test_manifest_changes_when_an_unmatched_observation_changes():
    _, first = merge([observation(symbol='خارج', value=1.)])
    _, second = merge([observation(symbol='خارج', value=2.)])
    assert first != second


@pytest.mark.parametrize('observed,limit,status', [
    ('2024-01-02T08:59:00+03:30', 60, 'eligible'),
    ('2024-01-02T08:58:59.999999999+03:30', 60, 'stale_observation'),
    ('2024-01-02T08:00:00+03:30', 60, 'stale_observation'),
    ('2024-01-02T09:00:00+03:30', 0, 'eligible'),
    ('2024-01-02T08:59:59.999999999+03:30', 0, 'stale_observation'),
])
def test_declared_freshness_uses_observation_not_a_late_receipt(observed, limit, status):
    out, audit = merge([observation(observed_at=observed, available_at='2024-01-02T09:00:00+03:30')],
                       max_age_seconds={'metric':limit})
    assert out.loc[77, 'test__metric__status'] == status
    assert pd.isna(out.loc[77, 'test__metric']) == (status != 'eligible')
    assert audit['max_age_seconds'] == {'metric':limit}
    assert not audit['accepted_for_signals']


@pytest.mark.parametrize('limits', [{}, {'other':60}, {'metric':60, 'extra':60}, [],
    {'metric':True}, {'metric':1.5}, {'metric':-1}, {'metric':'60'}, {'metric':float('inf')}, {'metric':2**40}])
def test_freshness_policy_is_explicit_for_the_whole_declared_family(limits):
    with pytest.raises(ValueError, match='حد تازگی'):
        merge(max_age_seconds=limits)


def test_unknown_freshness_limit_is_explicit_and_not_an_implicit_seconds_feed():
    rows = [observation(observed_at='2024-01-02T08:00:00+03:30')]
    out, audit = merge(rows)
    assert out.loc[77, 'test__metric__status'] == 'eligible'  # Timing contract only.
    assert audit['max_age_seconds'] == {'metric':None}
    assert not audit['native_semantics_independently_verified']


def test_freshness_limits_are_specific_to_each_feature():
    rows = [observation(observed_at='2024-01-02T08:00:00+03:30'),
            observation(feature='context', observed_at='2024-01-02T08:00:00+03:30')]
    out, audit = merge(rows, feature_units={'metric':'Rial','context':'Rial'},
        max_age_seconds={'metric':60,'context':None})
    assert out.loc[77, 'test__metric__status'] == 'stale_observation'
    assert out.loc[77, 'test__context__status'] == 'eligible'
    assert audit['status_counts'] == {'stale_observation':1,'eligible':1}


def test_manifest_binds_values_without_rounding_away_one_float_step():
    _, first = merge([observation(value=1.)])
    _, second = merge([observation(value=np.nextafter(1., 2.))])
    assert first['normalized_observations_sha256'] != second['normalized_observations_sha256']
    assert first['output_panel_sha256'] != second['output_panel_sha256']
    assert first['normalized_base_sha256'] == second['normalized_base_sha256']


def test_fingerprints_are_repeatable_and_preserve_nondefault_index():
    b = base()
    b.index = pd.MultiIndex.from_tuples([('sample', 77)], names=['case','row'])
    out, first = merge(canonical=b)
    _, second = merge(canonical=b)
    assert first == second and out.index.equals(b.index)
    assert first['version'] == 'research-extension-panel-v2'


def test_unserializable_objects_fail_closed_instead_of_an_unbound_manifest():
    with pytest.raises(ValueError, match='اثر انگشت'):
        merge([observation(value=object())])


def test_sealed_boundary_is_checked_before_cell_fingerprints():
    class NeverInspect:
        def __repr__(self):
            raise AssertionError('sealed value inspected')
    with pytest.raises(ValueError, match='مرز'):
        merge([observation(date='2026-10-20', symbol='خارج', value=NeverInspect())])


def test_missing_and_nonfinite_cells_have_json_safe_fingerprints():
    _, audit = merge([observation(value=float('nan'))], canonical=base(ret_1d=float('inf')))
    assert audit['status_counts'] == {'unknown_or_invalid_number':1}
    json.dumps(audit, allow_nan=False)
