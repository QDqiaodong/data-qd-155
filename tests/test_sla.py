"""Tests for queue-sla: business-hours SLA tick accounting.

Small scenarios are cross-checked with ``sla_oracle``, an independent
per-tick enumeration that never imports the production engine.  The
large-scale scenario is verified against expectations derived from a
provable closed construction (sparse multi-period windows, overlapping
open/closed exceptions, same-calendar cases with different pauses and
many distinct checkpoint ticks), not merely "the program finished".
"""
import copy
import hashlib
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

import sla_oracle as oracle
from queue_sla.cli import run as sla_run
from queue_sla.model import analyze

ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
TICK_LIMIT = 10 ** 12
PERIOD_LIMIT = 10 ** 9


def calendar(cal_id, period, windows, exceptions=None):
    return {'id': cal_id, 'period': period, 'windows': windows,
            'exceptions': exceptions or []}


def exception(exc_id, kind, low, high):
    return {'id': exc_id, 'kind': kind, 'from_tick': low,
            'to_tick': high}


def pause(pause_id, low, high):
    return {'id': pause_id, 'from_tick': low, 'to_tick': high}


def checkpoint(checkpoint_id, at_tick):
    return {'id': checkpoint_id, 'at_tick': at_tick}


def case(case_id, cal_id, opened_at, budget, pauses=None,
         checkpoints=None):
    return {'id': case_id, 'calendar': cal_id, 'opened_at': opened_at,
            'budget': budget, 'pauses': pauses or [],
            'checkpoints': checkpoints or []}


def document(horizon, calendars, cases):
    return {'horizon': horizon, 'calendars': calendars, 'cases': cases}


def write_json(directory, name, doc):
    path = Path(directory) / ('%s-%d.json' % (name, random.randrange(10 ** 9)))
    path.write_text(json.dumps(doc, sort_keys=True))
    return path


# ---------------------------------------------------------------------------
# Hand-built scenarios whose answers are also cross-checked per tick.
# ---------------------------------------------------------------------------

def overlap_document():
    """Closed/pause overlaps and an open exception hidden by a pause.

    Calendar C period 10 with windows [2,6); a closed exception
    [5,9) overlaps the window and the following closed stretch, an
    open exception [12,15) falls partly inside a pause.  Two cases
    share the calendar but carry different pauses to prove isolation.
    """
    cal = calendar('C', 10, [[2, 6]], [
        exception('cx', 'closed', 5, 9),
        exception('ox', 'open', 12, 15)])
    case_a = case('A', 'C', 0, 6,
                  pauses=[pause('p1', 4, 7), pause('p2', 6, 13)],
                  checkpoints=[checkpoint('c%02d' % t, t)
                               for t in range(0, 21)])
    case_b = case('B', 'C', 0, 4, pauses=[],
                  checkpoints=[checkpoint('q%02d' % t, t)
                               for t in range(0, 21)])
    return document(20, [cal], [case_a, case_b])


class OracleCrossCheckTest(unittest.TestCase):
    def assert_matches_oracle(self, doc):
        expected = oracle.solve(doc)
        actual = analyze(copy.deepcopy(doc))
        self.assertEqual(actual, expected)
        return actual

    def test_overlap_document_matches_per_tick_oracle(self):
        self.assert_matches_oracle(overlap_document())

    def test_hand_closed_pause_no_double_deduction(self):
        # Closed [4,8) and a pause [6,10): the closed-and-paused ticks
        # are removed once, not twice; elapsed can never go negative.
        doc = document(20, [
            calendar('C', 5, [[0, 5]], [exception('x', 'closed', 4, 8)])],
            [case('K', 'C', 0, 3,
                  pauses=[pause('p', 6, 10)],
                  checkpoints=[checkpoint('t%d' % t, t)
                               for t in range(21)])])
        self.assert_matches_oracle(doc)

    def test_open_exception_inside_pause_does_not_accrue(self):
        # Open exception [10,14) is wholly inside pause [9,15): it must
        # not add any time to this case.
        doc = document(20, [
            calendar('C', 100, [], [exception('o', 'open', 10, 14)])],
            [case('K', 'C', 0, 2, pauses=[pause('p', 9, 15)],
                  checkpoints=[checkpoint('t%d' % t, t)
                               for t in (0, 9, 10, 14, 15, 20)])])
        rows = {row['id']: row for row in
                self.assert_matches_oracle(doc)['cases']}
        self.assertTrue(all(item['elapsed'] == 0
                            for item in rows['K']['checkpoints']))

    def test_two_cases_same_calendar_different_pauses_isolated(self):
        doc = overlap_document()
        rows = {row['id']: row for row in analyze(doc)['cases']}
        # Case B has no pauses; its accrual must exceed paused case A
        # once the paused open ticks would otherwise have counted.
        elapsed = {cid: {item['at_tick']: item['elapsed']
                         for item in rows[cid]['checkpoints']}
                   for cid in ('A', 'B')}
        self.assertGreaterEqual(
            [elapsed['B'][t] for t in sorted(elapsed['B'])],
            [elapsed['A'][t] for t in sorted(elapsed['A'])])
        self.assertNotEqual(elapsed['A'], elapsed['B'])

    def test_removing_pause_still_bound_by_closed_exception(self):
        # With the pause removed, the closed exception still blocks the
        # same ticks; the oracle encodes the joint set directly.
        doc = document(30, [
            calendar('C', 10, [[0, 10]],
                     [exception('c', 'closed', 10, 20)])],
            [case('K', 'C', 0, 5, pauses=[pause('p', 15, 25)],
                  checkpoints=[checkpoint('t%d' % t, t)
                               for t in (0, 10, 20, 30)])])
        self.assert_matches_oracle(doc)
        doc['cases'][0]['pauses'] = []
        self.assert_matches_oracle(doc)

    def test_randomized_documents_against_oracle(self):
        rng = random.Random(7)
        for _ in range(120):
            doc = _random_document(rng)
            self.assert_matches_oracle(doc)

    def test_randomized_degenerate_documents_against_oracle(self):
        rng = random.Random(11)
        for _ in range(60):
            doc = _random_document(rng, empty_calendars=True)
            self.assert_matches_oracle(doc)


