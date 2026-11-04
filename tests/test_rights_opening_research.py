"""Synthetic data are confined to tests; they do not establish market performance."""

import json
from decimal import Decimal, localcontext
from fractions import Fraction

import pytest

from config import settings
from data_collector.tsetmc import TsetmcProvider
from research.rights_opening import (
    canceled_opening_candidate,
    horizon_observation,
    opening_observation,
    peak_episode_observation,
    peak_session_observation,
    reference_right_parity,
    theoretical_right,
)


def tape(code, day, entries):
    rows = [dict(nTran=i, hEven=clock, pTran=price, qTitTran=volume,
                 canceled=0, insCode=code, dEven=int(day.replace('-', '')))
            for i, (clock, price, volume) in enumerate(entries, 1)]
    prices = [row['pTran'] for row in rows]
    daily = dict(date=day, ins_code=code, source='tsetmc', price_unit='Rial',
                 open_raw=prices[0], low_raw=min(prices), high_raw=max(prices), close_raw=prices[-1],
                 close_final_raw=prices[-1], ref_price_raw=prices[0],
                 volume=sum(row['qTitTran'] for row in rows), trade_count=len(rows),
                 value=sum(row['pTran'] * row['qTitTran'] for row in rows))
    observation = dict(source='tsetmc', price_unit='Rial', requested_date=day, ins_code=code,
                       endpoint_is_grouped=False, response={'tradeHistory': rows})
    return daily, observation


def opening(**kwargs):
    right, trades = tape('12', '2024-01-01', [(90000, 100, 10), (90000, 100, 5), (90001, 101, 20)])
    parent, parent_trades = tape('13', '2024-01-01', [(85959, 1200, 10), (90001, 1300, 10)])
    args = dict(issue_confirmed=True, notice_available_before_open=True, fixed_theory=102,
                parent_daily=parent, parent_observation=parent_trades, exercise_price=1000)
    return opening_observation(right, trades, **(args | kwargs))


def test_theoretical_price_accounts_for_cash_and_bonus_increase():
    result = theoretical_right(previous_close=10663, capital_before=2800,
                               cash_increase=1400, total_increase=1400, exercise_price=1000, dividend=0)
    assert result['share_rial'] == Decimal('7442')
    assert result['right_parity_rial'] == Decimal('6442')
    mixed = theoretical_right(previous_close=5000, capital_before=1000, cash_increase=500,
                              total_increase=1000, exercise_price=1000, dividend=200)
    assert mixed['share_rial'] == Decimal('2650')
    assert mixed['right_parity_rial'] == Decimal('1650')
    with pytest.raises(ValueError):
        theoretical_right(previous_close=5000, capital_before=1000, cash_increase=1001,
                          total_increase=1000, exercise_price=1000, dividend=0)


def test_equal_opening_is_not_a_discount_even_when_intermediate_division_repeats():
    theory = theoretical_right(previous_close=10000, capital_before=7, cash_increase=2,
                               total_increase=41, exercise_price=1000, dividend=0)
    assert theory['right_parity_rial'] == Decimal('500')
    assert theory['right_parity_exact'] == {'numerator': 500, 'denominator': 1}
    daily, obs = tape('12', '2024-01-01', [(90000, 500, 10), (90001, 501, 10)])
    result = opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True,
                                 fixed_theory=theory)
    assert result['fixed_discount'] is False
    assert result['entry_still_below_fixed'] is False
    assert result['fixed_theory_representation'] == 'exact_ratio'


def test_exact_theory_comparison_does_not_depend_on_display_rounding_or_decimal_context():
    # This theoretical value exceeds 500 by less than the displayed precision.
    previous = Decimal('10000.' + '0' * 89 + '1')
    with localcontext() as context:
        context.prec = 2
        theory = theoretical_right(previous_close=previous, capital_before=7, cash_increase=2,
                                   total_increase=41, exercise_price=1000, dividend=0)
    ratio = theory['right_parity_exact']
    assert Fraction(ratio['numerator'], ratio['denominator']) == Fraction(500) + Fraction(7, 48 * 10**90)
    assert theory['right_parity_rial'] == Decimal('500')
    daily, obs = tape('12', '2024-01-01', [(90000, 500, 10), (90001, 501, 10)])
    result = opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True,
                                 fixed_theory=theory)
    assert result['fixed_discount'] is True
    assert result['entry_still_below_fixed'] is False
    # A scalar is a separate, explicitly stated decimal assumption, not the exact ratio.
    scalar = opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True,
                                 fixed_theory=theory['right_parity_rial'])
    assert scalar['fixed_discount'] is False
    assert scalar['fixed_theory_representation'] == 'explicit_decimal'


