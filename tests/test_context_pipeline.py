"""Context experiment registration, complete replay, family pinning and archival guards."""

import json

import pytest

from config import settings
from research.context_expectancy import CONTEXT_PREDICTORS, recipe, validate_context_model
from research.expectancy_funnel import inspect_archive
from research.expectancy_run import (
    CONTEXT_RUN_VERSION,
    VERSION,
    expected_modes,
    main,
    registered_model_family,
    review_expectancy,
    run_expectancy,
)

pytest_plugins = ['tests.test_flow_research']


def test_context_family_is_registered_before_fit_and_fully_replayed(db, flow_baseline, monkeypatch):
    from features.store import build_feature_store
    from research import expectancy_run
    from research.run import run_research

    _, base = flow_baseline
    monkeypatch.setitem(settings.RESEARCH_CONFIG, 'horizons', [1, 2])
    build_feature_store(db)
    source = run_research(db, random_trials=1)
    original = expectancy_run.fit_expectancy
    saw_registered_context = []
    def guarded(*args, **kwargs):
        assert kwargs['model_family'] == 'context-v1'
        record = db.conn.execute("SELECT parameters_json FROM experiments WHERE strategy_name='expectancy-protocol' ORDER BY rowid DESC LIMIT 1").fetchone()
        protocol = json.loads(record[0])
        assert protocol['version'] == CONTEXT_RUN_VERSION
        assert protocol['model_family'] == 'context-v1'
        assert protocol['predictors'] == list(CONTEXT_PREDICTORS)
        assert protocol['interaction_recipe'] == recipe()
        saw_registered_context.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(expectancy_run, 'fit_expectancy', guarded)
    result = run_expectancy(db, source_id=source['experiment_id'], trials_attempted=100,
        replications=1000, min_train_sessions=104, test_sessions=30, model_family='context-v1')
    assert saw_registered_context and result['review']['model_retrained_by_review']
    assert result['review']['model_family'] == 'context-v1'
    assert result['review']['uncertainty_recomputed'] and result['review']['execution_replayed']
    assert set(result['summary']) == expected_modes()
    folder = base/'research'/result['experiment_id']
    for path in folder.glob('fold-*-model.json'):
        validate_context_model(json.loads(path.read_text(encoding='utf-8')))
    funnel = inspect_archive(db, result['experiment_id'])
    assert funnel['forecast_rows_recomputed'] and funnel['conditional_picks_recomputed']
    assert not result['accepted_for_signals'] and not (base/'research'/'holdout-unsealed.json').exists()
    source_path = base/'research'/(result['experiment_id']+'.json')
    original_source = source_path.read_bytes()
    original_protocol = (folder/'protocol.json').read_bytes()
    modified = json.loads(original_source)
    modified['protocol']['interaction_recipe'][0]['formula'] = 'future_return'
    try:
        source_path.write_text(json.dumps(modified), encoding='utf-8')
        (folder/'protocol.json').write_text(json.dumps(modified['protocol']), encoding='utf-8')
        with pytest.raises(ValueError, match='دستور'):
            review_expectancy(db, result['experiment_id'])
        with pytest.raises(ValueError, match='دستور'):
            inspect_archive(db, result['experiment_id'])
    finally:
        source_path.write_bytes(original_source)
        (folder/'protocol.json').write_bytes(original_protocol)


@pytest.mark.parametrize('change', ['unknown', 'downgrade', 'recipe', 'order', 'missing_family'])
def test_family_contract_is_not_reinterpreted(change):
    protocol = dict(version=CONTEXT_RUN_VERSION, model_family='context-v1',
        predictors=list(CONTEXT_PREDICTORS), interaction_recipe=recipe(),
        fixed_hypotheses='docs/research/context-expectancy-protocol-fa.md')
    if change == 'unknown':
        protocol['model_family'] = 'another'
    elif change == 'downgrade':
        protocol['version'] = VERSION
    elif change == 'recipe':
        protocol['interaction_recipe'] = []
    elif change == 'order':
        protocol['predictors'].reverse()
    else:
        del protocol['model_family']
    with pytest.raises(ValueError):
        registered_model_family(protocol)


def test_review_cannot_override_the_archived_model_family():
    with pytest.raises(ValueError, match='دو کار جدا'):
        main(['--review', 'not-read', '--model-family', 'context-v1'])


def test_unknown_family_stops_before_database_or_inputs_are_touched():
    with pytest.raises(ValueError, match='خانواده'):
        run_expectancy(None, source_id=None, trials_attempted=100, model_family='wrong')