def _random_document(rng, empty_calendars=False):
    horizon = rng.randint(1, 40)
    calendars = []
    for ci in range(rng.randint(1, 3)):
        period = rng.randint(1, 10)
        pairs = [(a, b) for a in range(period)
                 for b in range(a + 1, period + 1)]
        rng.shuffle(pairs)
        if empty_calendars and rng.random() < 0.5:
            windows = []
        else:
            windows = [list(pair) for pair in
                       pairs[:rng.randint(0, min(3, len(pairs)))]]
        exceptions = []
        for ei in range(rng.randint(0, 4)):
            low = rng.randint(0, horizon - 1)
            high = rng.randint(low + 1, horizon)
            exceptions.append(exception(
                'e%d' % ei, rng.choice(['open', 'closed']), low, high))
        calendars.append(calendar('cal%d' % ci, period, windows,
                                  exceptions))
    cases = []
    for ki in range(rng.randint(0, 4)):
        opened = rng.randint(0, horizon)
        pauses = [pause('p%d' % pi,
                        rng.randint(opened, max(opened, horizon - 1)),
                        rng.randint(opened + 1, horizon))
                  for pi in range(rng.randint(0, 3))
                  if opened < horizon]
        pauses = [p for p in pauses if p['from_tick'] < p['to_tick']]
        checkpoints = [checkpoint('t%d' % ti, rng.randint(opened, horizon))
                       for ti in range(rng.randint(0, 5))]
        cases.append(case('case%d' % ki,
                          rng.choice(calendars)['id'], opened,
                          rng.randint(0, 8), pauses, checkpoints))
    return document(horizon, calendars, cases)


# ---------------------------------------------------------------------------
# Boundary acceptance: due endpoints, empty sets, budget 0, horizon edges,
# period wrap, ordering invariance.
# ---------------------------------------------------------------------------