def test_theory_comparison_rejects_tampered_or_unbounded_exact_ratios():
    theory = theoretical_right(previous_close=10000, capital_before=7, cash_increase=2,
                               total_increase=41, exercise_price=1000, dividend=0)
    daily, obs = tape('12', '2024-01-01', [(90000, 500, 10)])
    for ratio in ({'numerator': 501, 'denominator': 1},
                  {'numerator': 500, 'denominator': 0},
                  {'numerator': True, 'denominator': 1},
                  {'numerator': 1 << 4097, 'denominator': 1}):
        with pytest.raises(ValueError):
            opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True,
                                fixed_theory=theory | {'right_parity_exact': ratio})
    with pytest.raises(ValueError, match='supported exact precision'):
        theoretical_right(previous_close=Decimal('1e101'), capital_before=7, cash_increase=2,
                          total_increase=41, exercise_price=1000, dividend=0)


def test_missing_dividend_never_becomes_an_implicit_zero():
    values = dict(previous_close=5000, capital_before=1000, cash_increase=500,
                  total_increase=500, exercise_price=1000)
    with pytest.raises(TypeError):
        theoretical_right(**values)
    with pytest.raises(ValueError):
        theoretical_right(**values, dividend=None)
    no_dividend_scenario = theoretical_right(**values, dividend=0)
    distributed_scenario = theoretical_right(**values, dividend=200)
    assert no_dividend_scenario['right_parity_rial'] != distributed_scenario['right_parity_rial']
    assert no_dividend_scenario['historical_inputs_verified'] is False
    assert distributed_scenario['historical_inputs_verified'] is False


def test_parent_reference_is_an_explicit_parity_proxy_not_a_verified_corporate_action():
    result = reference_right_parity(parent_reference=9952, exercise_price=1000)
    assert result['right_parity_rial'] == Decimal('8952')
    assert result['reference_cause_verified'] is False and result['tradability_verified'] is False


def test_only_later_second_can_be_the_modeled_entry():
    result = opening()
    assert result['fixed_discount'] is True
    assert result['floating_discount'] is True
    assert result['opening_clock'] == 90000 and result['entry_clock'] == 90001
    assert result['entry_proxy_rial'] == '101' and result['entry_volume_observed'] == 20
    assert result['parent_age_seconds'] == 1
    assert result['execution_verified'] is False and result['accepted_for_signals'] is False


def test_negative_theoretical_parity_is_a_valid_non_discount():
    result = opening(fixed_theory=-50)
    assert result['status'] == 'observed' and result['fixed_discount'] is False
    assert result['entry_still_below_fixed'] is False
    assert reference_right_parity(parent_reference=900, exercise_price=1000)['right_parity_rial'] == Decimal('-100')


def test_issue_and_data_incompleteness_are_unknown_not_no_opportunity():
    assert opening(issue_confirmed=False)['fixed_discount'] is None
    right, obs = tape('12', '2024-01-01', [(90000, 100, 1), (90001, 101, 1)])
    obs['response']['tradeHistory'][1]['qTitTran'] = 2
    result = opening_observation(right, obs, issue_confirmed=True, notice_available_before_open=True,
                                 fixed_theory=110)
    assert result['status'] == 'right_tape_unverified'
    assert result['fixed_discount'] is None and result['entry_proxy_rial'] is None


