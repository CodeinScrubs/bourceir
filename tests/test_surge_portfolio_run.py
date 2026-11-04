"""Temporary, synthetic archives only; no production data or sealed prices."""

import json
import shutil

import pandas as pd
import pytest

from config import settings
from ops.research_review import sha256
from research.surge_patterns import build_pre_move_features, pattern_masks
from research.surge_portfolio import fixed_contract
from research.surge_portfolio_run import _comparisons, _selection_inputs, _source_inputs, review_study, run_study
from tests.test_portfolio import history


def _write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False),encoding='utf-8')


def source_archives(db,tmp_path):
    prices,days = history(180,('a','b'))
    census = tmp_path/'EXP-2026-10-20-RAREEVENT-00000000000000000000000000000000'
    pattern = tmp_path/'EXP-2026-10-20-SURGEPATTERN-00000000000000000000000000000000'
    census.mkdir()
    pattern.mkdir()
    source = settings.BASE_DIR/'research/surge_patterns.py'
    for folder in (census,pattern):
        (folder/'code').mkdir()
        shutil.copyfile(source,folder/'code'/'source.py')
    code = {'source.py':sha256(census/'code'/'source.py')}
    cohort = prices.loc[prices.date.isin(days[::20]),['symbol','date']].copy()
    for name,frame in (('prices',prices),('cohort',cohort),('outcomes',pd.DataFrame({'date':[]}))):
        frame.to_parquet(census/(name+'.parquet'),index=False)
    parent_hashes = {p.name:sha256(p) for p in census.glob('*.parquet')}
    cp = dict(version='rare-event-cohort-v1',holdout_start=settings.RESEARCH_CONFIG['holdout_start'],
        observed_through=days[-1],horizons=[10,20,60],threshold=2.,anchor_stride=20,
        holdout_opened=False,accepted_for_signals=False,profitability_proven=False,
        source_code=code,prices_sha256=parent_hashes['prices.parquet'],cohort_sha256=parent_hashes['cohort.parquet'],
        calendar=days.tolist(),source_rows=len(prices),opportunities=len(cohort),development_start=days[0])
    parent = dict(experiment_id=census.name,protocol=cp,summary={},artifacts_sha256=parent_hashes)
    _write(census/'protocol.json',cp)
    _write(census/'result.json',parent)
    features = build_pre_move_features(prices,cohort,days)
    flags = pd.concat([features[['date','symbol']],pattern_masks(features)],axis=1)
    for name,frame in (('features',features),('flags',flags),('executions',pd.DataFrame({'date':[]}))):
        frame.to_parquet(pattern/(name+'.parquet'),index=False)
    pp = dict(version='surge-pattern-diagnostics-v1',hypotheses=fixed_contract(1.)['hypotheses'],
        horizons=[20,60],thresholds_optimized=False,models_fitted=0,holdout_opened=False,accepted_for_signals=False,
        source_files_sha256=code,parent_path=str(census),parent_experiment_id=census.name,
        parent_artifacts_sha256=parent_hashes,parent_prices_sha256=parent_hashes['prices.parquet'],
        calendar=days.tolist(),folds=[dict(test_start=days[60],test_end_exclusive=days[120]),
                                    dict(test_start=days[120],test_end_exclusive=days[-1])])
    result = dict(experiment_id=pattern.name,protocol=pp,summary={},
        artifact_sha256={p.name:sha256(p) for p in pattern.glob('*.parquet')},
        accepted_for_signals=False,profitability_proven=False)
    _write(pattern/'protocol.json',pp)
    _write(pattern/'result.json',result)
    for identifier,protocol,registered_status in ((census.name,cp,'completed'),(pattern.name,pp,'registered')):
        for suffix,status in (('-PROTOCOL',registered_status),('','completed')):
            assert db.log_experiment(experiment_id=identifier+suffix,strategy_name='test source',
                symbols_tested='synthetic test only',parameters_json=json.dumps(protocol),metrics_json='{}',status=status)
    return pattern,census


