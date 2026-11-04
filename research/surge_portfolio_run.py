"""Register/replay the fixed surge family with continuous shared cash before the holdout."""

import argparse
import json
import shutil
import uuid
from pathlib import Path

import pandas as pd

from backtest.portfolio import ENTRY_ALLOCATION_VERSION, PORTFOLIO_VERSION, RISK_EXIT_VERSION, replay_portfolio
from backtest.prepared import PRICE_SNAPSHOT_VERSION, prepare_portfolio_prices
from config import settings
from core.timeutils import now_tehran_iso
from database.db import DatabaseManager
from ops.research_review import _load_json, _relative_file, _verify_inventory, read_development_parquet, sha256
from research.run import _json_safe, archive_research_code, research_code_fingerprint
from research.surge_patterns import build_pre_move_features, pattern_masks
from research.surge_portfolio import (
    ALLOCATION,
    BLOCKS,
    COMPARISON_FAMILY,
    VERSION,
    WITHIN_RUN_CONFIGURATIONS,
    fixed_contract,
    mode_specs,
    portfolio_config,
    ranked_decisions,
    relevant_prices,
    summarize_portfolio,
)
from research.uncertainty import compare_equity_curves

FEATURE_POLICIES = ('verify-frozen-v1','rebuild-raw-pit-v1')