def test_canceled_opening_candidate_reconciles_without_overwriting_native_daily():
    daily, obs = tape('12', '2024-01-01', [(90001, 100, 10), (90002, 105, 5)])
    for row in obs['response']['tradeHistory']:
        row['nTran'] += 1
    obs['response']['tradeHistory'].append({'nTran': 1, 'hEven': 120000, 'pTran': 120, 'qTitTran': 1,
                                            'canceled': 1, 'insCode': '12', 'dEven': 20240101})
    daily['open_raw'] = 120
    result = canceled_opening_candidate(daily, obs)
    assert result['status'] == 'candidate_reconciled_research_only'
    assert daily['open_raw'] == 120 and result['candidate_daily']['open_raw'] == 100
    assert result['candidate_evidence']['complete'] is True
    assert result['database_modified'] is False and result['accepted_for_signals'] is False
    obs['response']['tradeHistory'][-1]['pTran'] = 119
    assert canceled_opening_candidate(daily, obs)['status'] == 'canceled_first_not_evidenced'


def test_canceled_opening_candidate_rejects_inconsistent_totals_or_identity():
    daily, obs = tape('12', '2024-01-01', [(90001, 100, 10)])
    obs['response']['tradeHistory'][0]['nTran'] = 2
    obs['response']['tradeHistory'].append({'nTran': 1, 'hEven': 120000, 'pTran': 120, 'qTitTran': 1,
                                            'canceled': 1, 'insCode': '12', 'dEven': 20240101})
    daily['open_raw'] = 120
    obs['ins_code'] = '13'
    result = canceled_opening_candidate(daily, obs)
    assert result['status'] == 'candidate_not_reconciled' and result['candidate_daily'] is None
    with pytest.raises(ValueError):
        canceled_opening_candidate(daily | {'date': settings.RESEARCH_CONFIG['holdout_start']}, obs)


def test_stale_or_simultaneous_parent_trade_does_not_create_a_floating_discount():
    parent, trades = tape('13', '2024-01-01', [(85000, 1200, 10), (90000, 1300, 10)])
    result = opening(parent_daily=parent, parent_observation=trades)
    assert result['parent_age_seconds'] == 600
    assert result['floating_discount'] is None
    assert result['fixed_discount'] is True


def test_holdout_is_rejected_before_market_observation():
    with pytest.raises(ValueError):
        opening_observation({'date': settings.RESEARCH_CONFIG['holdout_start']}, None,
                            issue_confirmed=True, notice_available_before_open=True)


@pytest.mark.parametrize('bad_code', [None, True, '0', '012', '12x'])
def test_opening_requires_a_real_instrument_identity(bad_code):
    daily, obs = tape('12', '2024-01-01', [(90000, 100, 1)])
    daily['ins_code'] = bad_code
    with pytest.raises(ValueError):
        opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True)


def test_horizon_uses_same_instrument_next_print_and_net_fees():
    o = opening()
    dates = [f'2024-01-{day:02d}' for day in range(1, 12)]
    daily, obs = tape('12', dates[5], [(90000, 98, 50), (90001, 99, 10)])
    result = horizon_observation(o, market_dates=dates, horizon=5, calendar_verified=False,
                                 exit_daily=daily, exit_observation=obs, buy_fee='0.003712', sell_fee='0.0088')
    expected = (Decimal(98) * Decimal('0.9912') / (Decimal(101) * Decimal('1.003712')) - 1) * 100
    assert result['exit_date'] == dates[5]
    assert Decimal(result['net_return_pct']) == expected
    assert result['calendar_verified'] is False and result['execution_verified'] is False
    wrong, wrong_obs = tape('14', dates[5], [(90000, 500, 1)])
    absent = horizon_observation(o, market_dates=dates, horizon=5, calendar_verified=True,
                                 exit_daily=wrong, exit_observation=wrong_obs, buy_fee=0, sell_fee=0)
    assert absent['status'] == 'exit_daily_missing' and absent['net_return_pct'] is None
    with pytest.raises(ValueError):
        horizon_observation(o | {'version': 'wrong'}, market_dates=dates, horizon=5,
                            calendar_verified=False, exit_daily=daily, exit_observation=obs,
                            buy_fee=0, sell_fee=0)


