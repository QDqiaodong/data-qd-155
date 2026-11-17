"""Integration tests for the rolling dispatch lifecycle."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from queue_planner import service, store
from queue_planner.planner import optimize


def job(jid, **over):
    base = dict(id=jid, arrival=0, service=2, deadline=20, value=5,
                eligible=[0, 1])
    base.update(over)
    return base


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'wb.sqlite')
        store.initialize(self.db, 1, 0)
        self.conn = store.connect(self.db)
        self.rid = 0

    def apply(self, op, payload, rev=None):
        self.rid += 1
        if rev is None:
            rev = store.snapshot(self.conn)['revision']
        return service.apply_request(
            self.conn,
            {'request_id': f'r{self.rid}', 'expected_revision': rev,
             'op': op, 'payload': payload},
            f'digest-{self.rid}')

    def test_predicted_release_does_not_auto_complete(self):
        self.apply('load', {'jobs': [
            job('a', service=5, eligible=[0]),
            job('b', arrival=6, service=2, deadline=20, eligible=[0])]})
        self.apply('start', {'jobs': ['a']})  # a runs [0,5), now stays 0
        # Advance the clock past the predicted release without a finish.
        self.apply('observe', {'now': 7, 'finishes': []})
        snap = store.snapshot(self.conn)
        by = {j['id']: j for j in snap['jobs']}
        self.assertEqual(by['a']['status'], 'running')
        # The running server's predicted release is max(now+1, start+svc)=8;
        # any positive suggestion on that server must not start before it.
        report = service.build_report(self.conn)
        for suggestion in report['suggestions']:
            if suggestion['id'] == 'b' and suggestion['server'] == 0:
                self.assertGreaterEqual(suggestion['start'], 8)
        # Critically, the clock advancing alone did not complete job a.
        self.assertEqual(by['a']['actual_finish'], None)
        # Observing the finish (late) releases the floor and replans.
        self.apply('observe',
                   {'now': 8,
                    'finishes': [{'id': 'a', 'actual_finish': 6}]})
        snap2 = store.snapshot(self.conn)
        self.assertEqual(
            {j['id']: j['status'] for j in snap2['jobs']}['a'], 'done')
        plan = optimize(snap2['now'], snap2['servers'],
                        [j for j in snap2['jobs']
                         if j['status'] == 'pending'],
                        {j['server']: (j['actual_start'],
                                       j['service'])
                         for j in snap2['jobs']
                         if j['status'] == 'running'})
        # b can now be scheduled at or after the observed finish.
        if plan['suggestions']:
            self.assertGreaterEqual(
                min(s['start'] for s in plan['suggestions']), 6)

    def test_start_uses_current_revision_plan(self):
        self.apply('load', {'jobs': [
            job('p', service=3, eligible=[0]),
            job('z', service=0, eligible=[0])]})
        # The plan at revision 1 must contain both start==now suggestions.
        snap = store.snapshot(self.conn)
        report = service.build_report(self.conn)
        at_now = {s['id'] for s in report['suggestions']
                  if s['start'] == snap['now']}
        self.assertEqual(at_now, {'p', 'z'})
        self.apply('start', {'jobs': ['p', 'z']})
        snap2 = store.snapshot(self.conn)
        by = {j['id']: j for j in snap2['jobs']}
        self.assertEqual(by['p']['status'], 'running')
        self.assertEqual(by['p']['actual_start'], 0)
        self.assertEqual(by['z']['status'], 'done')
        self.assertEqual(by['z']['actual_start'], 0)
        self.assertEqual(by['z']['actual_finish'], 0)
        # Old revision can no longer start anything.
        before = snap2['revision']
        with self.assertRaises(ValueError):
            service.apply_request(
                self.conn,
                {'request_id': 'stale', 'expected_revision': before - 1,
                 'op': 'start', 'payload': {'jobs': ['p']}},
                'stale-digest')
        self.conn.rollback()

    def test_zero_then_positive_same_tick(self):
        self.apply('load', {'jobs': [
            job('z', service=0, eligible=[0]),
            job('p', service=2, eligible=[0])]})
        self.apply('start', {'jobs': ['z', 'p']})
        snap = store.snapshot(self.conn)
        by = {j['id']: j for j in snap['jobs']}
        self.assertEqual(by['z']['status'], 'done')
        self.assertEqual(by['p']['status'], 'running')
        self.assertEqual(by['z']['actual_start'], by['p']['actual_start'])


class ConcurrencyTest(unittest.TestCase):
    def test_optimistic_revision_serializes_writers(self):
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, 'wb.sqlite')
        store.initialize(db, 1, 0)
        # Two independent connections both believe revision is 0.  The first
        # to take the write lock commits; the second's expected_revision then
        # no longer matches and must be rejected with a full rollback.
        conn_a = store.connect(db)
        conn_b = store.connect(db)
        receipt_a = service.apply_request(
            conn_a,
            {'request_id': 'ra', 'expected_revision': 0, 'op': 'load',
             'payload': {'jobs': [job('a', eligible=[0])]}}, 'da')
        self.assertEqual(receipt_a['revision'], 1)
        with self.assertRaises(ValueError):
            service.apply_request(
                conn_b,
                {'request_id': 'rb', 'expected_revision': 0, 'op': 'load',
                 'payload': {'jobs': [job('b', eligible=[0])]}}, 'db')
        conn_b.rollback()
        snap = store.snapshot(store.connect(db))
        self.assertEqual(snap['revision'], 1)
        self.assertEqual([j['id'] for j in snap['jobs']], ['a'])

    def test_begin_immediate_is_exclusive(self):
        import sqlite3
        tmp = tempfile.mkdtemp()
        db = os.path.join(tmp, 'wb.sqlite')
        store.initialize(db, 1, 0)
        holder = store.connect(db)
        store.begin_write(holder)
        waiter = sqlite3.connect(db, timeout=0.1)
        with self.assertRaises(sqlite3.OperationalError):
            waiter.execute('BEGIN IMMEDIATE')
        waiter.rollback()
        holder.rollback()


if __name__ == '__main__':
    import sqlite3  # noqa: F401
    unittest.main()
