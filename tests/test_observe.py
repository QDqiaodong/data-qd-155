"""Tests for queue_observe: strict log parsing, cross-clock feasibility,
tight interval witnesses and conflict-cycle evidence.

Every interval/cycle assertion is checked against ``observe_oracle``, an
independent brute-force implementation that never imports the production
feasibility solver.
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

import observe_oracle as oracle
from queue_replay.cli import run as replay_run
from queue_observe.report import observe

ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def event(event_id, job, kind, clock, tick, server=None, service_seq=None):
    return {'event_id': event_id, 'job': job, 'kind': kind, 'clock': clock,
            'tick': tick, 'server': server, 'service_seq': service_seq}


def clock(clock_id, low, high):
    return {'id': clock_id, 'min_offset': low, 'max_offset': high}


def replay_document(servers, jobs, tmp):
    source = Path(tmp) / ('source-%d.json' % random.randrange(10 ** 9))
    source.write_text(json.dumps({'servers': servers, 'jobs': jobs}))
    return replay_run(source)


def write_json(directory, name, document):
    path = Path(directory) / (name + '-%d.json' % random.randrange(10 ** 9))
    path.write_text(json.dumps(document, sort_keys=True))
    return path


def simple_job_log(jobs, clocks=None, calibrations=(), queries=(),
                   reference='REF', servers=2):
    """Three same-clock events per job, trivial by default."""
    clocks = clocks or [clock('REF', 0, 0)]
    events = []
    for index, job in enumerate(jobs):
        base = index * 10
        events.extend([
            event('e%d-a' % index, job['id'], 'arrival', 'REF', base),
            event('e%d-s' % index, job['id'], 'start', 'REF', base + 1,
                  job['server'], job['seq']),
            event('e%d-f' % index, job['id'], 'finish', 'REF',
                  base + 1 + job['service'], job['server'], job['seq']),
        ])
    return {'reference_clock': reference, 'clocks': clocks,
            'events': events, 'calibrations': list(calibrations),
            'queries': list(queries)}


class ObservationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.jobs = [
            {'id': 'A', 'arrival': 0, 'service': 4},
            {'id': 'B', 'arrival': 0, 'service': 3},
            {'id': 'C', 'arrival': 2, 'service': 1},
        ]
        self.baseline = replay_document(2, self.jobs, self.tmp)

    def baseline_path(self, document=None):
        return write_json(self.tmp, 'baseline', document or self.baseline)

    def matching_baseline(self, log):
        """A self-consistent replay baseline containing every observed job."""
        job_ids = sorted({item['job'] for item in log['events']})
        servers = max([item['server'] for item in log['events']
                       if item['server'] is not None] or [0]) + 1
        jobs = [{'id': job_id, 'arrival': 0, 'service': 1}
                for job_id in job_ids]
        return replay_document(servers, jobs, self.tmp), servers

    def check_against_oracle(self, log, servers=None):
        baseline, observed_servers = self.matching_baseline(log)
        servers = servers or observed_servers
        report = observe(log, 'a' * 64, baseline, 'b' * 64)
        feasible = oracle.enumerate_assignments(log, servers)
        if report['feasible']:
            self.assertTrue(feasible)
            expected = oracle.expected_bounds(log, servers,
                                              log['queries'])
            self.assertEqual([q['id'] for q in report['queries']],
                             sorted(q['id'] for q in log['queries']))
            for item in report['queries']:
                self.assertEqual((item['min'], item['max']),
                                 expected[item['id']])
                oracle.verify_witness(log, item['min_offsets'], servers,
                                      item['job'], item['metric'], item['min'])
                oracle.verify_witness(log, item['max_offsets'], servers,
                                      item['job'], item['metric'], item['max'])
                predicted = oracle.expected_predicted(baseline,
                                                      item['job'], item['metric'])
                self.assertEqual(item['predicted'], predicted)
                self.assertEqual(item['delta_min'], item['min'] - predicted)
                self.assertEqual(item['delta_max'], item['max'] - predicted)
            self.assertNotIn('conflict_cycle', report)
        else:
            self.assertFalse(feasible)
            oracle.verify_cycle(log, report['conflict_cycle'])
            self.assertNotIn('queries', report)
        return report

    # ---- cross-clock tightening -------------------------------------
    def test_cross_clock_offset_cancels(self):
        log = simple_job_log([
            {'id': 'A', 'server': 0, 'seq': 0, 'service': 4},
        ], clocks=[clock('REF', 0, 0), clock('D', -3, 3)],
            queries=[{'id': 'q', 'job': 'A', 'metric': 'service'}])
        log['events'][2]['clock'] = 'D'
        log['events'][2]['tick'] = 5
        report = self.check_against_oracle(log)
        self.assertEqual(report['queries'][0]['min'], 1)
        self.assertEqual(report['queries'][0]['max'], 7)

    def test_occupancy_tightens_each_clock_alone_valid(self):
        # Without the same-server order edge, J1's sojourn would be 0..10;
        # the edge finish(J1) <= start(J2) pins J1's finish (offset A) <= 0,
        # and lifecycle already forces offset A >= 0, so the tight interval
        # collapses to exactly 0.
        def build(other_server):
            return {
                'reference_clock': 'REF',
                'clocks': [clock('REF', 0, 0), clock('A', -10, 10)],
                'events': [
                    event('a1', 'J1', 'arrival', 'REF', 0),
                    event('a2', 'J1', 'start', 'REF', 0, 0, 0),
                    event('a3', 'J1', 'finish', 'A', 0, 0, 0),
                    event('b1', 'J2', 'arrival', 'REF', 0),
                    event('b2', 'J2', 'start', 'REF', 0, other_server,
                          0 if other_server != 0 else 1),
                    event('b3', 'J2', 'finish', 'REF', 2, other_server,
                          0 if other_server != 0 else 1),
                ],
                'calibrations': [],
                'queries': [{'id': 'q', 'job': 'J1', 'metric': 'sojourn'}],
            }
        tightened = self.check_against_oracle(build(0))
        self.assertEqual((tightened['queries'][0]['min'],
                          tightened['queries'][0]['max']), (0, 0))
        unconstrained = oracle.enumerate_assignments(build(1), 2)
        values = {oracle.metric_value(build(1), offsets, 'J1', 'sojourn')
                  for offsets in unconstrained}
        self.assertGreater(max(values), 0)
        # Every single inequality is individually satisfiable on its own; the
        # collapse comes from combining them in one global assignment.
        self.assertIn(10, values)

    def test_same_clock_offset_cancels(self):
        # Every event is read on the same non-reference clock, so the query
        # interval is the raw tick difference; the shared offset cancels.
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('D', -8, 8)],
            'events': [
                event('a1', 'J', 'arrival', 'D', 100),
                event('a2', 'J', 'start', 'D', 107, 0, 0),
                event('a3', 'J', 'finish', 'D', 112, 0, 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'w', 'job': 'J', 'metric': 'wait'},
                        {'id': 's', 'job': 'J', 'metric': 'service'},
                        {'id': 't', 'job': 'J', 'metric': 'sojourn'}],
        }
        report = self.check_against_oracle(log)
        values = {(item['id'], item['min'], item['max'])
                  for item in report['queries']}
        self.assertEqual(values, {('w', 7, 7), ('s', 5, 5), ('t', 12, 12)})
        # The common offset is only bounded by the clock, not narrowed.
        for item in report['queries']:
            self.assertGreaterEqual(item['min_offsets']['D'], -8)
            self.assertLessEqual(item['max_offsets']['D'], 8)

    def test_occupancy_uses_service_seq_not_line_or_tick_order(self):
        # File order and device ticks disagree with the per-server sequence;
        # the adjacent edge still forces B (seq 1) to start after A (seq 0)
        # finished, tightening B's wait.
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('A', -12, -10)],
            'events': [
                event('b-start', 'B', 'start', 'REF', 0, 0, 1),
                event('b-fin', 'B', 'finish', 'REF', 1, 0, 1),
                event('a-fin', 'A', 'finish', 'A', 10, 0, 0),
                event('a-arr', 'A', 'arrival', 'REF', 0),
                event('a-start', 'A', 'start', 'REF', 0, 0, 0),
                event('b-arr', 'B', 'arrival', 'REF', 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'B', 'metric': 'wait'}],
        }
        report = self.check_against_oracle(log)
        # A's finish true time = 10 + off(A) with off(A) in [-12, -10], so
        # true(A finish) is in [-2, 0]; the adjacency edge additionally forces
        # it <= start(B) = 0 and B's wait is exactly 0.
        self.assertEqual(report['queries'][0]['max'], 0)
        reordered = copy.deepcopy(log)
        reordered['events'].reverse()
        baseline, _servers = self.matching_baseline(log)
        again = observe(reordered, 'a' * 64, baseline, 'b' * 64)
        self.assertEqual(json.dumps(report, sort_keys=True),
                         json.dumps(again, sort_keys=True))

    def test_calibrations_chain_across_three_clocks(self):
        # X -> Y -> Z, each calibration bounding a different clock pair; the
        # composed sojourn is exact only through the whole chain.
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('X', -5, 5),
                       clock('Y', -5, 5), clock('Z', -5, 5)],
            'events': [
                event('a1', 'J', 'arrival', 'X', 0),
                event('a2', 'J', 'start', 'Y', 0, 0, 0),
                event('a3', 'J', 'finish', 'Z', 0, 0, 0),
                event('p1', 'P', 'arrival', 'REF', 0),
                event('p2', 'P', 'start', 'REF', 1, 1, 0),
                event('p3', 'P', 'finish', 'REF', 2, 1, 0),
            ],
            'calibrations': [
                {'id': 'c1', 'from_event': 'a1', 'to_event': 'a2',
                 'min_delta': 3, 'max_delta': 4},
                {'id': 'c2', 'from_event': 'a2', 'to_event': 'a3',
                 'min_delta': 2, 'max_delta': 3},
            ],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'sojourn'},
                        {'id': 'q2', 'job': 'J', 'metric': 'wait'}],
        }
        report = self.check_against_oracle(log)
        by_id = {item['id']: item for item in report['queries']}
        self.assertEqual((by_id['q']['min'], by_id['q']['max']), (5, 5))
        self.assertEqual((by_id['q2']['min'], by_id['q2']['max']), (3, 3))

    def test_unqueried_job_constrains_queried_one(self):
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('D', -4, 4)],
            'events': [
                event('a1', 'J', 'arrival', 'REF', 0),
                event('a2', 'J', 'start', 'REF', 0, 0, 0),
                event('a3', 'J', 'finish', 'REF', 1, 0, 0),
                event('u1', 'U', 'arrival', 'D', 4),
                event('u2', 'U', 'start', 'D', 5, 1, 0),
                event('u3', 'U', 'finish', 'D', 6, 1, 0),
            ],
            'calibrations': [
                {'id': 'pin', 'from_event': 'a3', 'to_event': 'u1',
                 'min_delta': 3, 'max_delta': 3},
            ],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'wait'}],
        }
        report = self.check_against_oracle(log)
        self.assertEqual(report['queries'][0]['min'], 0)
        self.assertEqual(report['queries'][0]['max'], 0)

    def test_reference_clock_not_first_and_negative_ticks(self):
        log = {
            'reference_clock': 'MID',
            'clocks': [clock('LATE', 0, 4), clock('MID', 0, 0),
                       clock('NEG', -5, 5), clock('NEGOFF', -5, -2)],
            'events': [
                event('a1', 'J', 'arrival', 'NEG', -3),
                event('a2', 'J', 'start', 'LATE', 0, 0, 0),
                event('a3', 'J', 'finish', 'MID', 4, 0, 0),
                event('u1', 'U', 'arrival', 'MID', 0),
                event('u2', 'U', 'start', 'NEGOFF', 4, 1, 0),
                event('u3', 'U', 'finish', 'NEGOFF', 5, 1, 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'wait'}],
        }
        report = self.check_against_oracle(log)
        # true(arrival) = -3 + off(NEG) in [0, 2]; true(start) = off(LATE).
        self.assertEqual(report['queries'][0]['min'], 0)
        self.assertEqual(report['queries'][0]['max'], 4)
        # U's start time on NEGOFF = 4 + off, in [0, 2]: negative offsets used.
        self.assertTrue(report['feasible'])

    def test_true_time_upper_bound(self):
        big = 10 ** 12
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('D', -10, 10)],
            'events': [
                event('a1', 'J', 'arrival', 'REF', 0),
                event('a2', 'J', 'start', 'REF', 0, 0, 0),
                event('a3', 'J', 'finish', 'D', big, 0, 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'service'}],
        }
        report = self.check_against_oracle(log)
        self.assertEqual(report['queries'][0]['max'], big)

    def test_zero_service_and_simultaneous_adjacent(self):
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0)],
            'events': [
                event('a1', 'Z', 'arrival', 'REF', 0),
                event('a2', 'Z', 'start', 'REF', 3, 0, 0),
                event('a3', 'Z', 'finish', 'REF', 3, 0, 0),
                event('b1', 'W', 'arrival', 'REF', 0),
                event('b2', 'W', 'start', 'REF', 3, 0, 1),
                event('b3', 'W', 'finish', 'REF', 5, 0, 1),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'W', 'metric': 'wait'}],
        }
        report = self.check_against_oracle(log)
        self.assertEqual((report['queries'][0]['min'],
                          report['queries'][0]['max']), (3, 3))

    def test_empty_events_and_queries(self):
        empty = {'reference_clock': 'REF', 'clocks': [clock('REF', 0, 0)],
                 'events': [], 'calibrations': [], 'queries': []}
        empty_baseline = replay_document(1, [], self.tmp)
        report = observe(empty, 'a' * 64, empty_baseline, 'b' * 64)
        self.assertTrue(report['feasible'])
        self.assertEqual(report['queries'], [])

    def test_huge_clock_ranges_with_few_events(self):
        big = 10 ** 12
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('D', -big, big)],
            'events': [
                event('a1', 'J', 'arrival', 'REF', 0),
                event('a2', 'J', 'start', 'D', 0, 0, 0),
                event('a3', 'J', 'finish', 'D', 5, 0, 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'service'}],
        }
        baseline, _servers = self.matching_baseline(log)
        report = observe(log, 'a' * 64, baseline, 'b' * 64)
        self.assertTrue(report['feasible'])
        self.assertEqual((report['queries'][0]['min'],
                          report['queries'][0]['max']), (5, 5))

    # ---- infeasibility ----------------------------------------------
    def test_calibration_cycle_conflict(self):
        # Each calibration is individually satisfiable (see the assertions on
        # its single-calibration subsets), but the triangle of three minimum
        # deltas sums to -2 and is contradictory.
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('X', -2, 2), clock('REF', 0, 0),
                       clock('Y', -2, 2)],
            'events': [
                event('a1', 'J1', 'arrival', 'REF', 0),
                event('a2', 'J1', 'start', 'X', 0, 0, 0),
                event('a3', 'J1', 'finish', 'Y', 0, 0, 0),
                event('b1', 'J2', 'arrival', 'REF', 0),
                event('b2', 'J2', 'start', 'REF', 0, 1, 0),
                event('b3', 'J2', 'finish', 'REF', 2, 1, 0),
            ],
            'calibrations': [
                {'id': 'c1', 'from_event': 'b2', 'to_event': 'a3',
                 'min_delta': 2, 'max_delta': 2},
                {'id': 'c2', 'from_event': 'a3', 'to_event': 'a2',
                 'min_delta': 0, 'max_delta': 0},
                {'id': 'c3', 'from_event': 'a2', 'to_event': 'b2',
                 'min_delta': 0, 'max_delta': 0},
            ],
            'queries': [{'id': 'q', 'job': 'J1', 'metric': 'service'}],
        }
        for dropped in ('c1', 'c2', 'c3'):
            subset = copy.deepcopy(log)
            subset['calibrations'] = [cal for cal in log['calibrations']
                                      if cal['id'] != dropped]
            self.assertTrue(oracle.enumerate_assignments(subset, 2))
        report = self.check_against_oracle(log)
        self.assertFalse(report['feasible'])
        self.assertNotIn('queries', report)
        self.assertGreater(len(report['conflict_cycle']), 0)
        self.assertTrue(any(edge['constraint_id'][0] == 'cal'
                            for edge in report['conflict_cycle']))

    def test_lifecycle_conflict(self):
        # arrival tick 0 on A, finish tick 0 on B, but A is pinned 5 seconds
        # behind B by clock bounds: arrival <= finish is impossible.
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('A', 5, 5),
                       clock('B', 0, 0)],
            'events': [
                event('a1', 'J', 'arrival', 'REF', 0),
                event('a2', 'J', 'start', 'A', 0, 0, 0),
                event('a3', 'J', 'finish', 'B', 0, 0, 0),
            ],
            'calibrations': [],
            'queries': [],
        }
        report = self.check_against_oracle(log)
        self.assertFalse(report['feasible'])
        self.assertNotIn('queries', report)

    # ---- deterministic witnesses ------------------------------------
    def test_input_reorder_and_duplicate_delivery_are_byte_stable(self):
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('D', -2, 2), clock('REF', 0, 0)],
            'events': [
                event('a1', 'J', 'arrival', 'REF', 0),
                event('a2', 'J', 'start', 'D', 0, 0, 0),
                event('a3', 'J', 'finish', 'D', 3, 0, 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'service'},
                        {'id': 'q2', 'job': 'J', 'metric': 'wait'}],
        }
        baseline, _servers = self.matching_baseline(log)
        first = observe(log, 'a' * 64, baseline, 'b' * 64)
        shuffled = copy.deepcopy(log)
        random.Random(5).shuffle(shuffled['events'])
        shuffled['events'].append(copy.deepcopy(shuffled['events'][0]))
        shuffled['clocks'].reverse()
        second = observe(shuffled, 'a' * 64, baseline, 'b' * 64)
        self.assertEqual(json.dumps(first, sort_keys=True),
                         json.dumps(second, sort_keys=True))

    def test_multiple_endpoint_witnesses_all_valid(self):
        log = {
            'reference_clock': 'REF',
            'clocks': [clock('REF', 0, 0), clock('D', -5, 5),
                       clock('E', -5, 5)],
            'events': [
                event('a1', 'J', 'arrival', 'REF', 0),
                event('a2', 'J', 'start', 'D', 0, 0, 0),
                event('a3', 'J', 'finish', 'D', 2, 0, 0),
                event('u1', 'U', 'arrival', 'REF', 0),
                event('u2', 'U', 'start', 'E', 0, 1, 0),
                event('u3', 'U', 'finish', 'E', 1, 1, 0),
            ],
            'calibrations': [],
            'queries': [{'id': 'q', 'job': 'J', 'metric': 'wait'}],
        }
        baseline, _servers = self.matching_baseline(log)
        report = observe(log, 'a' * 64, baseline, 'b' * 64)
        item = report['queries'][0]
        self.assertEqual((item['min'], item['max']), (0, 5))
        oracle.verify_witness(log, item['min_offsets'], 2, 'J', 'wait', 0)
        oracle.verify_witness(log, item['max_offsets'], 2, 'J', 'wait', 5)
        occupied = oracle.enumerate_assignments(log, 2)
        self.assertGreater(len(occupied), 2)
        # Several complete assignments attain the same endpoint.
        self.assertGreater(len([o for o in occupied if o['D'] == 5]), 1)
        self.assertGreater(len([o for o in occupied if o['D'] == 0]), 1)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.baseline = replay_document(
            2, [{'id': 'A', 'arrival': 0, 'service': 3},
                {'id': 'B', 'arrival': 0, 'service': 2}], self.tmp)

    def base_log(self):
        return simple_job_log([
            {'id': 'A', 'server': 0, 'seq': 0, 'service': 3},
            {'id': 'B', 'server': 1, 'seq': 0, 'service': 2},
        ])

    def expect_error(self, log, document=None):
        with self.assertRaises((ValueError, TypeError)):
            observe(log, 'a' * 64, document or self.baseline, 'b' * 64)

    def test_top_level_and_field_edges(self):
        log = self.base_log()
        broken = copy.deepcopy(log)
        broken['extra'] = 1
        self.expect_error(broken)
        missing = copy.deepcopy(log)
        del missing['calibrations']
        self.expect_error(missing)
        for field in ('event_id', 'job', 'kind', 'clock', 'tick', 'server',
                      'service_seq'):
            broken = copy.deepcopy(log)
            del broken['events'][0][field]
            self.expect_error(broken)
        for section in ('events', 'calibrations', 'queries'):
            broken = copy.deepcopy(log)
            broken[section] = 'nope'
            self.expect_error(broken)
        broken = copy.deepcopy(log)
        broken['events'][-1]['kind'] = 'pause'
        self.expect_error(broken)
        broken = copy.deepcopy(log)
        broken['events'][-1]['extra'] = 1
        self.expect_error(broken)
        broken = copy.deepcopy(log)
        broken['calibrations'] = [{'id': 'c', 'from_event': 'e0-a',
                                   'to_event': 'e0-s', 'min_delta': 2,
                                   'max_delta': 1}]
        self.expect_error(broken)
        broken = copy.deepcopy(log)
        broken['queries'] = [{'id': 'q', 'job': 'A', 'metric': 'wait',
                              'extra': 1}]
        self.expect_error(broken)
        for body in (None, [], 'x', 3):
            with self.assertRaises((ValueError, TypeError)):
                observe(body, 'a' * 64, self.baseline, 'b' * 64)

    def test_array_limits(self):
        from queue_observe.log import parse_log
        log = self.base_log()
        self.assertEqual(parse_log(log, 2)['jobs'].keys(), {'A', 'B'})
        too_many = copy.deepcopy(log)
        too_many['clocks'] = [clock('C%03d' % index, 0, 0)
                              for index in range(65)]
        self.expect_error(too_many)
        too_many_queries = copy.deepcopy(log)
        too_many_queries['queries'] = [
            {'id': 'q%03d' % index, 'job': 'A', 'metric': 'wait'}
            for index in range(129)]
        self.expect_error(too_many_queries)
        too_many_cal = copy.deepcopy(log)
        too_many_cal['calibrations'] = [
            {'id': 'c%03d' % index, 'from_event': 'e0-a',
             'to_event': 'e0-s', 'min_delta': 0, 'max_delta': 1}
            for index in range(513)]
        self.expect_error(too_many_cal)
        empty_clocks = copy.deepcopy(log)
        empty_clocks['clocks'] = []
        empty_clocks['reference_clock'] = 'REF'
        self.expect_error(empty_clocks)
        too_many_lines = copy.deepcopy(log)
        too_many_lines['events'] = [copy.deepcopy(log['events'][0])
                                    for _ in range(10001)]
        self.expect_error(too_many_lines)
        too_many_unique = copy.deepcopy(log)
        too_many_unique['events'] = [
            event('x%d' % index, 'A', 'arrival', 'REF', index)
            for index in range(6001)]
        self.expect_error(too_many_unique)
        # Exactly 10000 lines with duplicates collapsing below 6000 is fine.
        at_limit = copy.deepcopy(log)
        filler = log['events'][0]
        at_limit['events'] = [copy.deepcopy(filler)
                              for _ in range(10_000 - len(log['events']))]
        at_limit['events'].extend(copy.deepcopy(log['events']))
        self.assertIn('A', parse_log(at_limit, 2)['jobs'])

    def test_clock_rules(self):
        for mutate in (
                lambda log: log['clocks'][0].update(min_offset=1),
                lambda log: log['clocks'].append(clock('REF', 0, 0)),
                lambda log: log['clocks'].append(clock('@reference', 0, 0)),
                lambda log: log['clocks'][0].update(min_offset=-10 ** 12 - 1),
                lambda log: log['clocks'][0].update(max_offset=1),
                lambda log: log.update(reference_clock='NOPE'),
                lambda log: log['clocks'].clear()):
            log = self.base_log()
            mutate(log)
            self.expect_error(log)

    def test_bool_rejected_as_integer(self):
        for path in ('tick', 'server', 'service_seq'):
            log = self.base_log()
            log['events'][1][path] = True
            self.expect_error(log)
        log = self.base_log()
        log['clocks'][0]['min_offset'] = False
        self.expect_error(log)
        log = self.base_log()
        log['calibrations'] = [{'id': 'c', 'from_event': 'e0-a',
                                'to_event': 'e0-s', 'min_delta': True,
                                'max_delta': 2}]
        self.expect_error(log)

    def test_event_conflicts_and_lifecycle(self):
        log = self.base_log()
        duplicate = copy.deepcopy(log['events'][0])
        duplicate['event_id'] = 'copy'
        log['events'].append(duplicate)
        self.expect_error(log)

        log = self.base_log()
        clash = copy.deepcopy(log['events'][0])
        clash['tick'] = 9
        log['events'].append(clash)
        self.expect_error(log)

        log = self.base_log()
        log['events'].append(event('x', 'A', 'arrival', 'REF', 0))
        self.expect_error(log)

        log = self.base_log()
        log['events'].append(event('x', 'A', 'start', 'REF', 0, 1, 0))
        self.expect_error(log)

        log = self.base_log()
        log['events'][1]['server'] = 7
        self.expect_error(log)

        log = self.base_log()
        log['events'][1]['server'] = None
        self.expect_error(log)

        log = self.base_log()
        log['events'][0]['server'] = 0
        self.expect_error(log)

        log = self.base_log()
        log['events'][1]['service_seq'] = 10 ** 9 + 1
        self.expect_error(log)

    def test_duplicate_event_key_order_does_not_conflict(self):
        log = self.base_log()
        reordered = {key: copy.deepcopy(log['events'][0][key])
                     for key in reversed(list(log['events'][0]))}
        log['events'].append(reordered)
        self.assertTrue(observe(log, 'a' * 64, self.baseline,
                                'b' * 64)['feasible'])

    def test_shared_server_sequence(self):
        log = self.base_log()
        log['events'][3]['service_seq'] = 0  # B reuses A's (0, 0)
        self.expect_error(log)

    def test_missing_and_extra_references(self):
        log = self.base_log()
        log['calibrations'] = [{'id': 'c', 'from_event': 'nope',
                                'to_event': 'e0-s', 'min_delta': 0,
                                'max_delta': 1}]
        self.expect_error(log)
        log = self.base_log()
        log['calibrations'] = [{'id': 'c', 'from_event': 'e0-a',
                                'to_event': 'e0-s', 'min_delta': 0,
                                'max_delta': 1, 'extra': 1}]
        self.expect_error(log)
        log = self.base_log()
        log['queries'] = [{'id': 'q', 'job': 'Z', 'metric': 'wait'}]
        self.expect_error(log)
        log = self.base_log()
        log['queries'] = [{'id': 'q', 'job': 'A', 'metric': 'latency'}]
        self.expect_error(log)
        log = self.base_log()
        log['queries'] = [{'id': 'q', 'job': 'A', 'metric': 'wait'}]
        log['calibrations'] = [
            {'id': 'c', 'from_event': 'e0-a', 'to_event': 'e0-s',
             'min_delta': 9, 'max_delta': 10},
            {'id': 'c', 'from_event': 'e0-a', 'to_event': 'e0-s',
             'min_delta': 0, 'max_delta': 1}]
        self.expect_error(log)

    def test_observed_job_absent_from_baseline(self):
        log = simple_job_log([
            {'id': 'A', 'server': 0, 'seq': 0, 'service': 3},
            {'id': 'B', 'server': 1, 'seq': 0, 'service': 2},
            {'id': 'Z', 'server': 0, 'seq': 1, 'service': 1},
        ])
        self.expect_error(log)

    def test_baseline_must_be_exactly_consistent(self):
        good = copy.deepcopy(self.baseline)
        mutations = [
            lambda doc: doc['result']['jobs'][0].update(start=1),
            lambda doc: doc['result']['jobs'][0].update(server=5),
            lambda doc: doc['result'].update(max_wait=9),
            lambda doc: doc['result']['servers'][0].update(busy_ticks=99),
            lambda doc: doc['result']['jobs'][0].update(wait=True),
            lambda doc: doc['result'].update(mean_wait='2/4'),
            lambda doc: doc['result']['jobs'][0].update(arrival=None),
            lambda doc: doc.update(source_sha256='A' * 64),
            lambda doc: doc.update(extra=1),
            lambda doc: doc['result']['jobs'].reverse(),
            lambda doc: doc['result']['jobs'][0].pop('wait'),
            lambda doc: doc['result']['servers'][0].pop('utilization'),
        ]
        for mutate in mutations:
            document = copy.deepcopy(good)
            mutate(document)
            self.expect_error(self.base_log(), document)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.baseline_doc = replay_document(
            2, [{'id': 'A', 'arrival': 0, 'service': 3},
                {'id': 'B', 'arrival': 0, 'service': 2}], self.tmp)
        self.baseline = write_json(self.tmp, 'baseline', self.baseline_doc)
        self.log = write_json(self.tmp, 'log', simple_job_log([
            {'id': 'A', 'server': 0, 'seq': 0, 'service': 3},
            {'id': 'B', 'server': 1, 'seq': 0, 'service': 2},
        ], queries=[{'id': 'q', 'job': 'A', 'metric': 'wait'}]))

    def call(self, source, baseline, output):
        return subprocess.run(
            [sys.executable, '-m', 'queue_observe.cli', '--source',
             str(source), '--baseline', str(baseline), '--output',
             str(output)], capture_output=True, text=True, env=ENV)

    def test_success_and_digests(self):
        output = self.tmp / 'report.json'
        result = self.call(self.log, self.baseline, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertEqual(report['log_sha256'],
                         hashlib.sha256(self.log.read_bytes()).hexdigest())
        self.assertEqual(report['baseline_sha256'],
                         hashlib.sha256(self.baseline.read_bytes()).hexdigest())
        self.assertEqual(report['baseline_source_sha256'],
                         self.baseline_doc['source_sha256'])
        self.assertTrue(report['feasible'])
        self.assertIn('queries', report)
        first = output.read_bytes()
        self.assertEqual(self.call(self.log, self.baseline, output).returncode, 0)
        self.assertEqual(first, output.read_bytes())

    def test_failure_preserves_old_report(self):
        output = self.tmp / 'report.json'
        output.write_text('prior')
        bad_log = self.tmp / 'bad.json'
        for body in ('{}', '{"a":1,"a":2}', 'NaN', '[]'):
            bad_log.write_text(body)
            self.assertEqual(self.call(bad_log, self.baseline,
                                       output).returncode, 2)
            self.assertEqual(output.read_text(), 'prior')
        broken = json.loads(self.log.read_text())
        broken['events'][0]['tick'] = True
        bad_log.write_text(json.dumps(broken))
        self.assertEqual(self.call(bad_log, self.baseline, output).returncode, 2)
        self.assertEqual(output.read_text(), 'prior')

    def test_infeasible_exits_zero_with_cycle(self):
        log = json.loads(self.log.read_text())
        log['clocks'] = [clock('REF', 0, 0), clock('D', 100, 100)]
        log['events'][0]['clock'] = 'D'  # arrival 100s away, start at REF 1
        log['events'][1]['clock'] = 'REF'
        bad = write_json(self.tmp, 'infeasible', log)
        output = self.tmp / 'report.json'
        result = self.call(bad, self.baseline, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        self.assertFalse(report['feasible'])
        self.assertNotIn('queries', report)
        oracle.verify_cycle(log, report['conflict_cycle'])

    def test_output_path_safety(self):
        before = self.log.read_bytes()
        self.assertEqual(self.call(self.log, self.baseline,
                                   self.log).returncode, 2)
        self.assertEqual(self.log.read_bytes(), before)
        link = self.tmp / 'link.json'
        link.symlink_to(self.log)
        self.assertEqual(self.call(self.log, self.baseline,
                                   link).returncode, 2)
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(self.call(self.log, self.baseline,
                                   self.baseline).returncode, 2)
        self.assertEqual(self.log.read_bytes(), before)
        # Same physical file through a directory-symlink alias.
        alias_dir = self.tmp / 'alias-dir'
        alias_dir.symlink_to(self.tmp)
        self.assertEqual(
            self.call(alias_dir / self.log.name, self.baseline,
                      self.log).returncode, 2)
        # Hard links share an inode even though resolve() differs.
        hard = self.tmp / 'hard-link.json'
        os.link(self.log, hard)
        self.assertEqual(self.call(self.log, self.baseline,
                                   hard).returncode, 2)
        self.assertEqual(self.log.read_bytes(), before)

    def test_oversized_input_rejected(self):
        huge = self.tmp / 'huge.json'
        huge.write_bytes(b' ' * 8_000_001)
        output = self.tmp / 'report.json'
        output.write_text('prior')
        self.assertEqual(self.call(huge, self.baseline, output).returncode, 2)
        self.assertEqual(output.read_text(), 'prior')


class ApiAndIsolationTest(unittest.TestCase):
    def test_run_api_reads_paths_and_digests(self):
        from queue_observe.cli import run as observe_run
        tmp = Path(tempfile.mkdtemp())
        baseline = write_json(
            tmp, 'baseline', replay_document(
                2, [{'id': 'A', 'arrival': 0, 'service': 3}], tmp))
        log = write_json(tmp, 'log', simple_job_log(
            [{'id': 'A', 'server': 0, 'seq': 0, 'service': 3}],
            queries=[{'id': 'q', 'job': 'A', 'metric': 'sojourn'}]))
        report = observe_run(log, baseline)
        self.assertEqual(report['log_sha256'],
                         hashlib.sha256(log.read_bytes()).hexdigest())
        self.assertEqual(report['baseline_sha256'],
                         hashlib.sha256(baseline.read_bytes()).hexdigest())
        self.assertTrue(report['feasible'])
        self.assertEqual(report['queries'][0]['metric'], 'sojourn')

    def test_module_does_not_touch_planner_database(self):
        package = ROOT / 'src' / 'queue_observe'
        for path in package.glob('*.py'):
            text = path.read_text()
            self.assertNotIn('sqlite3', text, path)
            self.assertNotIn('queue_planner.store', text, path)
            self.assertNotIn('queue_planner.service', text, path)


class FixtureAndDeterminismTest(unittest.TestCase):
    def test_shuffled_duplicate_delivery_byte_identical(self):
        import subprocess
        tmp = Path(tempfile.mkdtemp())
        source = ROOT / 'fixtures/observe/observation-log.json'
        baseline = ROOT / 'fixtures/observe/baseline.json'
        expected = ROOT / 'fixtures/observe/expected-report.json'
        first = tmp / 'first.json'
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        result = subprocess.run(
            [sys.executable, '-m', 'queue_observe.cli', '--source',
             str(source), '--baseline', str(baseline), '--output',
             str(first)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        document = json.loads(source.read_text())
        references = json.loads(baseline.read_text())
        self.assertEqual(references['result']['jobs'][0]['id'], 'A')
        self.assertEqual(first.read_text(), expected.read_text())
        report = json.loads(first.read_text())
        self.assertEqual(report['queries'][2]['id'], 'qc-sojourn')
        self.assertEqual((report['queries'][2]['min'],
                          report['queries'][2]['max']), (10, 10))
        self.assertEqual(report['queries'][2]['predicted'], 3)
        self.assertEqual(report['queries'][2]['delta_min'], 7)

        shuffled = copy.deepcopy(document)
        random.Random(21).shuffle(shuffled['events'])
        shuffled['events'].extend(copy.deepcopy(shuffled['events'][:5]))
        shuffled['clocks'].reverse()
        shuffled['calibrations'].reverse()
        random.Random(4).shuffle(shuffled['calibrations'])
        shuffled_path = tmp / 'shuffled.json'
        shuffled_path.write_text(json.dumps(shuffled))
        second = tmp / 'second.json'
        result = subprocess.run(
            [sys.executable, '-m', 'queue_observe.cli', '--source',
             str(shuffled_path), '--baseline', str(baseline), '--output',
             str(second)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        first_bytes = json.loads(first.read_text())
        second_bytes = json.loads(second.read_text())
        first_bytes.pop('log_sha256')
        second_bytes.pop('log_sha256')
        self.assertEqual(json.dumps(first_bytes, sort_keys=True),
                         json.dumps(second_bytes, sort_keys=True))

        conflicting = copy.deepcopy(document)
        clash = copy.deepcopy(conflicting['events'][0])
        clash['tick'] = 1
        conflicting['events'].append(clash)
        conflict_path = tmp / 'conflict.json'
        conflict_path.write_text(json.dumps(conflicting))
        blocked = tmp / 'blocked.json'
        blocked.write_text('prior')
        result = subprocess.run(
            [sys.executable, '-m', 'queue_observe.cli', '--source',
             str(conflict_path), '--baseline', str(baseline), '--output',
             str(blocked)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(blocked.read_text(), 'prior')

    def test_observation_can_diverge_from_prediction(self):
        # The log says B waited 0 but the baseline predicted 0; C's sojourn
        # observed 10 while the baseline predicted 3: deltas are kept as is.
        tmp = Path(tempfile.mkdtemp())
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}
        output = tmp / 'report.json'
        result = subprocess.run(
            [sys.executable, '-m', 'queue_observe.cli', '--source',
             str(ROOT / 'fixtures/observe/observation-log.json'),
             '--baseline', str(ROOT / 'fixtures/observe/baseline.json'),
             '--output', str(output)], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(output.read_text())
        by_id = {item['id']: item for item in report['queries']}
        self.assertEqual(by_id['qc-sojourn']['predicted'], 3)
        self.assertNotEqual(by_id['qc-sojourn']['min'], 3)
        self.assertEqual(by_id['qd-wait']['predicted'], 1)
        self.assertGreater(by_id['qd-wait']['delta_min'], 0)


if __name__ == '__main__':
    unittest.main()