def test_no_fill_and_sealed_horizon_remain_unknown():
    o = opening()
    dates = ['2024-01-01', '2024-01-02']
    missing = horizon_observation(o, market_dates=dates, horizon=5, calendar_verified=False,
                                  exit_daily=None, exit_observation=None, buy_fee=0, sell_fee=0)
    assert missing['status'] == 'missing_calendar_horizon' and missing['net_return_pct'] is None
    before = settings.RESEARCH_CONFIG['holdout_start']
    dates = ['2025-03-13', '2025-03-14', '2025-03-15', '2025-03-16', '2025-03-17', before]
    with pytest.raises(ValueError):
        horizon_observation(o | {'date': dates[0]}, market_dates=dates, horizon=5,
                            calendar_verified=False, exit_daily=None, exit_observation=None,
                            buy_fee=0, sell_fee=0)


def test_peak_price_threshold_and_net_threshold_use_distinct_bases():
    observed = opening()
    daily, obs = tape('12', '2024-01-02', [(90000, 110, 10), (90001, 113, 20)])
    result = peak_session_observation(observed, daily=daily, observation=obs,
                                      buy_fee='0.003712', sell_fee='0.0088')
    assert result['first_gross_witness']['price_rial'] == '110'
    assert result['first_gross_after_entry_witness']['price_rial'] == '110'
    assert result['first_net_after_entry_witness']['price_rial'] == '113'
    assert result['peak_after_entry']['price_rial'] == '113'
    assert result['execution_verified'] is False and result['full_trading_window_verified'] is False


def test_peak_ten_percent_boundary_is_exact_under_low_decimal_precision():
    first, first_tape = tape('12', '2024-01-01', [(90000, 101, 10), (90001, 101, 10)])
    opened = opening_observation(first, first_tape, issue_confirmed=True, notice_available_before_open=True)
    daily, obs = tape('12', '2024-01-02', [(90000, 111, 10), (90001, 112, 20)])
    with localcontext() as context:
        context.prec = 2
        result = peak_session_observation(opened, daily=daily, observation=obs, buy_fee=0, sell_fee=0)
    assert Decimal(result['gross_target_rial']) == Decimal('111.1')
    assert Decimal(result['net_target_rial']) == Decimal('111.1')
    assert result['first_gross_witness']['price_rial'] == '112'
    assert result['first_net_after_entry_witness']['price_rial'] == '112'


def test_opening_tape_value_reconciliation_is_not_fooled_by_low_precision():
    daily, obs = tape('12', '2024-01-01', [(90000, 111, 1), (90001, 112, 1)])
    with localcontext() as context:
        context.prec = 2
        actual = opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True)
        bad = opening_observation(daily | {'value': 220}, obs, issue_confirmed=True,
                                  notice_available_before_open=True)
    assert actual['status'] == 'observed'
    assert bad['status'] == 'right_tape_unverified'
    assert bad['right_evidence']['complete'] is False


def test_peak_on_entry_second_is_not_a_sell_after_entry():
    daily, obs = tape('12', '2024-01-01', [(90000, 100, 10), (90001, 120, 1),
                                           (90001, 120, 1), (90002, 100, 1)])
    opened = opening_observation(daily, obs, issue_confirmed=True, notice_available_before_open=True,
                                 fixed_theory=130)
    result = peak_session_observation(opened, daily=daily, observation=obs, buy_fee=0, sell_fee=0)
    assert result['first_gross_witness']['price_rial'] == '120'
    assert result['first_gross_after_entry_witness'] is None
    assert result['first_net_after_entry_witness'] is None
    assert result['peak_after_entry']['price_rial'] == '100'


def test_old_opening_version_is_not_silently_reused_with_new_exact_rules():
    old = opening() | {'version': 'rights-opening-research-v1'}
    daily, obs = tape('12', '2024-01-02', [(90000, 120, 10)])
    with pytest.raises(ValueError, match='compatible opening'):
        peak_session_observation(old, daily=daily, observation=obs, buy_fee=0, sell_fee=0)


