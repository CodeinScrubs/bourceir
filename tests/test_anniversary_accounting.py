"""Economic replay, missing evidence, immutable inputs and real Parquet verification."""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from config import settings
from research.anniversary import replay_sleeve
from research.anniversary_accounting import review, run
from research.corporate_inputs import load_inputs
from tests.test_anniversary import freeze
from tests.test_anniversary import pinned as pinned  # noqa: F401


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


@pytest.fixture
def accounting_parent(pinned):
    root, intent = pinned
    frozen = freeze(root)
    documents = root / 'documents'
    documents.mkdir()
    evidence = []
    for identifier, role in [('coverage', 'coverage_review'), ('issuer', 'issuer_disclosure'),
                             ('cash', 'holder_cash_receipt'), ('credit', 'holder_share_credit')]:
        p = documents / (identifier + '.txt')
        p.write_text('Fictional test document: ' + role, encoding='utf-8')
        evidence.append(dict(id=identifier, role=role, path='documents/' + p.name,
            sha256=hashlib.sha256(p.read_bytes()).hexdigest(), available_at='2024-02-01T10:00:00+03:30'))
    cases = [dict(symbol=row['symbol'], ins_code=row['ins_code'], coverage='declared_complete',
        coverage_note='Scoped fictional test coverage review, never real market evidence.',
        coverage_evidence_ids=['coverage'], bonuses=[], dividends=[], payments=[], releases=[], fraction_settlements=[])
        for row in frozen['selected']]
    payload = dict(version='corporate-action-inputs-v1', price_unit='Rial', evidence=evidence, cases=cases)
    actions = root / 'actions.json'
    write(actions, payload)
    return root, intent, frozen, actions, payload


def execute(fixture):
    root, _, _, actions, _ = fixture
    return run(root / 'study', actions, root / 'economic')


