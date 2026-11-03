"""Synthetic inputs exist only in tests; outcomes are not market evidence."""

import importlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

engine = importlib.import_module('research.rights_opening')


def tape(day, entries):
    code = '12'
    rows = [dict(nTran=i, hEven=clock, pTran=price, qTitTran=volume, canceled=0,
                 insCode=code, dEven=int(day.replace('-', '')))
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


class TestRetrospectiveRightsScope(unittest.TestCase):
    def setUp(self):
        now = datetime(2026, 11, 1, 12, tzinfo=ZoneInfo('Asia/Tehran'))
        patcher = patch.object(engine, 'now_tehran', return_value=now)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.cutoff = '2026-11-01'

    def open_day(self, day):
        daily, obs = tape(day, [(90000, 100, 5), (90001, 101, 10)])
        opening = engine.opening_observation(daily, obs, issue_confirmed=True,
            notice_available_before_open=True, fixed_theory=120,
            retrospective_cutoff_exclusive=self.cutoff)
        return daily, obs, opening

    def test_default_guards_and_explicit_retrospective_work_for_both_sides(self):
        for day in ('2010-01-01', '2025-04-01'):
            daily, obs = tape(day, [(90000, 100, 5), (90001, 101, 10)])
            with self.assertRaises(ValueError):
                engine.opening_observation(daily, obs, issue_confirmed=True,
                                           notice_available_before_open=True)
            _, _, opening = self.open_day(day)
            self.assertTrue(opening['fixed_discount'])
            self.assertEqual(opening['analysis_scope']['cutoff_exclusive'], self.cutoff)
            self.assertTrue(opening['analysis_scope']['holdout_contaminated_for_rights_hypothesis'])
            self.assertFalse(opening['analysis_scope']['independent_holdout_for_rights_hypothesis'])
            self.assertFalse(opening['accepted_for_signals'])

    def test_peak_replays_with_consistent_scope_and_cannot_become_signal(self):
        first, first_tape, opening = self.open_day('2010-01-01')
        second, second_tape = tape('2010-01-02', [(90000, 112, 5), (90001, 111, 10)])
        result = engine.peak_episode_observation(opening,
            sessions=[dict(date=first['date'], daily=first, observation=first_tape),
                      dict(date=second['date'], daily=second, observation=second_tape)],
            market_dates=[first['date'], second['date']], window_end=second['date'],
            calendar_complete_verified=True, window_end_verified=True,
            source_complete_verified=True, first_open_verified=True, buy_fee=0, sell_fee=0,
            retrospective_cutoff_exclusive=self.cutoff)
        self.assertTrue(result['gross_target_reached'])
        self.assertTrue(result['analysis_scope']['holdout_contaminated_for_rights_hypothesis'])
        self.assertFalse(result['accepted_for_signals'])
        self.assertFalse(result['profitability_proven'])
        self.assertFalse(result['execution_verified'])

    def test_later_registration_notice_cannot_become_a_known_opening(self):
        daily, obs = tape('2025-04-01', [(90000, 100, 5), (90001, 101, 10)])
        result = engine.opening_observation(daily, obs, issue_confirmed=True,
            notice_available_before_open=False, fixed_theory=120,
            retrospective_cutoff_exclusive=self.cutoff)
        self.assertEqual(result['status'], 'issue_unverified')
        self.assertIsNone(result['opening_rial'])
        self.assertIsNone(result['fixed_discount'])
        self.assertFalse(result['accepted_for_signals'])

    def test_scope_mismatch_and_unverified_boundary_fail_closed(self):
        first, first_tape, opening = self.open_day('2025-04-01')
        args = dict(sessions=[dict(date=first['date'], daily=first, observation=first_tape)],
            market_dates=[first['date']], window_end=first['date'],
            calendar_complete_verified=True, window_end_verified=False,
            source_complete_verified=True, first_open_verified=True, buy_fee=0, sell_fee=0)
        with self.assertRaises(ValueError):
            engine.peak_episode_observation(opening, **args)
        with self.assertRaises(ValueError):
            engine.peak_episode_observation(opening, **args, retrospective_cutoff_exclusive='2026-10-31')
        result = engine.peak_episode_observation(opening, **args, retrospective_cutoff_exclusive=self.cutoff)
        self.assertIsNone(result['gross_target_reached'])
        self.assertIsNone(result['conditional_gross_target_reached'])

    def test_complete_no_witness_differs_from_unknown_missing_day(self):
        first, first_tape, opening = self.open_day('2025-04-01')
        args = dict(market_dates=['2025-04-01', '2025-04-02'], window_end='2025-04-02',
            calendar_complete_verified=True, window_end_verified=True,
            source_complete_verified=True, first_open_verified=True, buy_fee=0, sell_fee=0,
            retrospective_cutoff_exclusive=self.cutoff)
        day2, day2_tape = tape('2025-04-02', [(90000, 105, 10), (90001, 106, 10)])
        first_session = dict(date=first['date'], daily=first, observation=first_tape)
        second_session = dict(date=day2['date'], daily=day2, observation=day2_tape)
        result = engine.peak_episode_observation(opening, sessions=[first_session, second_session], **args)
        self.assertIs(result['gross_target_reached'], False)
        missing = engine.peak_episode_observation(opening, sessions=[first_session], **args)
        self.assertIsNone(missing['gross_target_reached'])
        self.assertEqual(missing['unknown_session_dates'], ['2025-04-02'])

    def test_witness_remains_conditional_when_episode_boundaries_are_unknown(self):
        first, first_tape, opening = self.open_day('2025-04-01')
        second, second_tape = tape('2025-04-02', [(90000, 112, 5), (90001, 111, 10)])
        result = engine.peak_episode_observation(opening,
            sessions=[dict(date=first['date'], daily=first, observation=first_tape),
                      dict(date=second['date'], daily=second, observation=second_tape)],
            market_dates=[first['date'], second['date']], window_end=second['date'],
            calendar_complete_verified=False, window_end_verified=False,
            source_complete_verified=False, first_open_verified=True, buy_fee=0, sell_fee=0,
            retrospective_cutoff_exclusive=self.cutoff)
        self.assertIs(result['conditional_gross_target_reached'], True)
        self.assertIsNone(result['gross_target_reached'])
        self.assertFalse(result['episode_complete_by_supplied_evidence'])

    def test_invalid_future_and_exclusive_cutoff_rejected(self):
        for cutoff in (True, '2026-02-30', '1967-01-01', '2026-11-02', 20261101):
            with self.assertRaises(ValueError):
                engine._day('2025-04-01', retrospective_cutoff_exclusive=cutoff)
        for day in ('1966-12-31', '2026-11-01', '2027-01-01'):
            with self.assertRaises(ValueError):
                engine._day(day, retrospective_cutoff_exclusive=self.cutoff)
        self.assertEqual(engine._day('1967-01-01', retrospective_cutoff_exclusive=self.cutoff), '1967-01-01')

    def test_development_opening_cannot_be_relabelled_retrospective(self):
        daily, obs = tape('2024-01-01', [(90000, 100, 5), (90001, 101, 10)])
        opening = engine.opening_observation(daily, obs, issue_confirmed=True,
                                            notice_available_before_open=True)
        with self.assertRaises(ValueError):
            engine.peak_session_observation(opening, daily=daily, observation=obs, buy_fee=0, sell_fee=0,
                                            retrospective_cutoff_exclusive=self.cutoff)

    def test_horizon_and_canceled_paths_keep_scope(self):
        daily, obs, opening = self.open_day('2025-04-01')
        days = [(datetime(2025, 4, 1) + timedelta(days=i)).date().isoformat() for i in range(6)]
        last, last_obs = tape(days[-1], [(90000, 120, 10), (90001, 121, 10)])
        result = engine.horizon_observation(opening, market_dates=days, horizon=5,
            calendar_verified=True, exit_daily=last, exit_observation=last_obs, buy_fee=0, sell_fee=0,
            retrospective_cutoff_exclusive=self.cutoff)
        self.assertEqual(result['status'], 'modeled_price_scenario')
        self.assertEqual(result['analysis_scope']['cutoff_exclusive'], self.cutoff)
        candidate = engine.canceled_opening_candidate(daily, obs, retrospective_cutoff_exclusive=self.cutoff)
        self.assertEqual(candidate['analysis_scope']['cutoff_exclusive'], self.cutoff)
        self.assertFalse(candidate['database_modified'])
        with self.assertRaises(ValueError):
            engine.horizon_observation(opening, market_dates=days + [self.cutoff], horizon=5,
                calendar_verified=True, exit_daily=last, exit_observation=last_obs, buy_fee=0, sell_fee=0,
                retrospective_cutoff_exclusive=self.cutoff)