def test_missing_or_conflicting_peak_data_is_not_a_negative_window_result():
    observed = opening()
    daily, obs = tape('12', '2024-01-02', [(90000, 120, 10)])
    obs['response']['tradeHistory'][0]['qTitTran'] = 11
    bad = peak_session_observation(observed, daily=daily, observation=obs, buy_fee=0, sell_fee=0)
    assert bad['status'] == 'session_unverified' and bad['first_gross_witness'] is None
    with pytest.raises(ValueError):
        peak_session_observation(observed, daily=daily | {'ins_code': '13'},
                                 observation=obs, buy_fee=0, sell_fee=0)
    with pytest.raises(ValueError):
        peak_session_observation(observed, daily=daily | {'date': settings.RESEARCH_CONFIG['holdout_start']},
                                 observation=obs, buy_fee=0, sell_fee=0)


def episode(sessions, *, market_dates=None, **kwargs):
    dates = market_dates or ['2024-01-01', '2024-01-02', '2024-01-03']
    args = dict(window_end=dates[-1], calendar_complete_verified=True, window_end_verified=True,
                source_complete_verified=True, first_open_verified=True, buy_fee='0.003712', sell_fee='0.0088')
    return peak_episode_observation(opening(), sessions=sessions, market_dates=dates, **(args | kwargs))


def episode_day(day, prices):
    daily, obs = tape('12', day, prices)
    return {'date': day, 'daily': daily, 'observation': obs}


def opening_day():
    return episode_day('2024-01-01', [(90000, 100, 10), (90000, 100, 5), (90001, 101, 20)])


def test_episode_positive_witness_survives_an_unrelated_missing_session():
    hit = episode_day('2024-01-03', [(90000, 113, 10)])
    result = episode([opening_day(), hit], calendar_complete_verified=False)
    assert result['opening_reconciled'] is True
    assert result['episode_complete_by_supplied_evidence'] is False
    assert result['unknown_session_dates'] == ['2024-01-02']
    assert result['gross_target_reached'] is True and result['net_target_after_entry_reached'] is True
    assert result['witnesses']['first_gross_witness']['date'] == '2024-01-03'
    assert result['execution_verified'] is False and result['profitability_proven'] is False


@pytest.mark.parametrize('unverified_boundary', ['first_open_verified', 'window_end_verified'])
def test_episode_witness_remains_conditional_until_issue_boundaries_are_verified(unverified_boundary):
    hit = episode_day('2024-01-03', [(90000, 113, 10)])
    middle = episode_day('2024-01-02', [(90000, 105, 10)])
    result = episode([opening_day(), middle, hit], **{unverified_boundary: False})
    assert result['episode_contract_version'] == 'rights-peak-episode-v2'
    assert result['episode_boundaries_verified_by_caller'] is False
    assert result['gross_target_reached'] is None
    assert result['gross_target_after_entry_reached'] is None
    assert result['net_target_after_entry_reached'] is None
    assert result['conditional_gross_target_reached'] is True
    assert result['conditional_gross_target_after_entry_reached'] is True
    assert result['conditional_net_target_after_entry_reached'] is True
    assert result['witnesses']['first_gross_witness']['date'] == '2024-01-03'
    assert result['execution_verified'] is False


def test_episode_miss_requires_all_sessions_and_explicit_independent_coverage():
    days = [opening_day(), episode_day('2024-01-02', [(90000, 105, 20)]),
            episode_day('2024-01-03', [(90000, 109, 20)])]
    complete = episode(days)
    assert complete['episode_complete_by_supplied_evidence'] is True
    assert complete['gross_target_reached'] is False and complete['net_target_after_entry_reached'] is False
    incomplete = episode(days[:-1])
    assert incomplete['gross_target_reached'] is None and incomplete['unknown_session_dates'] == ['2024-01-03']
    for flag in ('calendar_complete_verified', 'window_end_verified', 'source_complete_verified', 'first_open_verified'):
        assert episode(days, **{flag: False})['gross_target_reached'] is None


