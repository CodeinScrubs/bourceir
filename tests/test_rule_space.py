"""A batch accelerator must preserve logic, missingness and the trial denominator."""

import itertools

import numpy as np
import pandas as pd
import pytest

from research.rule_space import Predicate, RuleMatrix, canonical_rule, conjunction_budget, rule_id


def features():
    return pd.DataFrame(dict(date=['2024-01-01']*4,symbol=['a','b','c','d'],
        value_ratio_20d=[1.,1.4,1.7,np.nan],ret_5d=[-.1,np.nan,.1,.2]))


def named(name,*rules):
    return dict(name=name,predicates=list(rules))


def test_redundant_thresholds_and_aliases_share_one_evaluation():
    weak,strong=Predicate('value_ratio_20d','>=',1.3),Predicate('value_ratio_20d','>=',1.6)
    assert canonical_rule([weak,strong])==(strong,)
    assert rule_id([weak,strong])==rule_id([strong,strong])
    matrix=RuleMatrix(features(),[named('both',weak,strong),named('strong',strong)])
    assert matrix.admission['nominal_named_rules']==2 and matrix.admission['logical_unique_rules']==1
    summary=matrix.summaries()[0]
    assert (summary['true_rows'],summary['false_rows'],summary['unknown_rows'])==(1,2,1)
    assert summary['aliases']==['both','strong']


def test_missing_values_are_three_valued_and_and_false_dominates_unknown():
    rule=[Predicate('value_ratio_20d','>=',1.3),Predicate('ret_5d','>',0)]
    _,values=next(RuleMatrix(features(),[named('r',*rule)]).batches())
    assert values[:,0].tolist()==[0,-1,1,-1]
    missing=RuleMatrix(features(),[named('missing',Predicate('buyer_power','>',1.2))])
    assert missing.summaries()[0]['unknown_rows']==4


def test_distinct_rules_with_coincident_training_masks_are_not_merged():
    a,b=Predicate('ret_5d','>',0),Predicate('ret_5d','>',.01)
    matrix=RuleMatrix(features(),[named('one',a),named('two',b)])
    assert matrix.admission['logical_unique_rules']==2
    _,values=next(matrix.batches())
    np.testing.assert_array_equal(values[:,0],values[:,1])


def test_canonicalization_matches_exhaustive_three_valued_truth():
    atoms=[Predicate('ret_5d',op,point) for op in ('>','>=','<','<=','==') for point in (-.1,0.,.1)]
    df=pd.DataFrame(dict(date=['2024-01-01']*7,symbol=list('abcdefg'),ret_5d=[-.2,-.1,-.01,0.,.1,.2,np.nan]))
    comparisons={'>':np.greater,'>=':np.greater_equal,'<':np.less,'<=':np.less_equal,'==':np.equal}
    for pair in itertools.combinations(atoms,2):
        canonical=canonical_rule(pair)
        _,values=next(RuleMatrix(df,[named('rule',*canonical)],batch_size=1).batches())
        raw=df.ret_5d.to_numpy()
        expected=np.full(len(raw),-1,dtype=np.int8)
        known=np.isfinite(raw)
        expected[known]=np.logical_and.reduce([comparisons[p.operator](raw[known],p.threshold) for p in pair]).astype(np.int8)
        np.testing.assert_array_equal(values[:,0],expected)


def test_batches_preserve_the_named_denominator_and_fit_declared_shape():
    specs=[named(str(i),Predicate('ret_5d','>',i/100)) for i in range(7)]
    matrix=RuleMatrix(features(),specs,batch_size=2)
    batches=list(matrix.batches())
    assert [values.shape for _,values in batches]==[(4,2),(4,2),(4,2),(4,1)]
    assert sum(len(names) for names,_ in batches)==7
    assert all(r['true_rows']+r['false_rows']+r['unknown_rows']==4 for r in matrix.summaries())
    assert not matrix.admission['performance_evaluated']


@pytest.mark.parametrize('field',['adj_close','net_ret_2d','label','tomorrow_close','untrusted_python()'])
def test_outcomes_adjusted_prices_and_arbitrary_expressions_are_not_predicates(field):
    with pytest.raises(ValueError,match='allowlisted'):
        Predicate(field,'>',1.)


@pytest.mark.parametrize('threshold',[True,None,np.nan,np.inf,'1.2'])
def test_thresholds_require_known_finite_numbers(threshold):
    with pytest.raises(ValueError):
        Predicate('ret_5d','>',threshold)


def test_memory_and_combinatorial_budgets_reject_before_large_work():
    with pytest.raises(ValueError,match='before allocation'):
        RuleMatrix(features(),[named('one',Predicate('ret_5d','>',0))],max_bytes=1)
    with pytest.raises(ValueError,match='before materialization'):
        conjunction_budget(43,min_terms=2,max_terms=6,max_candidates=10000)
    assert conjunction_budget(4,min_terms=1,max_terms=2)['nominal_candidates']==10


def test_empty_scopes_duplicate_keys_unknown_columns_and_holdout_are_rejected():
    spec=[named('one',Predicate('ret_5d','>',0))]
    for frame in (features().iloc[:0],pd.concat([features(),features()]),
                  features().assign(label=1),features().assign(date='2026-01-01')):
        with pytest.raises(ValueError):
            RuleMatrix(frame,spec)


def test_rule_order_and_input_order_do_not_change_matching_symbol_keys():
    rules=[Predicate('ret_5d','>',0),Predicate('value_ratio_20d','>=',1.3)]
    a=RuleMatrix(features(),[named('one',*rules)])
    b=RuleMatrix(features().iloc[::-1],[named('renamed',*reversed(rules))])
    pd.testing.assert_frame_equal(a.keys,b.keys)
    assert list(a.rules)==list(b.rules)
    np.testing.assert_array_equal(next(a.batches())[1],next(b.batches())[1])