class BoundaryTest(unittest.TestCase):
    def test_budget_exhausted_at_open_segment_right_end(self):
        # Window [0,4) then a long closed stretch; budget 4 must expire
        # exactly at tick 4, the right end of the open segment.
        doc = document(20, [
            calendar('C', 10, [[0, 4]], [exception('c', 'closed', 4, 19)])],
            [case('K', 'C', 0, 4, pauses=[pause('p', 6, 9)],
                  checkpoints=[checkpoint('end', 4)])])
        rows = {row['id']: row for row in analyze(doc)['cases']}
        self.assertEqual(rows['K']['due'], 4)
        self.assertEqual(rows['K']['checkpoints'][0]['elapsed'], 4)
        self.assertTrue(rows['K']['checkpoints'][0]['met'])

    def test_due_does_not_skip_pause_plateau_to_next_open(self):
        # Budget reached at tick 4; a pause starts exactly at 4.  Due is
        # still 4, never the next open tick after the pause.
        doc = document(20, [calendar('C', 10, [[0, 5]], [])],
                       [case('K', 'C', 0, 4,
                             pauses=[pause('p', 4, 9)], checkpoints=[])])
        self.assertEqual(analyze(doc)['cases'][0]['due'], 4)

    def test_pause_ending_exactly_at_due(self):
        # Pause [2,6) stops two open ticks; at tick 6 only 2 ticks are
        # accrued and two more open ticks are needed, so the earliest
        # due tick is 8.  The pause's right end does not itself count.
        doc = document(20, [calendar('C', 10, [[0, 10]], [])],
                       [case('K', 'C', 0, 4,
                             pauses=[pause('p', 2, 6)], checkpoints=[])])
        row = analyze(doc)['cases'][0]
        self.assertEqual(row['due'], 8)
        self.assertEqual(row, oracle.expected_case(doc, doc['cases'][0],
                                                   20))
        # A checkpoint exactly at the pause end sees the plateau value.
        doc['cases'][0]['checkpoints'] = [checkpoint('end', 6)]
        self.assertEqual(
            analyze(doc)['cases'][0]['checkpoints'][0]['elapsed'], 2)

    def test_empty_open_set_means_null_due_unless_budget_zero(self):
        doc = document(10, [calendar('EMPTY', 7, [], [])],
                       [case('K', 'EMPTY', 0, 1, [],
                             [checkpoint('a', 0), checkpoint('b', 10)])])
        row = analyze(doc)['cases'][0]
        self.assertIsNone(row['due'])
        self.assertTrue(all(item['elapsed'] == 0
                            for item in row['checkpoints']))
        self.assertFalse(row['checkpoints'][1]['met'])

    def test_budget_zero_due_is_opened_at_even_when_closed(self):
        doc = document(10, [
            calendar('C', 10, [], [exception('c', 'closed', 0, 10)])],
            [case('K', 'C', 3, 0, [], [checkpoint('a', 3)])])
        row = analyze(doc)['cases'][0]
        self.assertEqual(row['due'], 3)
        self.assertEqual(row['checkpoints'][0]['remaining'], 0)
        self.assertTrue(row['checkpoints'][0]['met'])

    def test_due_exactly_at_horizon(self):
        # Every tick open; budget equals the available window length.
        doc = document(10, [calendar('C', 10, [[0, 10]], [])],
                       [case('K', 'C', 0, 10, [], [checkpoint('h', 10)])])
        row = analyze(doc)['cases'][0]
        self.assertEqual(row['due'], 10)
        self.assertEqual(row['checkpoints'][0]['elapsed'], 10)

    def test_horizon_zero_and_opened_at_horizon(self):
        doc = document(0, [calendar('C', 3, [[0, 2]], [])],
                       [case('K', 'C', 0, 0, [], [checkpoint('z', 0)])])
        row = analyze(doc)['cases'][0]
        self.assertEqual(row['due'], 0)
        self.assertEqual(row['checkpoints'][0],
                         {'id': 'z', 'at_tick': 0, 'elapsed': 0,
                          'remaining': 0, 'met': True})

    def test_entirely_non_business_horizon(self):
        doc = document(8, [
            calendar('C', 4, [[0, 4]],
                     [exception('c', 'closed', 0, 8)])],
            [case('K', 'C', 0, 2, [], [checkpoint('e', 8)])])
        row = analyze(doc)['cases'][0]
        self.assertIsNone(row['due'])
        self.assertEqual(row['checkpoints'][0]['elapsed'], 0)

    def test_period_boundary_wrapping_windows(self):
        # Windows wrap only via explicit split; here two windows touch
        # the period ends and merge across the boundary as a set.
        doc = document(25, [
            calendar('C', 5, [[0, 2], [3, 5]],
                     [exception('o', 'open', 6, 7)])],
            [case('K', 'C', 0, 9, [],
                  [checkpoint('a', 5), checkpoint('b', 10),
                   checkpoint('c', 25)])])
        oracle.solve(copy.deepcopy(doc))
        row = analyze(doc)['cases'][0]
        # 4 open ticks per period; check oracle agrees on due too.
        self.assertEqual(row, oracle.expected_case(doc, doc['cases'][0], 25))

    def test_overlapping_adjacent_duplicate_windows_count_once(self):
        doc = document(10, [
            calendar('C', 5, [[0, 3], [0, 2], [2, 3], [1, 4]], [])],
            [case('K', 'C', 0, 4, [], [checkpoint('a', 5)])])
        row = analyze(doc)['cases'][0]
        # Union is [0,4): 4 open ticks in the first period.
        self.assertEqual(row['checkpoints'][0]['elapsed'], 4)

    def test_checkpoint_duplicate_tick_and_unsorted_input(self):
        doc = document(10, [calendar('C', 5, [[0, 5]], [])],
                       [case('K', 'C', 0, 2, [],
                             [checkpoint('z', 10), checkpoint('a', 2),
                              checkpoint('m', 10)])])
        row = analyze(doc)['cases'][0]
        self.assertEqual([item['id'] for item in row['checkpoints']],
                         ['a', 'm', 'z'])

    def test_elapsed_may_exceed_budget_remaining_floored(self):
        doc = document(10, [calendar('C', 10, [[0, 10]], [])],
                       [case('K', 'C', 0, 3, [], [checkpoint('a', 7)])])
        item = analyze(doc)['cases'][0]['checkpoints'][0]
        self.assertEqual(item['elapsed'], 7)
        self.assertEqual(item['remaining'], 0)
        self.assertTrue(item['met'])

    def test_met_is_json_boolean_not_int(self):
        doc = document(5, [calendar('C', 5, [[0, 5]], [])],
                       [case('K', 'C', 0, 1, [], [checkpoint('a', 5)])])
        text = json.dumps(analyze(doc)['cases'][0]['checkpoints'][0])
        self.assertIn('"met": true', text)
        self.assertIsInstance(analyze(doc)['cases'][0]
                              ['checkpoints'][0]['met'], bool)