def test_episode_rejects_conflicting_opening_even_when_later_prices_cross():
    changed = episode_day('2024-01-01', [(90000, 100, 10), (90001, 102, 20)])
    hit = episode_day('2024-01-02', [(90000, 120, 1)])
    result = episode([changed, hit])
    assert result['opening_reconciled'] is False
    assert result['gross_target_reached'] is None
    assert result['witnesses']['first_gross_witness'] is None
    assert result['peak_after_entry_observed'] is None
    assert '2024-01-01' in result['unknown_session_dates']


def test_episode_zero_volume_requires_coherent_daily_identity_and_totals():
    day = {'date': '2024-01-02', 'ins_code': '12', 'source': 'tsetmc', 'price_unit': 'Rial',
           'volume': 0, 'trade_count': 0, 'value': 0}
    zero = {'date': day['date'], 'daily': day, 'observation': None}
    last = episode_day('2024-01-03', [(90000, 105, 20)])
    assert episode([opening_day(), zero, last])['gross_target_reached'] is False
    zero['daily'] = dict(day, trade_count=1)
    unknown = episode([opening_day(), zero, last])
    assert unknown['gross_target_reached'] is None and unknown['unknown_session_dates'] == ['2024-01-02']


def test_episode_checks_order_duplicates_flags_and_sealed_boundary():
    with pytest.raises(ValueError):
        episode([opening_day(), opening_day()])
    with pytest.raises(ValueError):
        episode([opening_day()], market_dates=['2024-01-02', '2024-01-01'])
    with pytest.raises(ValueError):
        episode([opening_day()], first_open_verified=1)
    with pytest.raises(ValueError):
        episode([opening_day()], window_end=settings.RESEARCH_CONFIG['holdout_start'])


def test_development_history_excludes_holdout_before_return(monkeypatch):
    p = TsetmcProvider()
    data = {'closingPriceDaily': [
        {'dEven': 20240101, 'insCode': '12', 'priceFirst': 123},
        {'dEven': 20250321, 'insCode': '12', 'priceFirst': 999999},
    ]}
    content = json.dumps(data).encode()
    monkeypatch.setattr(p, '_get_quote_context_json', lambda *a, **kw: {
        'content': content, 'body_sha256': 'test-digest', 'source': 'tsetmc', 'ins_code': '12'})
    result = p.get_development_history_observation('12', '2024-01-01', '2025-03-21')
    assert result['payload']['closingPriceDaily'] == [data['closingPriceDaily'][0]]
    assert 'content' not in result and result['source_body_archived'] is False
    assert result['selected_row_count'] == 1 and 'source_row_count' not in result
    with pytest.raises(ValueError):
        p.get_development_history_observation('12', '2024-01-01', '2025-03-22')


def test_invalid_sealed_numeric_value_does_not_change_development_rows(monkeypatch):
    p = TsetmcProvider()
    content = (b'{"closingPriceDaily":[{"dEven":20240101,"insCode":"12","priceFirst":123},'
               b'{"dEven":20250321,"insCode":"different","priceFirst":1e999}]}')
    monkeypatch.setattr(p, '_get_quote_context_json', lambda *a, **kw: {
        'content': content, 'body_sha256': 'test-digest', 'source': 'tsetmc', 'ins_code': '12'})
    result = p.get_development_history_observation('12', '2024-01-01', '2025-03-21')
    assert result['payload']['closingPriceDaily'] == [{'dEven': 20240101, 'insCode': '12', 'priceFirst': 123}]


@pytest.mark.parametrize('body', [b'{"closingPriceDaily":[],"closingPriceDaily":[]}',
                                  b'{"closingPriceDaily":[{"dEven":20240101,"insCode":"13"}]}',
                                  b'{"closingPriceDaily":[{"dEven":20241301,"insCode":"12"}]}',
                                  b'{"closingPriceDaily":[{"dEven":20240101,"insCode":"12","p":1e999}]}'])
def test_development_history_rejects_bad_source_without_fabrication(monkeypatch, body):
    p = TsetmcProvider()
    monkeypatch.setattr(p, '_get_quote_context_json', lambda *a, **kw: {'content': body})
    assert p.get_development_history_observation('12', '2024-01-01', '2025-03-21') is None
