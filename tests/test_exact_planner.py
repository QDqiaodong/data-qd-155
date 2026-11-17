"""Exact-planner tests against an independent brute-force oracle."""
import random
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

from queue_planner.planner import optimize
from planner_oracle import brute_force


def make_job(i, rng, servers):
    return {
        'id': f'j{i:02d}',
        'arrival': rng.randrange(0, 12),
        'service': rng.choice([0, 0, 1, 2, 3, 4]),
        'deadline': rng.randrange(8, 28),
        'value': rng.randrange(1, 20),
        'eligible': sorted(set(rng.choices(range(servers),
                                          k=rng.randrange(1, servers + 1)))),
    }


def same(plan, oracle):
    return plan['tuples'] == oracle['tuples'] and \
        plan['value_total'] == oracle['value_total'] and \
        plan['wait_total'] == oracle['wait_total'] and \
        plan['unselected'] == oracle['unselected']


class OracleComparison(unittest.TestCase):
    def test_random_small_fixtures(self):
        rng = random.Random(20261117)
        for _ in range(180):
            servers = rng.randrange(1, 4)
            jobs = [make_job(i, rng, servers)
                    for i in range(rng.randrange(0, 6))]
            now = rng.randrange(0, 6)
            running = {}
            if servers and rng.random() < 0.3:
                sid = rng.randrange(servers)
                running = {sid: (rng.randrange(now + 1),
                                 rng.randrange(1, 6))}
            plan = optimize(now, servers, jobs, running)
            oracle = brute_force(now, servers, jobs, running)
            self.assertTrue(same(plan, oracle),
                            msg=f'\nplan={plan}\noracle={oracle}')

    def test_same_value_different_subsets(self):
        jobs = [
            {'id': 'a', 'arrival': 0, 'service': 6, 'deadline': 6,
             'value': 5, 'eligible': [0]},
            {'id': 'b', 'arrival': 0, 'service': 3, 'deadline': 3,
             'value': 5, 'eligible': [0]},
            {'id': 'c', 'arrival': 3, 'service': 3, 'deadline': 6,
             'value': 5, 'eligible': [0]}]
        plan = optimize(0, 1, jobs, {})
        oracle = brute_force(0, 1, jobs, {})
        self.assertTrue(same(plan, oracle))
        self.assertEqual(plan['value_total'], 10)

    def test_scarce_server_preemption(self):
        jobs = [
            {'id': 'x', 'arrival': 0, 'service': 8, 'deadline': 8,
             'value': 9, 'eligible': [0]},
            {'id': 'y', 'arrival': 0, 'service': 8, 'deadline': 8,
             'value': 9, 'eligible': [0, 1]},
            {'id': 'z', 'arrival': 0, 'service': 8, 'deadline': 8,
             'value': 9, 'eligible': [1]}]
        plan = optimize(0, 2, jobs, {})
        self.assertEqual(plan['value_total'],
                         brute_force(0, 2, jobs, {})['value_total'])

    def test_deliberate_idle_for_future_high_value(self):
        jobs = [
            {'id': 'lo', 'arrival': 0, 'service': 4, 'deadline': 4,
             'value': 1, 'eligible': [0]},
            {'id': 'hi', 'arrival': 3, 'service': 4, 'deadline': 7,
             'value': 100, 'eligible': [0]}]
        plan = optimize(0, 1, jobs, {})
        oracle = brute_force(0, 1, jobs, {})
        self.assertTrue(same(plan, oracle))
        self.assertIn('hi', [s['id'] for s in plan['suggestions']])

    def test_exact_deadline_and_zero_service(self):
        jobs = [
            {'id': 'd', 'arrival': 2, 'service': 5, 'deadline': 7,
             'value': 3, 'eligible': [0]},
            {'id': 'z', 'arrival': 0, 'service': 0, 'deadline': 5,
             'value': 4, 'eligible': [0]},
            {'id': 'p', 'arrival': 0, 'service': 5, 'deadline': 10,
             'value': 4, 'eligible': [0]}]
        plan = optimize(0, 1, jobs, {})
        oracle = brute_force(0, 1, jobs, {})
        self.assertTrue(same(plan, oracle))

    def test_greedy_loses_fixture(self):
        # Greedy by value takes hi (10) and blocks both medium jobs; the
        # joint optimum accepts the two medium jobs for value 12.
        jobs = [
            {'id': 'hi', 'arrival': 0, 'service': 10, 'deadline': 10,
             'value': 10, 'eligible': [0]},
            {'id': 'm1', 'arrival': 0, 'service': 5, 'deadline': 5,
             'value': 6, 'eligible': [0]},
            {'id': 'm2', 'arrival': 5, 'service': 5, 'deadline': 10,
             'value': 6, 'eligible': [0]}]
        plan = optimize(0, 1, jobs, {})
        self.assertEqual(plan['value_total'], 12)
        self.assertEqual(
            plan['tuples'], brute_force(0, 1, jobs, {})['tuples'])

    def test_time_shift_to_ten_power_twelve(self):
        rng = random.Random(31)
        shift = 10 ** 12
        for _ in range(8):
            servers = rng.randrange(1, 4)
            jobs = [make_job(i, rng, servers) for i in range(5)]
            now = rng.randrange(0, 5)
            running = {}
            if rng.random() < 0.4:
                sid = rng.randrange(servers)
                running = {sid: (rng.randrange(now + 1),
                                 rng.randrange(1, 5))}
            base = optimize(now, servers, jobs, running)
            shifted_jobs = [
                dict(j, arrival=j['arrival'] + shift,
                     deadline=j['deadline'] + shift) for j in jobs]
            shifted_running = {
                sid: (start + shift, service)
                for sid, (start, service) in running.items()}
            moved = optimize(now + shift, servers, shifted_jobs,
                             shifted_running)
            self.assertEqual(base['value_total'], moved['value_total'])
            self.assertEqual(base['wait_total'], moved['wait_total'])
            self.assertEqual(
                sorted(base['tuples']),
                sorted((jid, sid, start - shift)
                       for jid, sid, start in moved['tuples']))


if __name__ == '__main__':
    unittest.main()