def test_full_registered_family_reconciles_and_replays_without_loading_outcome_labels(db,tmp_path,monkeypatch):
    pattern,census = source_archives(db,tmp_path)
    import research.surge_portfolio_run as runner
    reader = runner.read_development_parquet
    calls,prepared_inputs = [],[]
    replay = runner.replay_portfolio
    def cached_replay(prices,*args,**kwargs):
        from backtest.prepared import PreparedPortfolioPrices
        assert isinstance(prices,PreparedPortfolioPrices)
        prepared_inputs.append(prices)
        return replay(prices,*args,**kwargs)
    monkeypatch.setattr(runner,'replay_portfolio',cached_replay)
    def guarded(path,*args,**kwargs):
        calls.append(path.name)
        assert path.name not in ('outcomes.parquet','executions.parquet')
        return reader(path,*args,**kwargs)
    monkeypatch.setattr(runner,'read_development_parquet',guarded)
    result,folder = run_study(db,pattern=pattern,pattern_db=db,census_db=db,root=tmp_path/'studies',capital_rial=10000,
        feature_policy='rebuild-raw-pit-v1')
    monkeypatch.setattr(runner,'read_development_parquet',reader)
    assert len(result['records'])==164 and len(result['comparisons'])==200
    assert not result['accepted_for_signals'] and not result['profitability_proven']
    assert result['protocol']['source_outcome_labels_loaded'] is False
    assert result['feature_proof']['state']['policy']=='rebuild-raw-pit-v1'
    assert result['feature_proof']['financial_modes_started']==0
    assert db.conn.execute('SELECT status FROM experiments WHERE experiment_id=?',
        (result['experiment_id']+'-FEATURES',)).fetchone()[0]=='registered'
    assert not any(n=='outcomes.parquet' for n in calls)
    frozen = json.loads((folder/'selections-frozen.json').read_text(encoding='utf-8'))
    assert frozen['profit_modes_started']==0 and len(frozen['picks_sha256'])==164
    assert len(prepared_inputs)==164 and len({id(p) for p in prepared_inputs})==1
    proof = json.loads((folder/'prepared-prices.json').read_text(encoding='utf-8'))
    assert proof['rows']==360 and proof['financial_modes_started']==0
    assert db.conn.execute('SELECT status FROM experiments WHERE experiment_id=?',
        (result['experiment_id']+'-PRICES',)).fetchone()[0]=='registered'
    review = review_study(db,folder,replay_names=('liquidity-h20-normal','value_surge-h60-stress'))
    assert review['financial_modes_reconciled']==164 and len(review['financial_modes_reexecuted'])==2
    assert review['all_features_and_flags_recomputed'] and not review['economic_profit_verified']
    assert len(prepared_inputs)==166 and prepared_inputs[-1] is prepared_inputs[-2]
    assert prepared_inputs[-1] is not prepared_inputs[0]
    result['records']['liquidity-h20-normal']['audit']['audited_model_total_return'] = 10.
    _write(folder/'result.json',result)
    with pytest.raises(ValueError,match='registration'):
        review_study(db,folder)


def test_default_feature_policy_rejects_even_small_unregistered_float_drift(db,tmp_path):
    pattern,_ = source_archives(db,tmp_path)
    source = _source_inputs(pattern,db,db)
    frames = source['frames']
    frames['features'].loc[3,'ret_1'] += 1e-12
    with pytest.raises(AssertionError):
        _selection_inputs(frames,source['calendar'])


def test_explicit_rebuild_preserves_raw_and_original_features_and_counts_known_unknown_changes(db,tmp_path):
    pattern,_ = source_archives(db,tmp_path)
    source = _source_inputs(pattern,db,db)
    frames = source['frames']
    saved_hash = sha256(pattern/'features.parquet')
    raw = frames['prices'].copy(deep=True)
    frames['features'].loc[3,'ret_1'] = float('nan')
    frames['flags'].loc[3,'value_surge'] = True
    state = _selection_inputs(frames,source['calendar'],'rebuild-raw-pit-v1')
    assert state['feature_changes']['changed_rows']==1
    assert state['feature_changes']['became_known']['ret_1']==1
    assert state['flag_changes']['changed_rows']==1
    assert sha256(pattern/'features.parquet')==saved_hash
    pd.testing.assert_frame_equal(raw,frames['prices'])


def test_failed_derived_feature_registration_prevents_selection_and_new_pnl(db,tmp_path,monkeypatch):
    pattern,_ = source_archives(db,tmp_path)
    log = db.log_experiment
    def fail_derived(**kwargs):
        return None if kwargs['experiment_id'].endswith('-FEATURES') else log(**kwargs)
    monkeypatch.setattr(db,'log_experiment',fail_derived)
    with pytest.raises(RuntimeError,match='registration failed'):
        run_study(db,pattern=pattern,pattern_db=db,census_db=db,root=tmp_path/'studies',capital_rial=10000,
            feature_policy='rebuild-raw-pit-v1')
    assert not list((tmp_path/'studies').rglob('*-picks.parquet'))
    assert not list((tmp_path/'studies').rglob('*-equity.parquet'))