# ---------------------------------------------------------------------------
# Large constructed example with a provable closed-form expectation.
# ---------------------------------------------------------------------------

class LargeConstructedTest(unittest.TestCase):
    def test_sparse_multperiod_overlapping_exceptions_many_checks(self):
        # Period 1000; only window [100,200) is open => 100 ticks per
        # period.  Horizon 1,000,000 = 1000 periods, so the raw periodic
        # total is 1000 * 100 = 100,000 ticks.
        horizon = 1_000_000
        period = 1000
        windows = [[100, 200]]
        exceptions = [
            # Closed over whole window of periods 2 and 3 (ticks
            # 2100..3200 cover [2100,2200) and [3100,3200)).
            exception('closed-block', 'closed', 2050, 3250),
            # Open exceptions that mostly land on already-closed or
            # already-open ticks; only [500,600) of period 0 is new.
            exception('open-new', 'open', 500, 600),
            exception('open-overlap', 'open', 2100, 2150),
            exception('open-dup', 'open', 550, 650)]
        cal = calendar('C', period, windows, exceptions)

        # Case 1: no pauses.  Periodic total 100,000; closed removes the
        # two windows = 200 ticks; open-new adds [500,600)=100 ticks,
        # open-dup overlaps it adding [600,650)=50 more; open-overlap is
        # swallowed by the closed block.
        # Effective total = 100000 - 200 + 150 = 99950.
        checkpoints = [checkpoint('cp%05d' % i, i * 137)
                       for i in range(200)]  # 200 distinct ticks
        case_one = case('ONE', 'C', 0, 99950, [], checkpoints)

        # Case 2: a pause covering the new open region and part of the
        # closed block.  The open ticks [500,650) are paused (150 ticks)
        # and the closed part removes nothing extra.  Its total is the
        # case-one total minus the 150 genuinely-open paused ticks.
        case_two = case('TWO', 'C', 0, 500,
                        [pause('p', 480, 3300)],
                        [checkpoint('q%05d' % i, i * 211 + 1)
                         for i in range(150)])
        doc = document(horizon, [cal], [case_two, case_one])

        result = {row['id']: row for row in analyze(doc)['cases']}
        one = result['ONE']
        two = result['TWO']

        # The last effective open tick is the end of the final period's
        # window [999100, 999200); everything up to the horizon is
        # closed, so the budget equal to the full open count is reached
        # exactly at tick 999200, the earliest endpoint -- never later.
        earliest_due = (horizon // period - 1) * period + 200
        probe_one = analyze(document(
            horizon, [cal],
            [case('PROBE', 'C', 0, 99950, [],
                  [checkpoint('h', horizon)])]))['cases'][0]
        self.assertEqual(probe_one['checkpoints'][0]['elapsed'], 99950)
        self.assertEqual(probe_one['due'], earliest_due)
        self.assertEqual(one['due'], earliest_due)
        self.assertLess(earliest_due, horizon)

        # Case 2: a pause [480,3300) covers the 150 new open ticks in
        # period 0 and the whole 100-tick periodic window [1100,1200),
        # while its closed-block ticks were never open: 250 genuinely
        # open ticks are paused, so the horizon accrual is
        # 99950 - 250 = 99700 and a budget of 500 is still met.
        probe_two = analyze(document(
            horizon, [cal],
            [case('PROBE2', 'C', 0, 500, [pause('p', 480, 3300)],
                  [checkpoint('h', horizon)])]))['cases'][0]
        self.assertEqual(probe_two['checkpoints'][0]['elapsed'], 99700)
        self.assertTrue(probe_two['checkpoints'][0]['met'])
        self.assertEqual(two['due'], probe_two['due'])
        # Many distinct checkpoints are all answered and stay ordered.
        self.assertEqual(len(two['checkpoints']), 150)
        self.assertEqual([item['id'] for item in two['checkpoints']],
                         sorted(item['id'] for item in two['checkpoints']))

        # Determinism under full array reshuffling (digest aside).
        shuffled = copy.deepcopy(doc)
        rng = random.Random(99)
        rng.shuffle(shuffled['cases'])
        rng.shuffle(shuffled['calendars'][0]['exceptions'])
        rng.shuffle(shuffled['calendars'][0]['windows'])
        self.assertEqual(analyze(shuffled), analyze(doc))

    def test_period_one_huge_horizon_uses_no_expansion(self):
        # period == 1 with a 10^12 horizon: must finish instantly via
        # the closed formula; effectively always open.
        huge = TICK_LIMIT
        doc = document(huge, [calendar('C', 1, [[0, 1]], [])],
                       [case('K', 'C', 0, 123456789, [],
                             [checkpoint('a', huge)])])
        row = analyze(doc)['cases'][0]
        self.assertEqual(row['checkpoints'][0]['elapsed'], huge)
        self.assertEqual(row['due'], 123456789)

    def test_closed_precedence_over_open_exception_huge_span(self):
        huge = TICK_LIMIT
        doc = document(huge, [
            calendar('C', 1, [[0, 1]], [
                exception('o', 'open', 0, huge),
                exception('c', 'closed', 0, huge)])],
            [case('K', 'C', 0, 1, [], [checkpoint('h', huge)])])
        row = analyze(doc)['cases'][0]
        self.assertEqual(row['checkpoints'][0]['elapsed'], 0)
        self.assertIsNone(row['due'])


class ScaleStressTest(unittest.TestCase):
    """Many calendars/cases with a period-1 calendar and huge spans.

    The instance is far smaller than the absolute input caps but still
    forces every closed-form path: a period-1 calendar whose exceptions
    span 10^12 must not expand into one billion periods, and thousands
    of cases share calendars while carrying independent pause unions.
    """

    def test_many_calendars_cases_period_one_huge_spans(self):
        rng = random.Random(2026)
        huge = TICK_LIMIT
        calendars = []
        for ci in range(8):
            period = 1 if ci % 2 == 0 else 97
            windows = [[0, 1]] if period == 1 else [[10, 40], [60, 90]]
            exceptions = []
            for ei in range(40):
                low = rng.randint(0, huge - 1)
                high = rng.randint(low + 1, min(huge, low + 10 ** 9))
                exceptions.append(exception(
                    'e%03d' % ei,
                    rng.choice(['open', 'closed']), low, high))
            calendars.append(calendar('cal%02d' % ci, period, windows,
                                      exceptions))
        cases = []
        for ki in range(400):
            opened = rng.randint(0, huge)
            pauses = []
            if opened < huge:
                for pi in range(3):
                    low = rng.randint(opened, huge - 1)
                    high = rng.randint(low + 1, huge)
                    pauses.append(pause('p%d' % pi, low, high))
            checkpoints = [checkpoint('c%02d' % ti,
                                      rng.randint(opened, huge))
                           for ti in range(4)]
            cases.append(case('case%04d' % ki,
                              'cal%02d' % (ki % len(calendars)), opened,
                              rng.randint(0, huge), pauses, checkpoints))
        doc = document(huge, calendars, cases)

        start = __import__('time').time()
        result = analyze(doc)
        self.assertLess(__import__('time').time() - start, 10.0)
        self.assertEqual(len(result['cases']), 400)
        for row in result['cases']:
            for item in row['checkpoints']:
                self.assertEqual(item['remaining'],
                                 max(0, row['budget'] - item['elapsed']))
                self.assertEqual(item['met'],
                                 item['elapsed'] >= row['budget'])
                self.assertIsInstance(item['met'], bool)
                self.assertGreaterEqual(item['elapsed'], 0)
        # Same huge-span calendar shared by two differently paused
        # cases: their pauses must not leak into one another.
        shared = document(huge, [calendars[0]], [
            case('a', calendars[0]['id'], 0, 10,
                 [pause('p', 0, huge)], [checkpoint('h', huge)]),
            case('b', calendars[0]['id'], 0, 10, [],
                 [checkpoint('h', huge)])])
        rows = {row['id']: row for row in analyze(shared)['cases']}
        self.assertEqual(rows['a']['checkpoints'][0]['elapsed'], 0)
        self.assertGreater(rows['b']['checkpoints'][0]['elapsed'], 0)


# ---------------------------------------------------------------------------
# Validation: identities, fields, types, bounds, references, namespaces.
# ---------------------------------------------------------------------------

def valid_document():
    return document(12, [
        calendar('C', 5, [[0, 3]], [exception('e1', 'open', 6, 8)])],
        [case('K', 'C', 1, 2, [pause('p1', 1, 4)],
              [checkpoint('t1', 4)])])


class ValidationTest(unittest.TestCase):
    def assert_rejected(self, doc):
        with self.assertRaises((ValueError, TypeError, KeyError)):
            analyze(copy.deepcopy(doc))

    def test_top_level_shape(self):
        for bad in ({}, {'horizon': 12},
                    {'horizon': 12, 'calendars': [], 'cases': [],
                     'extra': 1}, []):
            self.assert_rejected(bad)

    def test_horizon_bounds_and_bool_rejection(self):
        doc = valid_document()
        for bad_horizon in (-1, TICK_LIMIT + 1, 1.5, '10', True, None):
            doc['horizon'] = bad_horizon
            self.assert_rejected(doc)

    def test_calendar_limits_and_duplicates(self):
        doc = valid_document()
        doc['calendars'].append(calendar('C', 5, [[0, 1]], []))
        self.assert_rejected(doc)  # duplicate calendar id
        doc = valid_document()
        doc['calendars'] = []
        self.assert_rejected(doc)  # at least one calendar

    def test_period_bounds(self):
        for bad_period in (0, PERIOD_LIMIT + 1, -1, True, 3.0):
            doc = valid_document()
            doc['calendars'][0]['period'] = bad_period
            self.assert_rejected(doc)

    def test_window_validation(self):
        doc = valid_document()
        for bad in ([[1, 1]], [[3, 1]], [[-1, 2]], [[0, 6]],
                    [[0]], [['a', 2]], [[0, True]], [[]]):
            doc['calendars'][0]['windows'] = bad
            self.assert_rejected(doc)

    def test_exception_validation(self):
        doc = valid_document()
        good = doc['calendars'][0]['exceptions'][0]
        for mutator in (
                lambda e: e.update(kind='shut'),
                lambda e: e.update(from_tick=8),  # from >= to
                lambda e: e.update(to_tick=6),    # from >= to
                lambda e: e.update(from_tick=-1),
                lambda e: e.update(to_tick=13),   # beyond horizon
                lambda e: e.update(kind=True),
                lambda e: e.update(from_tick=1.0)):
            clone = copy.deepcopy(doc)
            mutator(clone['calendars'][0]['exceptions'][0])
            self.assert_rejected(clone)
        # duplicate exception id even with identical content
        clone = copy.deepcopy(doc)
        clone['calendars'][0]['exceptions'].append(
            dict(clone['calendars'][0]['exceptions'][0]))
        self.assert_rejected(clone)
        # unknown field rejected
        clone = copy.deepcopy(doc)
        clone['calendars'][0]['exceptions'][0]['note'] = 'x'
        self.assert_rejected(clone)

    def test_unused_calendar_still_validated(self):
        doc = valid_document()
        doc['calendars'].append(calendar('UNUSED', 4, [[0, 2]],
                                         [exception('bad', 'weird', 0, 1)]))
        self.assert_rejected(doc)

    def test_case_validation_and_references(self):
        doc = valid_document()
        doc['cases'][0]['calendar'] = 'GHOST'
        self.assert_rejected(doc)
        doc = valid_document()
        doc['cases'].append(case('K', 'C', 0, 1, [], []))
        self.assert_rejected(doc)  # duplicate case id
        doc = valid_document()
        doc['cases'][0]['opened_at'] = 13  # beyond horizon
        self.assert_rejected(doc)
        doc = valid_document()
        doc['cases'][0]['budget'] = -1
        self.assert_rejected(doc)
        doc = valid_document()
        doc['cases'][0]['budget'] = True
        self.assert_rejected(doc)

    def test_pause_validation_and_duplicate_identity(self):
        doc = valid_document()
        doc['cases'][0]['pauses'].append(pause('p1', 5, 6))  # dup id
        self.assert_rejected(doc)
        for low, high in ((0, 4), (4, 4), (12, 13), (4, 3)):
            clone = copy.deepcopy(valid_document())
            clone['cases'][0]['pauses'] = [pause('p', low, high)]
            self.assert_rejected(clone)
        # A pause ending exactly at the horizon is legal and accepted.
        clone = copy.deepcopy(valid_document())
        clone['cases'][0]['pauses'] = [pause('p', 11, 12)]
        analyze(clone)

    def test_checkpoint_validation(self):
        doc = valid_document()
        doc['cases'][0]['checkpoints'].append(checkpoint('t1', 12))
        self.assert_rejected(doc)  # duplicate checkpoint id
        doc = valid_document()
        doc['cases'][0]['checkpoints'][0]['at_tick'] = 0  # < opened_at
        self.assert_rejected(doc)
        doc = valid_document()
        doc['cases'][0]['checkpoints'][0]['at_tick'] = 13
        self.assert_rejected(doc)

    def test_identifier_namespaces_are_independent(self):
        # Same text id is legal across cases and across pause/checkpoint
        # namespaces and calendar exception ids.
        doc = document(10, [
            calendar('SAME', 4, [[0, 2]], [exception('x', 'open', 0, 1)])],
            [case('SAME', 'SAME', 0, 1,
                  [pause('x', 0, 1)], [checkpoint('x', 1)]),
             case('other', 'SAME', 0, 1,
                  [pause('x', 0, 1)], [checkpoint('x', 1)])])
        analyze(doc)  # must not raise

    def test_blank_and_overlong_identifiers_rejected(self):
        for bad_id in ('', ' ', ' id', 'id ', 'x' * 129):
            doc = valid_document()
            doc['cases'][0]['id'] = bad_id
            self.assert_rejected(doc)

    def test_bool_never_accepted_as_integer_on_any_field(self):
        # bool must be rejected at every integer slot even where True
        # would numerically equal 1 and False would equal 0.
        mutations = [
            lambda d: d.update(horizon=True),
            lambda d: d['calendars'][0].update(period=True),
            lambda d: d['calendars'][0]['windows'].__setitem__(0, [0, True]),
            lambda d: d['calendars'][0]['exceptions'][0]
            .update(from_tick=False),
            lambda d: d['calendars'][0]['exceptions'][0]
            .update(to_tick=True),
            lambda d: d['cases'][0].update(opened_at=False),
            lambda d: d['cases'][0].update(budget=False),
            lambda d: d['cases'][0]['pauses'][0].update(from_tick=True),
            lambda d: d['cases'][0]['pauses'][0].update(to_tick=True),
            lambda d: d['cases'][0]['checkpoints'][0]
            .update(at_tick=True)]
        for mutate in mutations:
            clone = copy.deepcopy(valid_document())
            mutate(clone)
            self.assert_rejected(clone)
        # And bool is not accepted as an identifier/string field either.
        clone = copy.deepcopy(valid_document())
        clone['calendars'][0]['id'] = True
        self.assert_rejected(clone)


# ---------------------------------------------------------------------------
# Command, safety, digest stability and the documented API.
# ---------------------------------------------------------------------------

class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.document = overlap_document()
        self.source = write_json(self.tmp, 'source', self.document)
        self.output = self.tmp / 'report.json'

    def call(self, source, output):
        return subprocess.run(
            [sys.executable, '-m', 'queue_sla.cli', '--source',
             str(source), '--output', str(output)],
            capture_output=True, text=True, env=ENV)

    def test_success_digest_and_stable_rerun(self):
        result = self.call(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'cases'})
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(self.source.read_bytes())
                         .hexdigest())
        first_bytes = self.output.read_bytes()
        self.assertEqual(self.call(self.source, self.output).returncode, 0)
        self.assertEqual(first_bytes, self.output.read_bytes())

    def test_failure_preserves_old_report_and_clears_temp(self):
        self.output.write_text('prior')
        bad = self.tmp / 'bad.json'
        for body in ('{}', '{"a":1,"a":2}', 'NaN', '[]', '', 'null',
                     '{"horizon":true,"calendars":[],"cases":[]}',
                     '{"horizon":1,"calendars":[],"cases":[],"x":0}'):
            bad.write_text(body)
            self.assertEqual(self.call(bad, self.output).returncode, 2)
            self.assertEqual(self.output.read_text(), 'prior')
        self.assertEqual(
            list(self.tmp.glob('tmp*')), [])  # no half-written temp file

    def test_duplicate_json_keys_rejected(self):
        self.output.write_text('prior')
        bad = self.tmp / 'dup.json'
        bad.write_text('{"horizon": 1, "calendars": '
                       '[{"id":"c","period":1,"windows":[],"exceptions":[]}],'
                       ' "cases": [], "cases": []}')
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_publish_failure_clears_temp_and_keeps_old_report(self):
        # Force the atomic publication itself to fail after a valid
        # analysis: the temp file must be removed and the old report
        # kept, never replaced by a half-written document.
        from queue_sla import cli as cli_module
        real_save = cli_module.save
        self.output.write_text('prior')

        def failing_save(path, report):
            # Mimic the real save's temp-file creation so we can prove
            # the failure path clears the temp file it made.
            import tempfile
            stream = tempfile.NamedTemporaryFile(
                mode='w', dir=Path(path).parent, delete=False)
            stream.write('half')
            stream.close()
            raise OSError('simulated publication failure: ' + stream.name)

        cli_module.save = failing_save
        try:
            rc = cli_module.main(['--source', str(self.source),
                                  '--output', str(self.output)])
            self.assertEqual(rc, 2)
        finally:
            cli_module.save = real_save
        self.assertEqual(self.output.read_text(), 'prior')

    def test_real_save_clears_temp_when_write_raises(self):
        # Directly exercise the shared atomic publisher: if writing the
        # temp file raises, its finally-block unlinks the partial file.
        from queue_replay import common
        import tempfile
        target = self.tmp / 'atomic.json'
        target.write_text('prior')
        original_factory = tempfile.NamedTemporaryFile

        class FailingStream:
            def __init__(self, *args, **kwargs):
                self.name = original_factory(delete=False,
                                             dir=kwargs.get('dir')).name

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def write(self, data):
                raise OSError('disk full')

            def close(self):
                pass

        tempfile.NamedTemporaryFile = FailingStream
        try:
            with self.assertRaises(OSError):
                common.save(target, {'a': 1})
        finally:
            tempfile.NamedTemporaryFile = original_factory
        self.assertEqual(target.read_text(), 'prior')
        self.assertEqual(list(self.tmp.glob('tmp*')), [])

    def test_unknown_field_everywhere_rejected(self):
        self.output.write_text('prior')
        doc = copy.deepcopy(self.document)
        doc['cases'][0]['checkpoints'][0]['extra'] = 1
        bad = write_json(self.tmp, 'extra-field', doc)
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_output_path_safety(self):
        before = self.source.read_bytes()
        self.assertEqual(self.call(self.source, self.source).returncode, 2)
        self.assertEqual(self.source.read_bytes(), before)
        link_path = self.tmp / 'link.json'
        link_path.symlink_to(self.source)
        self.assertEqual(self.call(self.source, link_path).returncode, 2)
        self.assertEqual(self.source.read_bytes(), before)
        hard = self.tmp / 'hard.json'
        os.link(self.source, hard)
        self.assertEqual(self.call(self.source, hard).returncode, 2)
        self.assertEqual(self.source.read_bytes(), before)

    def test_oversized_input_rejected(self):
        huge = self.tmp / 'huge.json'
        huge.write_bytes(b' ' * 8_000_001)
        self.output.write_text('prior')
        self.assertEqual(self.call(huge, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_shuffled_inputs_byte_identical_except_digest(self):
        first = self.tmp / 'first.json'
        self.assertEqual(self.call(self.source, first).returncode, 0)
        shuffled_doc = copy.deepcopy(self.document)
        rng = random.Random(3)
        rng.shuffle(shuffled_doc['cases'])
        rng.shuffle(shuffled_doc['calendars'])
        rng.shuffle(shuffled_doc['calendars'][0]['windows'])
        rng.shuffle(shuffled_doc['calendars'][0]['exceptions'])
        for case_row in shuffled_doc['cases']:
            rng.shuffle(case_row['pauses'])
            rng.shuffle(case_row['checkpoints'])
        shuffled = write_json(self.tmp, 'shuffled', shuffled_doc)
        second = self.tmp / 'second.json'
        self.assertEqual(self.call(shuffled, second).returncode, 0)
        one = json.loads(first.read_text())
        two = json.loads(second.read_text())
        one.pop('source_sha256')
        two.pop('source_sha256')
        self.assertEqual(one, two)


class ApiAndFixtureTest(unittest.TestCase):
    def test_run_api_contract(self):
        source_path = ROOT / 'fixtures' / 'sla' / 'source.json'
        report = sla_run(source_path)
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'cases'})
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source_path.read_bytes())
                         .hexdigest())

    def test_analyze_api_contract_and_exceptions(self):
        result = analyze(valid_document())
        self.assertEqual(set(result), {'cases'})
        with self.assertRaises((ValueError, TypeError, KeyError)):
            analyze({'horizon': 'nope'})

    def test_fixture_matches_expected_report_and_oracle(self):
        tmp = Path(tempfile.mkdtemp())
        output = tmp / 'report.json'
        source_path = ROOT / 'fixtures' / 'sla' / 'source.json'
        result = subprocess.run(
            [sys.executable, '-m', 'queue_sla.cli', '--source',
             str(source_path), '--output', str(output)],
            capture_output=True, text=True, env=ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(),
                         (ROOT / 'fixtures' / 'sla' /
                          'expected-report.json').read_text())
        doc = json.loads(source_path.read_text())
        self.assertEqual(json.loads(output.read_text())['cases'],
                         oracle.solve(doc)['cases'])

    def test_standard_library_only(self):
        allowed = {
            'bisect', 'argparse', 'hashlib', 'json', 'sys', 'pathlib',
            'queue_cost', 'queue_planner', 'queue_replay', 'queue_sla'}
        package = ROOT / 'src' / 'queue_sla'
        for path in package.glob('*.py'):
            text = path.read_text()
            self.assertNotIn('sqlite3', text, path)
            self.assertNotIn('socket', text, path)
            for line in text.splitlines():
                if line.startswith('import '):
                    root = line.split()[1].split('.')[0]
                    self.assertIn(root, allowed, line)
                elif line.startswith('from '):
                    target = line.split()[1]
                    if target.startswith('.'):
                        continue  # intra-package relative import
                    root = target.split('.')[0]
                    self.assertIn(root, allowed, line)


if __name__ == '__main__':
    unittest.main()