def dividend_path(fixture, *, paid=False, bonus=False):
    root, _, frozen, actions, payload = fixture
    frame = pd.read_parquet(root / 'prices.parquet')
    symbol = frozen['selected'][0]['symbol']
    own = frame.symbol.eq(symbol)
    first = own & frame.date.ge('2024-01-24')
    after = own & frame.date.ge('2024-01-25')
    for mask, price in ((first, 100.), (after, 90.)):
        frame.loc[mask, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = price
        frame.loc[mask, 'high_raw'] = price + 1
        frame.loc[mask, 'low_raw'] = price - 1
        frame.loc[mask, 'value'] = frame.loc[mask, 'volume'] * price
    previous = float(frame.loc[own & frame.date.eq('2024-01-23'), 'close_final_raw'].iloc[0])
    frame.loc[own & frame.date.eq('2024-01-24'), 'ref_price_raw'] = previous
    frame.to_parquet(root / 'prices.parquet', index=False)
    # Recreate the parent only from these test inputs, before any child study.
    parent = root / 'adjusted-study'
    from research.anniversary_run import freeze_selection
    freeze_selection(parent, prices_path=root / 'prices.parquet', registry_path=root / 'registry.json',
                     intent_path=root / 'intent.json')
    case = payload['cases'][0]
    case['dividends'] = [dict(event_id='DPS', symbol=symbol, record_date='2024-01-24',
        ex_date='2024-01-25', dps_rial='10', previous_price_session='2024-01-24', evidence_id='issuer')]
    if paid:
        case['payments'] = [dict(payment_id='PAID', event_id='DPS', date='2024-01-26',
                                 per_share_rial='10', evidence_id='cash')]
    if bonus:
        frame = pd.read_parquet(root / 'prices.parquet')
        mask = own & frame.date.ge('2024-01-26')
        frame.loc[mask, ['open_raw', 'close_raw', 'close_final_raw', 'ref_price_raw']] = 72.
        frame.loc[mask, 'high_raw'], frame.loc[mask, 'low_raw'] = 73., 71.
        frame.loc[mask, 'value'] = frame.loc[mask, 'volume'] * 72.
        frame.to_parquet(root / 'bonus-prices.parquet', index=False)
        parent = root / 'bonus-study'
        freeze_selection(parent, prices_path=root / 'bonus-prices.parquet', registry_path=root / 'registry.json',
                         intent_path=root / 'intent.json')
        case['bonuses'] = [dict(event_id='BONUS', symbol=symbol, record_date='2024-01-25',
            ex_date='2024-01-26', bonus_numerator=1, bonus_denominator=4,
            previous_price_session='2024-01-25', previous_official_rial='90', ex_reference_rial='72', evidence_id='issuer')]
    write(actions, payload)
    return parent, symbol


def test_complete_no_action_study_preserves_parent_selection_and_replays_all_twenty(accounting_parent):
    root, intent, frozen, _, _ = accounting_parent
    parent_bytes = (root / 'study/selection.json').read_bytes()
    result = execute(accounting_parent)
    prices = pd.read_parquet(root / 'economic/inputs/prices.parquet')
    days = json.loads((root / 'economic/inputs/calendar.json').read_text(encoding='utf-8'))['days']
    returns = []
    for row in frozen['selected']:
        summary, *_ = replay_sleeve(prices, symbol=row['symbol'], decision=frozen['decision_date'], calendar=days,
            initial_capital_rial=intent['initial_capital_rial']/10, fees=intent['fees'],
            slippage_bps=intent['slippage_bps'], participation=intent['participation'])
        returns.append(summary['net_model_return'])
    assert result['scenarios']['normal']['net_model_return'] == pytest.approx(np.mean(returns))
    assert result['scenarios']['normal']['sleeves'] == 10
    assert result['selected'] == [s['symbol'] for s in frozen['selected']]
    assert (root / 'study/selection.json').read_bytes() == parent_bytes
    assert review(root / 'economic')['sleeves_recomputed'] == 20
    assert not result['accepted_for_signals'] and not result['economic_profit_verified']


def test_one_incomplete_sleeve_stays_in_denominator_and_has_no_plausible_full_nav(accounting_parent):
    root, _, _, actions, payload = accounting_parent
    payload['cases'][0]['coverage'] = 'partial'
    write(actions, payload)
    result = execute(accounting_parent)
    for mode in ('normal', 'stress'):
        case = result['scenarios'][mode]
        assert case['net_model_return'] is None and case['sleeves'] == 10 and case['unknown_sleeves'] == 1
        assert len(case['stocks']) == 10
        first = case['stocks'][0]
        assert first['status'] == 'incomplete_event_coverage' and first['summary']['fully_liquidated'] is None
        curve = pd.read_parquet(root / 'economic' / (first['ins_code'] + '-' + mode + '-curve.parquet'))
        assert curve.equity_rial.isna().all()


@pytest.mark.parametrize('mutation', ['missing_sleeve', 'wrong_unit', 'wrong_identity', 'wrong_hash', 'traversal', 'future_receipt'])
def test_invalid_evidence_or_scope_is_rejected_before_creating_an_outcome_study(accounting_parent, mutation):
    root, _, _, actions, payload = accounting_parent
    if mutation == 'missing_sleeve':
        payload['cases'].pop()
    elif mutation == 'wrong_unit':
        payload['price_unit'] = 'Toman'
    elif mutation == 'wrong_identity':
        payload['cases'][0]['ins_code'] = 25.
    elif mutation == 'wrong_hash':
        payload['evidence'][0]['sha256'] = 'a'*64
    elif mutation == 'traversal':
        payload['evidence'][0]['path'] = '../outside.txt'
    else:
        payload['evidence'][0]['available_at'] = '2099-01-01T10:00:00+03:30'
    write(actions, payload)
    with pytest.raises(ValueError):
        execute(accounting_parent)
    assert not (root / 'economic').exists()


def test_declared_complete_coverage_needs_an_explicit_coverage_review(accounting_parent):
    _, _, frozen, actions, payload = accounting_parent
    payload['cases'][0]['coverage_evidence_ids'] = ['issuer']
    write(actions, payload)
    with pytest.raises(ValueError, match='coverage review'):
        load_inputs(actions, frozen['selected'])


def test_dividend_receivable_is_not_cash_or_a_number_for_total_profit(accounting_parent):
    root, _, _, actions, _ = accounting_parent
    parent, symbol = dividend_path(accounting_parent)
    result = run(parent, actions, root / 'economic')
    case = result['scenarios']['normal']['stocks'][0]
    assert case['symbol'] == symbol and case['net_model_return'] is None
    account = case['summary']['corporate_accounting']
    assert account['cash_dividend_receivable_face_rial'] > 0 and account['cash_dividend_income_rial'] == 0
    assert result['scenarios']['normal']['net_model_return'] is None
    transitions = result['accounting_requirements'][0]['transitions']
    assert len(transitions) == 1 and transitions[0]['date'] == '2024-01-25'
    assert transitions[0]['declared_evidence_id'] == 'issuer' and not transitions[0]['action_type_inferred']
    assert review(root / 'economic')['verified']


def test_paid_dividend_is_counted_once_after_a_holder_receipt(accounting_parent):
    root, _, _, actions, _ = accounting_parent
    parent, _ = dividend_path(accounting_parent, paid=True)
    result = run(parent, actions, root / 'economic')
    case = result['scenarios']['normal']['stocks'][0]
    account = case['summary']['corporate_accounting']
    assert case['net_model_return'] is not None
    assert account['cash_dividend_receivable_face_rial'] == 0 and account['cash_dividend_income_rial'] > 0
    assert len([e for e in account['cash_dividend_ledger'] if e['kind'] == 'cash_received']) == 1
    assert review(root / 'economic')['verified']


def test_publisher_schedule_cannot_substitute_for_a_holder_payment(accounting_parent):
    root, _, _, actions, payload = accounting_parent
    dividend_path(accounting_parent, paid=True)
    payload['cases'][0]['payments'][0]['evidence_id'] = 'issuer'
    write(actions, payload)
    with pytest.raises(ValueError, match='Issuer schedules'):
        run(root / 'adjusted-study', actions, root / 'economic')


def test_uncredited_bonus_preserves_claim_and_blocks_economic_return(accounting_parent):
    root, _, _, actions, _ = accounting_parent
    parent, _ = dividend_path(accounting_parent, paid=True, bonus=True)
    result = run(parent, actions, root / 'economic')
    case = result['scenarios']['normal']['stocks'][0]
    assert case['net_model_return'] is None
    assert case['summary']['corporate_accounting']['pending_bonus_units']['numerator'] > 0
    assert review(root / 'economic')['verified']


def test_reference_model_mismatch_keeps_both_original_scenarios_instead_of_dropping_stock(accounting_parent):
    root, _, _, actions, payload = accounting_parent
    parent, _ = dividend_path(accounting_parent, paid=True, bonus=True)
    payload['cases'][0]['bonuses'][0]['ex_reference_rial'] = '75'
    write(actions, payload)
    result = run(parent, actions, root / 'economic')
    for mode in ('normal', 'stress'):
        row = result['scenarios'][mode]
        assert len(row['stocks']) == 10 and row['unknown_sleeves'] == 1
        assert row['stocks'][0]['status'] == 'blocked_explicit_accounting'
        assert row['net_model_return'] is None
    assert review(root / 'economic')['verified']


def test_review_rejects_a_self_consistent_rewritten_input_manifest(accounting_parent):
    root, _, _, _, _ = accounting_parent
    execute(accounting_parent)
    target = root / 'economic/inputs/prices.parquet'
    prices = pd.read_parquet(target)
    prices.loc[0, 'value'] += 1
    prices.to_parquet(target, index=False)
    manifest_path = root / 'economic/inputs-manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['prices.parquet'] = hashlib.sha256(target.read_bytes()).hexdigest()
    write(manifest_path, manifest)
    start_path = root / 'economic/evaluation-start.json'
    start = json.loads(start_path.read_text(encoding='utf-8'))
    start['inputs_manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    write(start_path, start)
    with pytest.raises(ValueError, match='Immutable'):
        review(root / 'economic')


def test_self_consistent_filed_ledger_is_compared_against_the_actual_replay(accounting_parent, monkeypatch):
    root, _, frozen, _, _ = accounting_parent
    target = frozen['selected'][0]['ins_code'] + '-normal-executions.parquet'
    original = pd.DataFrame.to_parquet

    def faulty_writer(frame, path, *args, **kwargs):
        if path.name == target:
            frame = frame.copy()
            frame.loc[0, 'quantity'] += 1
        return original(frame, path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(pd.DataFrame, 'to_parquet', faulty_writer)
        execute(accounting_parent)
    with pytest.raises(AssertionError):
        review(root / 'economic')


def test_sealed_parent_is_rejected_before_any_numeric_price_reads(accounting_parent, monkeypatch):
    root, _, _, _, _ = accounting_parent
    p = root / 'study/protocol.json'
    protocol = json.loads(p.read_text(encoding='utf-8'))
    protocol['intent']['requested_end'] = settings.RESEARCH_CONFIG['holdout_start']
    write(p, protocol)
    monkeypatch.setattr(pd, 'read_parquet', lambda *args, **kwargs: pytest.fail('No sealed numbers may be read'))
    with pytest.raises(ValueError, match='before the seal'):
        execute(accounting_parent)


def test_existing_study_is_never_overwritten(accounting_parent):
    execute(accounting_parent)
    with pytest.raises(ValueError, match='new private study'):
        execute(accounting_parent)


def test_reference_gap_task_does_not_infer_an_event_or_turn_missing_reference_into_one():
    from research.anniversary_accounting import accounting_requirements
    from tests.test_anniversary import market
    frame, _, days = market(4)
    own = frame.loc[frame.symbol.eq('a')].copy()
    own.loc[own.date.eq(days[2]), 'ref_price_raw'] = None
    case = dict(symbol='a', coverage='partial', bonuses=[], dividends=[], payments=[], releases=[])
    tasks = accounting_requirements(own, [case], days[0], days[-1])[0]['transitions']
    assert len(tasks) == 1 and tasks[0]['observed_multiplier'] is None
    assert tasks[0]['status'] == 'unknown_reference_transition' and tasks[0]['declared_evidence_id'] is None