def _write(path,value):
    path.write_text(json.dumps(_json_safe(value),ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def _registry(db,identifier,protocol,metrics,status):
    row = db.conn.execute('SELECT parameters_json,metrics_json,status FROM experiments WHERE experiment_id=?',
                          (identifier,)).fetchone()
    if (row is None or row['status']!=status or _load_json_text(row['parameters_json'])!=protocol
            or _load_json_text(row['metrics_json'])!=_json_safe(metrics)):
        raise ValueError('Independent experiment registration differs: '+identifier)


def _load_json_text(value):
    def unique(pairs):
        result = {}
        for key,item in pairs:
            if key in result:
                raise ValueError('Duplicate key in independent experiment registry')
            result[key] = item
        return result

    def invalid(value):
        raise ValueError('Nonfinite constant in independent registry: '+value)

    return json.loads(value,object_pairs_hook=unique,parse_constant=invalid)


def _register(db,identifier,protocol,metrics,status):
    result = db.log_experiment(experiment_id=identifier,strategy_name=VERSION,
        symbols_tested='entire frozen development census; no recent winner selection',
        parameters_json=json.dumps(_json_safe(protocol),ensure_ascii=False,allow_nan=False),
        metrics_json=json.dumps(_json_safe(metrics),ensure_ascii=False,allow_nan=False),status=status,
        start_date=protocol['start'],end_date=protocol['end_exclusive'],
        dataset_version=protocol['inputs_sha256']['prices.parquet'],features_version='frozen-pre-move-raw-PIT',
        model_version=VERSION)
    if result is None:
        raise RuntimeError('Shared-capital study registration failed')
    _registry(db,identifier,protocol,metrics,status)


def _source_inputs(pattern,pattern_db,census_db):
    """Validate original registrations and bytes; never load future outcome labels."""
    pattern = Path(pattern).resolve()
    result,protocol = _load_json(pattern/'result.json'),_load_json(pattern/'protocol.json')
    if (result['protocol']!=protocol or pattern.name!=result['experiment_id']
            or protocol['version']!='surge-pattern-diagnostics-v1'
            or protocol['hypotheses']!=fixed_contract(1.)['hypotheses']
            or protocol['horizons']!=[20,60] or protocol['thresholds_optimized'] is not False
            or protocol['models_fitted']!=0
            or any(protocol.get(k) is not False for k in ('holdout_opened','accepted_for_signals'))
            or any(result.get(k) is not False for k in ('accepted_for_signals','profitability_proven'))):
        raise ValueError('A fixed development-only pattern archive is required')
    _verify_inventory(pattern/'code',protocol['source_files_sha256'])
    _verify_inventory(pattern,result['artifact_sha256'],parquet=True)
    _registry(pattern_db,result['experiment_id']+'-PROTOCOL',protocol,{},'registered')
    _registry(pattern_db,result['experiment_id'],protocol,result['summary'],'completed')
    census = Path(protocol['parent_path']).resolve()
    parent,p = _load_json(census/'result.json'),_load_json(census/'protocol.json')
    cutoff = settings.RESEARCH_CONFIG['holdout_start']
    if (parent['protocol']!=p or census.name!=parent['experiment_id']
            or p['version']!='rare-event-cohort-v1' or p['holdout_start']!=cutoff
            or p['observed_through']>=cutoff or p['horizons']!=[10,20,60]
            or p['threshold']!=2. or p['anchor_stride']!=20
            or any(p.get(k) is not False for k in ('holdout_opened','accepted_for_signals','profitability_proven'))
            or protocol['parent_experiment_id']!=parent['experiment_id']
            or protocol['parent_artifacts_sha256']!=parent['artifacts_sha256']
            or protocol['calendar']!=p['calendar'] or protocol['parent_prices_sha256']!=p['prices_sha256']):
        raise ValueError('The original sealed development census differs')
    _verify_inventory(census/'code',p['source_code'])
    # Only price/cohort bytes are needed. The frozen outcome artifact is checked
    # by hash but never deserialized or supplied to selection or position sizing.
    _verify_inventory(census,parent['artifacts_sha256'],parquet=True)
    for suffix,metrics in (('-PROTOCOL',{}),('',parent['summary'])):
        _registry(census_db,parent['experiment_id']+suffix,p,metrics,'completed')
    paths = {name:census/name for name in ('prices.parquet','cohort.parquet')}
    paths.update({name:pattern/name for name in ('features.parquet','flags.parquet')})
    hashes = {name:(parent['artifacts_sha256'] if name in ('prices.parquet','cohort.parquet')
                    else result['artifact_sha256'])[name] for name in paths}
    frames = {name[:-8]:read_development_parquet(path,hashes[name],cutoff) for name,path in paths.items()}
    if (len(frames['prices'])!=p['source_rows'] or len(frames['cohort'])!=p['opportunities']
            or hashes['prices.parquet']!=p['prices_sha256'] or hashes['cohort.parquet']!=p['cohort_sha256']):
        raise ValueError('Frozen source denominator or price version differs')
    folds = protocol['folds']
    if not folds or any(a['test_end_exclusive']!=b['test_start'] for a,b in zip(folds,folds[1:])):
        raise ValueError('Contiguous previously declared test intervals are required')
    start,end = folds[0]['test_start'],folds[-1]['test_end_exclusive']
    if not p['development_start']<=start<end<=cutoff or end not in p['calendar']:
        raise ValueError('Only the previous development test span can be replayed')
    return dict(pattern=pattern,census=census,pattern_record=result,census_record=parent,
        paths=paths,inputs_sha256=hashes,frames=frames,calendar=p['calendar'],start=start,end=end)


def _changes(old,new):
    if list(old)!=list(new) or not old[['date','symbol']].equals(new[['date','symbol']]):
        raise ValueError('A feature rebuild must preserve every source census key and column')
    columns = [c for c in old if c not in ('date','symbol')]
    changed = ~(old[columns].eq(new[columns]) | (old[columns].isna() & new[columns].isna())).fillna(False)
    return dict(changed_rows=int(changed.any(axis=1).sum()),
        changed_cells={c:int(changed[c].sum()) for c in columns},
        became_unknown={c:int((old[c].notna() & new[c].isna()).sum()) for c in columns},
        became_known={c:int((old[c].isna() & new[c].notna()).sum()) for c in columns})


def _selection_inputs(frames,calendar,feature_policy='verify-frozen-v1'):
    if feature_policy not in FEATURE_POLICIES:
        raise ValueError('An explicit registered feature-source policy is required')
    features = build_pre_move_features(frames['prices'],frames['cohort'],calendar)
    flags = pd.concat([features[['date','symbol']],pattern_masks(features)],axis=1)
    state = dict(policy=feature_policy,source_inputs_preserved=True,
        feature_changes=_changes(frames['features'],features),flag_changes=_changes(frames['flags'],flags))
    if feature_policy=='verify-frozen-v1':
        pd.testing.assert_frame_equal(features,frames['features'],check_exact=True)
        pd.testing.assert_frame_equal(flags,frames['flags'],check_exact=True)
    # The original inputs on disk are NEVER replaced. Rebuilt artifacts and
    # hashes get their own registration before any selection or financial mode.
    frames['features'],frames['flags'] = features,flags
    # Selection needs only current anchor-day values, not millions of daily bars.
    # This is a computational projection of the same frozen raw source.
    frames['decision_prices'] = frames['prices'].loc[
        frames['prices'].date.isin(features.date.unique()),['date','symbol','value']].copy()
    return state


def _picks(frames,protocol,spec):
    return ranked_decisions(frames['decision_prices'],frames['features'],frames['flags'],protocol['calendar'],
        start=protocol['start'],end=protocol['end_exclusive'],horizon=spec['horizon'],
        hypothesis=spec['hypothesis'],random_seed=spec['random_seed'])


def _comparisons(records,curves,protocol,days):
    output = []
    cash = pd.DataFrame(dict(date=days,equity_rial=protocol['capital_rial']))
    for spec in protocol['modes']:
        if spec['hypothesis'] is None or spec['random_seed'] is not None:
            continue
        name,h = spec['name'],spec['horizon']
        scenario = 'stress' if spec['stress'] else 'normal'
        benchmarks = ['cash',f'liquidity-h{h}-{scenario}']+[
            f'{spec["hypothesis"]}-h{h}-random{s}-{scenario}' for s in protocol['random_seeds']]
        for benchmark in benchmarks:
            blockers = list(records[name]['audit']['computational_uncertainty_blockers'])
            if benchmark!='cash':
                blockers.extend('benchmark_'+b for b in records[benchmark]['audit']['computational_uncertainty_blockers'])
            if records[name]['audit']['buys']==0:
                blockers.append('no_candidate_executions')
            a = records[name]['audit']['audited_model_total_return']
            b = 0. if benchmark=='cash' else records[benchmark]['audit']['audited_model_total_return']
            # This financial rejection gate is fixed before production replay.
            # All endpoints and all 600 comparisons remain in the family; an
            # already nonpositive model does not need an expensive alpha test.
            if a is not None and a<=0:
                blockers.append('nonpositive_candidate_model_return')
            if a is not None and b is not None and a<=b:
                blockers.append('nonpositive_comparative_model_edge')
            diagnostic = None
            if not blockers:
                diagnostic = compare_equity_curves(curves[name],cash if benchmark=='cash' else curves[benchmark],
                    initial_capital_rial=protocol['capital_rial'],block_lengths=BLOCKS,
                    replications=protocol['replications'],seed=41,min_sessions=252,
                    trials_attempted=WITHIN_RUN_CONFIGURATIONS,expected_sessions=days,
                    family_comparisons=COMPARISON_FAMILY)
            output.append(dict(candidate=name,benchmark=benchmark,
                endpoint_model_return_difference=None if a is None or b is None else a-b,
                diagnostic_blockers=sorted(set(blockers)),diagnostic=diagnostic,
                entire_project_search_adjusted=False,comparable_economic_risk_verified=False,
                probability_of_future_profit=None,accepted_for_signals=False))
    return output


def _check_code(protocol,folder):
    if research_code_fingerprint()!=protocol['code_sha256']:
        raise ValueError('Research source changed during financial computation; preserve the interrupted archive')
    _verify_inventory(folder/'code',protocol['source_files'])
    for name,digest in protocol['source_files'].items():
        if settings.BASE_DIR.joinpath(name).read_bytes().replace(b'\r\n',b'\n')!=(folder/'code'/name).read_bytes():
            raise ValueError('Current source or locked dependency differs: '+name)


def _assert_frame(actual,saved):
    # An empty no-column execution/decision ledger round-trips through Parquet
    # with a different empty column-index dtype. No observed values are coerced.
    if actual.shape==(0,0) and saved.shape==(0,0):
        return
    pd.testing.assert_frame_equal(actual,saved)


def _price_proof(prepared,protocol):
    return dict(version=prepared.version,frame_fingerprint=prepared.frame_fingerprint,
        rows=prepared.row_count,sessions=len(prepared.sessions),
        source_prices_sha256=protocol['inputs_sha256']['prices.parquet'],
        independent_source_authenticity_verified=False,financial_modes_started=0)


def run_study(db,*,pattern,pattern_db,census_db,root,capital_rial,replications=20000,
              feature_policy='verify-frozen-v1'):
    if Path(db.db_path).resolve()==settings.DB_PATH.resolve():
        raise ValueError('Use a separate research DB, never the account DB')
    if type(replications) is not int or not 20000<=replications<=100000:
        raise ValueError('At least 20000 bootstrap draws are required for the declared family')
    if feature_policy not in FEATURE_POLICIES:
        raise ValueError('Unknown feature-source policy; never silently mix versions')
    contract = fixed_contract(capital_rial)
    source = _source_inputs(pattern,pattern_db,census_db)
    root = Path(root).resolve()
    if any(root==p or root.is_relative_to(p) for p in (source['pattern'],source['census'])):
        raise ValueError('Preserve original source archives; use a separate output root')
    identifier = f'EXP-{now_tehran_iso()[:10]}-SURGECAPITAL-{uuid.uuid4().hex}'
    folder = root/identifier
    folder.mkdir(parents=True,exist_ok=False)
    protocol = dict(**contract,registered_at=now_tehran_iso(),capital_rial=capital_rial,
        start=source['start'],end_exclusive=source['end'],calendar=source['calendar'],
        holdout_start=settings.RESEARCH_CONFIG['holdout_start'],feature_policy=feature_policy,
        inputs_sha256=source['inputs_sha256'],source_pattern_id=source['pattern_record']['experiment_id'],
        source_census_id=source['census_record']['experiment_id'],replications=replications,
        source_files=archive_research_code(folder/'code'),code_sha256=research_code_fingerprint(),
        portfolio_version=PORTFOLIO_VERSION,risk_exit_version=RISK_EXIT_VERSION,
        entry_allocation_version=ENTRY_ALLOCATION_VERSION,fees=dict(settings.FEES),
        price_preparation_version=PRICE_SNAPSHOT_VERSION,
        capital_is_research_scenario_not_broker_balance=True,
        source_outcome_labels_loaded=False,past_development_previously_exposed=True,
        independent_future_test=False,holdout_eligible=False,
        continuous_shared_cash=True,annual_or_fold_cash_reset=False,
        rank='decision-day traded value descending; normalized symbol tie break',
        selection='all ranked true flags; no top-five truncation or hindsight fill replacement',
        controls='same-date same-count random candidates among known flags/current valid bars; same liquidity rank',
        last_decision_policy='nominal decision+H must precede interval end; unresolved delayed exits stay open',
        entry_budget_is_hard_ongoing_weight=False,allocation_drift_rebalanced=False,
        loss_budget_is_guaranteed=False,risk_exit='observed EOD breach; request exit next session, locks/capacity apply',
        uncertainty_gate='positive endpoint and comparative edge; full fresh curve, no synthetic action, open position or noncash claim; never drop a bad day',
        historical_membership_verified=False,historical_fee_schedule_verified=False,
        actual_fills_verified=False,actual_corporate_entitlements_verified=False,
        cash_yield=0.,cash_yield_is_assumption=True,nominal_annual_target=.30,
        annual_target_is_isolated_sleeve_success_claim=False)
    _write(folder/'protocol.json',protocol)
    _register(db,identifier+'-PROTOCOL',protocol,{},'registered')
    artifacts = {}
    for name,path in source['paths'].items():
        shutil.copyfile(path,folder/name)
        if sha256(folder/name)!=protocol['inputs_sha256'][name]:
            raise ValueError('Source input changed during freezing')
        artifacts[name] = sha256(folder/name)
    for name,record in (('source-pattern-result.json',source['pattern_record']),('source-census-result.json',source['census_record'])):
        _write(folder/name,record)
        artifacts[name] = sha256(folder/name)
    frames = {name[:-8]:read_development_parquet(folder/name,digest,settings.RESEARCH_CONFIG['holdout_start'])
              for name,digest in protocol['inputs_sha256'].items()}
    feature_state = _selection_inputs(frames,protocol['calendar'],feature_policy)
    derived_hashes = {}
    for kind in ('features','flags'):
        name = 'recomputed-'+kind+'.parquet'
        frames[kind].to_parquet(folder/name,index=False)
        derived_hashes[name] = sha256(folder/name)
        artifacts[name] = derived_hashes[name]
    feature_proof = dict(registered_at=now_tehran_iso(),state=feature_state,
        source_inputs_sha256=protocol['inputs_sha256'],derived_sha256=derived_hashes,
        source_code_sha256=protocol['code_sha256'],financial_modes_started=0)
    _register(db,identifier+'-FEATURES',protocol,feature_proof,'registered')
    _write(folder/'feature-proof.json',feature_proof)
    artifacts['feature-proof.json'] = sha256(folder/'feature-proof.json')
    picks,coverage = {},{}
    # Freeze every rule and random-control selection before ANY mode's new P&L.
    for spec in protocol['modes']:
        name = spec['name']
        selection_key = (spec['hypothesis'],spec['horizon'],spec['random_seed'])
        if selection_key not in picks:
            picks[selection_key],coverage[selection_key] = _picks(frames,protocol,spec)
        filename = name+'-picks.parquet'
        picks[selection_key].to_parquet(folder/filename,index=False)
        artifacts[filename] = sha256(folder/filename)
    _write(folder/'selections-frozen.json',dict(at=now_tehran_iso(),
        picks_sha256={k:v for k,v in artifacts.items() if k.endswith('-picks.parquet')},
        profit_modes_started=0,feature_proof_sha256=artifacts['feature-proof.json'],holdout_opened=False))
    artifacts['selections-frozen.json'] = sha256(folder/'selections-frozen.json')
    # One immutable copy of the same frozen raw source serves every mode. The
    # entire history retains preceding references; no selection or future price
    # is used to alter today's row. Per-mode raw pruning remains audit-only.
    prepared = prepare_portfolio_prices(frames['prices'])
    price_proof = _price_proof(prepared,protocol)
    _register(db,identifier+'-PRICES',protocol,price_proof,'registered')
    _write(folder/'prepared-prices.json',price_proof)
    artifacts['prepared-prices.json'] = sha256(folder/'prepared-prices.json')
    days = [d for d in protocol['calendar'] if protocol['start']<=d<protocol['end_exclusive']]
    records,curves = {},{}
    for index,spec in enumerate(protocol['modes']):
        name = spec['name']
        key = (spec['hypothesis'],spec['horizon'],spec['random_seed'])
        selected = picks[key]
        prices = relevant_prices(frames['prices'],selected,start=protocol['start'],end=protocol['end_exclusive'])
        config = portfolio_config(capital_rial,spec['horizon'],spec['stress'])
        replay = replay_portfolio(prepared,selected,protocol['calendar'],config,
            start=protocol['start'],end=protocol['end_exclusive'],
            loss_floor_exit=True,entry_allocation_fraction=ALLOCATION)
        audit,rolling = summarize_portfolio(replay,prices,days,config)
        for kind,frame in (('equity',replay.equity),('executions',replay.executions),('decisions',replay.decisions),('rolling-year',rolling)):
            filename = name+'-'+kind+'.parquet'
            frame.to_parquet(folder/filename,index=False)
            artifacts[filename] = sha256(folder/filename)
        records[name] = dict(spec=spec,coverage=coverage[key],metrics=replay.metrics,assumptions=replay.assumptions,audit=audit)
        curves[name] = replay.equity[['date','equity_rial']]
        _write(folder/'progress.json',dict(at=now_tehran_iso(),completed_modes=index+1,
            expected_modes=len(protocol['modes']),last_mode=name,last_audit=audit))
        print(json.dumps(dict(completed_modes=index+1,total_modes=len(protocol['modes']),mode=name,
            status=replay.metrics['status'],model_return=audit['audited_model_total_return'],
            buys=audit['buys'],stale_days=audit['stale_mark_days']),ensure_ascii=False),flush=True)
    comparisons = _comparisons(records,curves,protocol,days)
    _check_code(protocol,folder)
    for name,digest in artifacts.items():
        if sha256(_relative_file(folder,name))!=digest:
            raise ValueError('Frozen evidence changed during computation: '+name)
    result = dict(experiment_id=identifier,protocol=protocol,completed_at=now_tehran_iso(),
        records=records,comparisons=comparisons,artifacts_sha256=artifacts,feature_proof=feature_proof,
        accepted_for_signals=False,profitability_proven=False,economic_profit_verified=False,
        limitations=['previously seen development data; no new independent OOS evidence',
            'frozen census has incomplete historical membership and source vintages',
            'daily OHLC/volume cannot establish queue rank or actual fills',
            'corporate-action reference factors are synthetic, not verified rights or cash receipts',
            'stale held marks are retained/reported; they are not current liquidation prices',
            'within-run family does not correct the unknown entire historical search',
            '30 percent is an entry budget with reported drift, not a maintained exposure cap',
            '70 percent idle zero-yield cash is assumed; this is an isolated sleeve diagnostic',
            'positive model endpoints, annual windows or intervals cannot approve signals'])
    _register(db,identifier,protocol,records,'completed')
    _write(folder/'result.json',result)
    return result,folder


def review_study(db,folder,*,replay_names=()):
    folder = Path(folder).resolve()
    result,p = _load_json(folder/'result.json'),_load_json(folder/'protocol.json')
    contract = fixed_contract(p['capital_rial'])
    if (result['protocol']!=p or folder.name!=result['experiment_id']
            or any(p.get(k)!=v for k,v in contract.items())
            or any(result.get(k) is not False for k in ('accepted_for_signals','profitability_proven','economic_profit_verified'))
            or p.get('code_sha256')!=research_code_fingerprint()
            or p.get('fees')!=dict(settings.FEES)
            or p.get('holdout_start')!=settings.RESEARCH_CONFIG['holdout_start']
            or p.get('price_preparation_version')!=PRICE_SNAPSHOT_VERSION
            or p.get('feature_policy') not in FEATURE_POLICIES):
        raise ValueError('Frozen study contract or current replay code differs')
    _registry(db,result['experiment_id']+'-PROTOCOL',p,{},'registered')
    _registry(db,result['experiment_id'],p,result['records'],'completed')
    _check_code(p,folder)
    for name,digest in result['artifacts_sha256'].items():
        if sha256(_relative_file(folder,name))!=digest:
            raise ValueError('Study artifact fingerprint differs: '+name)
    frames = {name[:-8]:read_development_parquet(folder/name,digest,settings.RESEARCH_CONFIG['holdout_start'])
              for name,digest in p['inputs_sha256'].items()}
    feature_state = _selection_inputs(frames,p['calendar'],p['feature_policy'])
    proof = _load_json(folder/'feature-proof.json')
    if (proof!=result.get('feature_proof') or proof.get('state')!=feature_state
            or proof.get('source_code_sha256')!=p['code_sha256']
            or proof.get('source_inputs_sha256')!=p['inputs_sha256'] or proof.get('financial_modes_started')!=0):
        raise ValueError('Independent feature rebuild proof differs')
    _registry(db,result['experiment_id']+'-FEATURES',p,proof,'registered')
    for kind in ('features','flags'):
        name = 'recomputed-'+kind+'.parquet'
        if proof['derived_sha256'][name]!=result['artifacts_sha256'][name]:
            raise ValueError('Feature proof and used artifact fingerprint differ')
        saved = read_development_parquet(folder/name,proof['derived_sha256'][name],p['holdout_start'])
        pd.testing.assert_frame_equal(frames[kind],saved,check_exact=True)
    names = {s['name'] for s in mode_specs()}
    if set(result['records'])!=names or not set(replay_names)<=names:
        raise ValueError('Missing, additional or unregistered financial mode')
    prepared = prepare_portfolio_prices(frames['prices']) if replay_names else None
    price_proof = _load_json(folder/'prepared-prices.json')
    if (price_proof.get('version')!=PRICE_SNAPSHOT_VERSION
            or price_proof.get('rows')!=len(frames['prices'])
            or price_proof.get('source_prices_sha256')!=p['inputs_sha256']['prices.parquet']
            or price_proof.get('financial_modes_started')!=0
            or (prepared is not None and price_proof!=_price_proof(prepared,p))):
        raise ValueError('Frozen price preparation differs')
    _registry(db,result['experiment_id']+'-PRICES',p,price_proof,'registered')
    from backtest.portfolio import PortfolioResult
    days = [d for d in p['calendar'] if p['start']<=d<p['end_exclusive']]
    curves,selections = {},{}
    for spec in p['modes']:
        name = spec['name']
        record = result['records'][name]
        key = (spec['hypothesis'],spec['horizon'],spec['random_seed'])
        if key not in selections:
            selections[key] = _picks(frames,p,spec)
        selected,coverage = selections[key]
        saved_picks = read_development_parquet(folder/(name+'-picks.parquet'),result['artifacts_sha256'][name+'-picks.parquet'],settings.RESEARCH_CONFIG['holdout_start'])
        pd.testing.assert_frame_equal(selected,saved_picks)
        if record['spec']!=spec or record['coverage']!=coverage:
            raise ValueError('Selection coverage or mode identity differs')
        saved = {kind:read_development_parquet(folder/(name+'-'+kind+'.parquet'),
            result['artifacts_sha256'][name+'-'+kind+'.parquet'],settings.RESEARCH_CONFIG['holdout_start'],require_date=False)
            for kind in ('equity','executions','decisions','rolling-year')}
        prices = relevant_prices(frames['prices'],selected,start=p['start'],end=p['end_exclusive'])
        config = portfolio_config(p['capital_rial'],spec['horizon'],spec['stress'])
        replay = PortfolioResult(saved['equity'],saved['executions'],saved['decisions'],record['metrics'],record['assumptions'])
        audit,rolling = summarize_portfolio(replay,prices,days,config)
        if _json_safe(audit)!=record['audit']:
            raise ValueError('Independently reconciled financial summary differs')
        pd.testing.assert_frame_equal(rolling,saved['rolling-year'])
        if name in replay_names:
            actual = replay_portfolio(prepared,selected,p['calendar'],config,start=p['start'],end=p['end_exclusive'],
                loss_floor_exit=True,entry_allocation_fraction=ALLOCATION)
            for kind in ('equity','executions','decisions'):
                _assert_frame(getattr(actual,kind),saved[kind])
            if _json_safe(actual.metrics)!=record['metrics'] or _json_safe(actual.assumptions)!=record['assumptions']:
                raise ValueError('Reexecuted portfolio metrics or assumptions differ')
        curves[name] = saved['equity'][['date','equity_rial']]
    if _json_safe(_comparisons(result['records'],curves,p,days))!=result['comparisons']:
        raise ValueError('Recomputed paired comparisons differ')
    return dict(experiment_id=result['experiment_id'],verified_at=now_tehran_iso(),
        all_features_and_flags_recomputed=True,selection_modes_recomputed=len(names),
        financial_modes_reconciled=len(names),financial_modes_reexecuted=list(replay_names),
        current_code_matches_capture=True,holdout_opened=False,accepted_for_signals=False,
        economic_profit_verified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--pattern',type=Path)
    parser.add_argument('--pattern-db',type=Path)
    parser.add_argument('--census-db',type=Path)
    parser.add_argument('--root',type=Path)
    parser.add_argument('--capital-rial',type=float)
    parser.add_argument('--replications',type=int,default=20000)
    parser.add_argument('--feature-policy',choices=FEATURE_POLICIES,default='verify-frozen-v1')
    parser.add_argument('--review',type=Path)
    parser.add_argument('--replay-mode',action='append',default=[])
    args = parser.parse_args(argv)
    if args.db.resolve()==settings.DB_PATH.resolve():
        parser.error('Use a separate research DB')
    if args.review:
        with DatabaseManager(args.db,read_only=True) as db:
            review = review_study(db,args.review,replay_names=args.replay_mode)
        print(json.dumps(review,ensure_ascii=False))
        return 0
    if any(v is None for v in (args.pattern,args.pattern_db,args.census_db,args.root,args.capital_rial)):
        parser.error('pattern, pattern-db, census-db, root and explicit research capital-rial are required')
    with DatabaseManager(args.db) as db, DatabaseManager(args.pattern_db,read_only=True) as pattern_db, DatabaseManager(args.census_db,read_only=True) as census_db:
        result,folder = run_study(db,pattern=args.pattern,pattern_db=pattern_db,census_db=census_db,
            root=args.root,capital_rial=args.capital_rial,replications=args.replications,feature_policy=args.feature_policy)
    print(json.dumps(dict(experiment_id=result['experiment_id'],folder=str(folder),
        modes=len(result['records']),accepted_for_signals=False,economic_profit_verified=False),ensure_ascii=False))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