def test_failed_registration_prevents_feature_computation_and_new_pnl(db,tmp_path,monkeypatch):
    pattern,_ = source_archives(db,tmp_path)
    import research.surge_portfolio_run as runner
    monkeypatch.setattr(db,'log_experiment',lambda **kwargs:None)
    monkeypatch.setattr(runner,'build_pre_move_features',lambda *a,**k:pytest.fail('unregistered computation'))
    with pytest.raises(RuntimeError,match='registration failed'):
        run_study(db,pattern=pattern,pattern_db=db,census_db=db,root=tmp_path/'studies',capital_rial=10000)
    assert not list((tmp_path/'studies').rglob('*-equity.parquet'))


def test_failed_price_preparation_registration_prevents_every_financial_replay(db,tmp_path,monkeypatch):
    pattern,_ = source_archives(db,tmp_path)
    import research.surge_portfolio_run as runner
    log = db.log_experiment
    def fail_prepared(**kwargs):
        return None if kwargs['experiment_id'].endswith('-PRICES') else log(**kwargs)
    monkeypatch.setattr(db,'log_experiment',fail_prepared)
    monkeypatch.setattr(runner,'replay_portfolio',lambda *a,**k:pytest.fail('unregistered financial replay'))
    with pytest.raises(RuntimeError,match='registration failed'):
        run_study(db,pattern=pattern,pattern_db=db,census_db=db,root=tmp_path/'studies',capital_rial=10000,
            feature_policy='rebuild-raw-pit-v1')
    assert len(list((tmp_path/'studies').rglob('*-picks.parquet')))==164
    assert not list((tmp_path/'studies').rglob('*-equity.parquet'))


@pytest.mark.parametrize('change',['feature_bytes','independent_registry','holdout','noncontiguous_folds'])
def test_source_claims_must_match_original_registry_and_frozen_bytes(db,tmp_path,change):
    pattern,census = source_archives(db,tmp_path)
    if change=='feature_bytes':
        with (pattern/'features.parquet').open('ab') as stream:
            stream.write(b'tamper')
    elif change=='independent_registry':
        db.conn.execute('DELETE FROM experiments WHERE experiment_id=?',(pattern.name,))
        db.conn.commit()
    else:
        target = census if change=='holdout' else pattern
        record = json.loads((target/'result.json').read_text(encoding='utf-8'))
        if change=='holdout':
            record['protocol']['observed_through'] = settings.RESEARCH_CONFIG['holdout_start']
        else:
            record['protocol']['folds'][1]['test_start'] = '2024-04-29'
        _write(target/'protocol.json',record['protocol'])
        _write(target/'result.json',record)
    with pytest.raises(ValueError):
        _source_inputs(pattern,db,db)


def test_account_db_and_source_archive_output_are_rejected(db,tmp_path,monkeypatch):
    pattern,_ = source_archives(db,tmp_path)
    with pytest.raises(ValueError,match='Preserve original'):
        run_study(db,pattern=pattern,pattern_db=db,census_db=db,root=pattern/'new',capital_rial=10000)
    monkeypatch.setattr(db,'db_path',settings.DB_PATH)
    with pytest.raises(ValueError,match='separate research DB'):
        run_study(db,pattern=pattern,pattern_db=db,census_db=db,root=tmp_path/'studies',capital_rial=10000)


def test_financially_rejected_model_does_not_get_an_alpha_test_or_disappear_from_family(monkeypatch):
    import research.surge_portfolio_run as runner
    p = fixed_contract(10000)
    p.update(capital_rial=10000,replications=20000)
    spec = next(m for m in p['modes'] if m['name']=='momentum-h20-normal')
    p['modes'] = [spec]
    names = ['momentum-h20-normal','liquidity-h20-normal']+[
        f'momentum-h20-random{s}-normal' for s in (41,97,173)]
    records = {n:{'audit':dict(audited_model_total_return=-.01,buys=1,computational_uncertainty_blockers=[])} for n in names}
    monkeypatch.setattr(runner,'compare_equity_curves',lambda *a,**k:pytest.fail('rejected model alpha test'))
    comparisons = _comparisons(records,{},p,['2024-01-01'])
    assert len(comparisons)==5 and p['within_run_comparison_family']==600
    assert all(c['diagnostic'] is None and 'nonpositive_candidate_model_return' in c['diagnostic_blockers']
               and not c['accepted_for_signals'] for c in comparisons)
