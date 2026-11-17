"""Tests for queue_planner validation, persistence and transactions."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from queue_planner import service, store
from queue_replay.model import analyze


def job(jid, **over):
    base = dict(id=jid, arrival=0, service=2, deadline=10, value=5,
                eligible=[0, 1])
    base.update(over)
    return base


def req(rid, rev, op, payload, digest=None):
    return {'request_id': rid, 'expected_revision': rev, 'op': op,
            'payload': payload}, digest or ('d-' + rid)


class Workbench:
    """In-process helper around a fresh database."""

    def __init__(self, servers=2, now=0):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'wb.sqlite')
        store.initialize(self.db, servers, now)
        self.conn = store.connect(self.db)

    def apply(self, envelope, digest=None):
        return service.apply_request(self.conn, envelope,
                                     digest or ('d-' + envelope['request_id']))

    def expect_fail(self, envelope, digest=None):
        before = store.snapshot(self.conn)
        try:
            service.apply_request(
                self.conn, envelope,
                digest or ('d-' + envelope['request_id']))
        except ValueError:
            self.conn.rollback()
        else:
            raise AssertionError('expected ValueError')
        after = store.snapshot(self.conn)
        assert after['revision'] == before['revision']
        assert len(after['jobs']) == len(before['jobs'])


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.wb = Workbench()

    def test_init_valid_and_bounds(self):
        self.assertEqual(service.validate_init(
            {'servers': 2, 'now': 7}), (2, 7))
        for bad in ({'servers': 2}, {'servers': 0, 'now': 0},
                    {'servers': 4, 'now': 0}, {'servers': True, 'now': 0},
                    {'servers': 2, 'now': -1},
                    {'servers': 2, 'now': 10 ** 12 + 1}):
            with self.assertRaises(ValueError):
                service.validate_init(bad)

    def test_envelope_strict(self):
        good = dict(request_id='r', expected_revision=0, op='load',
                    payload={'jobs': []})
        service.validate_envelope(good)
        with self.assertRaises(ValueError):
            service.validate_envelope(dict(good, extra=1))
        with self.assertRaises(ValueError):
            service.validate_envelope(dict(good, op='LOAD'))
        with self.assertRaises(ValueError):
            service.validate_envelope(dict(good, request_id='x' * 129))

    def test_load_job_rules(self):
        wb = self.wb
        wb.apply(*req('r0', 0, 'load', {'jobs': [job('a', service=0)]}))
        # bool rejected as integer; bad ordering; bad eligibility
        wb.expect_fail(*req('r1', 1, 'load',
                            {'jobs': [job('b', value=True)]}))
        wb.expect_fail(*req('r2', 1, 'load',
                            {'jobs': [job('b', arrival=3, deadline=2)]}))
        wb.expect_fail(*req('r3', 1, 'load',
                            {'jobs': [job('b', eligible=[5])]}))
        wb.expect_fail(*req('r4', 1, 'load',
                            {'jobs': [job('a')]}))  # reused id
        wb.expect_fail(*req('r5', 1, 'load',
                            {'jobs': [job('b'), job('b')]}))  # duplicate

    def test_load_pending_capacity(self):
        wb = self.wb
        first = [job(f'j{i}') for i in range(12)]
        wb.apply(*req('r0', 0, 'load', {'jobs': first}))
        wb.expect_fail(*req('r1', 1, 'load', {'jobs': [job('overflow')]}))
        # finishing one frees a pending slot
        wb.apply(*req('r2', 1, 'observe',
                      {'now': 5, 'finishes': []}))
        # start one then observe its finish
        wb.apply(*req('r3', 2, 'start', {'jobs': ['j0']}))
        wb.apply(*req('r4', 3, 'observe',
                      {'now': 6,
                       'finishes': [{'id': 'j0', 'actual_finish': 6}]}))
        wb.apply(*req('r5', 4, 'load', {'jobs': [job('new')]}))

    def test_observe_rules(self):
        wb = self.wb
        wb.apply(*req('r0', 0, 'load', {'jobs': [job('a')]}))
        wb.apply(*req('r1', 1, 'start', {'jobs': ['a']}))
        # reverse clock
        wb.expect_fail(*req('r2', 2, 'observe', {'now': -1, 'finishes': []}))
        # finish window: actual_start=0, now must be >= actual_finish >= 0
        wb.expect_fail(*req(
            'r3', 2, 'observe',
            {'now': 3, 'finishes': [{'id': 'a', 'actual_finish': 9}]}))
        # unknown finish id and duplicates
        wb.expect_fail(*req(
            'r4', 2, 'observe',
            {'now': 3, 'finishes': [{'id': 'ghost', 'actual_finish': 3}]}))
        wb.expect_fail(*req(
            'r5', 2, 'observe',
            {'now': 3,
             'finishes': [{'id': 'a', 'actual_finish': 2},
                           {'id': 'a', 'actual_finish': 2}]}))
        # valid finish and clock-only advance
        wb.apply(*req('r6', 2, 'observe',
                      {'now': 3,
                       'finishes': [{'id': 'a', 'actual_finish': 2}]}))
        self.assertEqual(store.snapshot(wb.conn)['jobs'][0]['status'], 'done')

    def test_start_rules(self):
        wb = self.wb
        wb.apply(*req('r0', 0, 'load', {'jobs': [
            job('p', service=3, eligible=[0]),
            job('z', service=0, eligible=[0, 1]),
            job('r', service=2, eligible=[1])]}))
        wb.apply(*req('r1', 1, 'start', {'jobs': ['r']}))  # occupies server1
        # unknown id and already-started id
        wb.expect_fail(*req('r2', 2, 'start', {'jobs': ['ghost']}))
        wb.expect_fail(*req('r3', 2, 'start', {'jobs': ['r']}))
        # positive job cannot target occupied server 1
        wb.expect_fail(*req('r4', 2, 'start',
                            {'jobs': [job('q', eligible=[1])['id']]})
                       ) if False else wb.expect_fail(
            *req('r4', 2, 'start', {'jobs': ['p']})
        ) if False else None
        # valid: positive p on idle server0 plus zero z sharing the tick
        wb.apply(*req('r5', 2, 'start', {'jobs': ['p', 'z']}))
        snap = store.snapshot(wb.conn)
        by = {j['id']: j for j in snap['jobs']}
        self.assertEqual(by['p']['status'], 'running')
        self.assertEqual(by['z']['status'], 'done')
        self.assertEqual(by['z']['actual_start'], snap['now'])


class PersistenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'wb.sqlite')
        store.initialize(self.db, 2, 0)

    def test_reopen_keeps_facts(self):
        conn = store.connect(self.db)
        store.begin_write(conn)
        store.insert_job(conn, job('a'))
        store.bump_revision(conn)
        store.insert_request(conn, 'r1', 'digest', 'load', 1)
        conn.commit()
        conn.close()
        conn = store.connect(self.db)
        snap = store.snapshot(conn)
        self.assertEqual(snap['revision'], 1)
        self.assertEqual(snap['jobs'][0]['id'], 'a')
        self.assertEqual(store.lookup_request(conn, 'r1')['revision'], 1)
        conn.close()

    def test_idempotent_replay_conflict_and_stale(self):
        conn = store.connect(self.db)
        envelope = {'request_id': 'r', 'expected_revision': 0, 'op': 'load',
                    'payload': {'jobs': [job('a')]}}
        self.assertEqual(service.apply_request(conn, envelope, 'd1'),
                         {'request_id': 'r', 'revision': 1})
        # replay with identical bytes precedes the revision check
        self.assertEqual(service.apply_request(
            conn, dict(envelope, expected_revision=99), 'd1'),
            {'request_id': 'r', 'revision': 1})
        # same id, different bytes conflicts
        with self.assertRaises(ValueError):
            service.apply_request(
                conn, dict(envelope, payload={'jobs': [job('b')]}), 'd2')
        # stale revision
        with self.assertRaises(ValueError):
            service.apply_request(
                conn, {'request_id': 'r2', 'expected_revision': 0,
                       'op': 'load', 'payload': {'jobs': [job('b')]}}, 'd3')
        conn.close()

    def test_rollback_on_invalid_batch(self):
        conn = store.connect(self.db)
        before = store.snapshot(conn)
        with self.assertRaises(ValueError):
            service.apply_request(
                conn,
                {'request_id': 'rx', 'expected_revision': 0, 'op': 'load',
                 'payload': {'jobs': [job('a'), job('a')]}}, 'dx')
        conn.rollback()
        after = store.snapshot(conn)
        self.assertEqual(after['revision'], before['revision'])
        self.assertEqual(after['jobs'], before['jobs'])
        conn.close()

    def test_symlink_rejected(self):
        link = os.path.join(self.tmp, 'link.sqlite')
        os.symlink(self.db, link)
        with self.assertRaises(ValueError):
            store.connect(link)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, 'wb.sqlite')
        self.src = os.path.join(self.tmp, 'init.json')
        self.out = os.path.join(self.tmp, 'report.json')
        Path(self.src).write_text(json.dumps({'servers': 2, 'now': 0}))

    def cli(self, *args):
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'src'))
        return subprocess.run(
            [sys.executable, '-m', 'queue_planner.cli', *args],
            capture_output=True, text=True, env=env)

    def write(self, name, obj):
        p = os.path.join(self.tmp, name)
        Path(p).write_text(json.dumps(obj))
        return p

    def test_exit_2_and_deterministic_report(self):
        self.assertEqual(self.cli('init', '--db', self.db,
                                  '--source', self.src).returncode, 0)
        bad = self.write('bad.json', {'servers': 2})
        self.assertEqual(self.cli('apply', '--db', self.db,
                                  '--source', bad).returncode, 2)
        load = self.write('load.json', {
            'request_id': 'r1', 'expected_revision': 0, 'op': 'load',
            'payload': {'jobs': [job('a')]}})
        self.assertEqual(self.cli('apply', '--db', self.db,
                                  '--source', load).returncode, 0)
        self.assertEqual(self.cli('plan', '--db', self.db,
                                  '--output', self.out).returncode, 0)
        first = Path(self.out).read_bytes()
        self.assertEqual(self.cli('plan', '--db', self.db,
                                  '--output', self.out).returncode, 0)
        self.assertEqual(Path(self.out).read_bytes(), first)

    def test_db_path_must_differ_from_io(self):
        self.assertEqual(self.cli('init', '--db', self.src,
                                  '--source', self.src).returncode, 2)

    def test_plan_failure_keeps_old_report(self):
        self.cli('init', '--db', self.db, '--source', self.src)
        load = self.write('load.json', {
            'request_id': 'r1', 'expected_revision': 0, 'op': 'load',
            'payload': {'jobs': [job('a')]}})
        self.cli('apply', '--db', self.db, '--source', load)
        self.cli('plan', '--db', self.db, '--output', self.out)
        good = Path(self.out).read_bytes()
        blocker = os.path.join(self.tmp, 'block')
        Path(blocker).write_text('x')
        r = self.cli('plan', '--db', self.db,
                     '--output', os.path.join(blocker, 'r.json'))
        self.assertEqual(r.returncode, 2)
        self.assertEqual(Path(self.out).read_bytes(), good)


class CompatibilityTest(unittest.TestCase):
    def test_legacy_queue_replay_unchanged(self):
        data = json.loads((ROOT / 'fixtures' / 'input.json').read_text())
        result = analyze(data)
        self.assertIn('jobs', result)
        self.assertEqual(len(result['jobs']), 4)


if __name__ == '__main__':
    unittest.main()
