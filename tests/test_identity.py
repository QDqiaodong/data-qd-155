"""Tests for queue-identity: point-in-time identity resolution over
multi-source customer master data with validity-interval links.

Every small scenario is cross-checked with ``identity_oracle``, an
independent per-query graph walker that never imports the production
identity engine.  The maximum-scale case is verified against
expectations derived directly from its provable construction
(alternating pair phases, one grand-merge window, final split).
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

import identity_oracle as oracle
from queue_identity.cli import run as identity_run
from queue_identity.model import resolve

ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}

W = 10 ** 9
TICK_LIMIT = 10 ** 12


def source(source_id, priority):
    return {'id': source_id, 'priority': priority}


def record(src, key, display_name=None, email=None, phone=None):
    return {'source': src, 'key': key, 'display_name': display_name,
            'email': email, 'phone': phone}


def link(link_id, left, right, low, high):
    return {'id': link_id, 'left': left, 'right': right,
            'from_tick': low, 'to_tick': high}


def query(query_id, record_ref, at):
    return {'id': query_id, 'record': record_ref, 'at_tick': at}


def document(sources, records, links, queries):
    return {'sources': sources, 'records': records, 'links': links,
            'queries': queries}


def field(value, src, key):
    return {'value': value, 'record': [src, key]}


def write_json(directory, name, doc):
    path = Path(directory) / ('%s-%d.json' % (name, random.randrange(10 ** 9)))
    path.write_text(json.dumps(doc, sort_keys=True))
    return path


def bridge_document():
    """Two clean pairs, one bridging link that quarantines, then heals.

    Side A {(CRM,c1), (APPT,a1)} and side B {(CRM,c2), (HOTLINE,h1)}
    are permanently linked inside [100,500); the bridge a1--h1 is
    active only inside [200,300), putting two CRM records into one
    component.  The two sides have different field winners.
    """
    sources = [source('CRM', 5), source('APPT', 1), source('HOTLINE', 2)]
    records = [
        record('CRM', 'c1', display_name='D-CRM-1', email='e-c1@x'),
        record('APPT', 'a1', display_name='D-APPT-1', phone='p-a1'),
        record('CRM', 'c2', display_name='D-CRM-2', phone='p-c2'),
        record('HOTLINE', 'h1', display_name='D-HOT-1', email='e-h1@x'),
    ]
    links = [
        link('L-A', ['CRM', 'c1'], ['APPT', 'a1'], 100, 500),
        link('L-B', ['CRM', 'c2'], ['HOTLINE', 'h1'], 100, 500),
        link('L-BRIDGE', ['APPT', 'a1'], ['HOTLINE', 'h1'], 200, 300),
    ]
    queries = [  # deliberately out of id order, with a duplicated point
        query('q-bridge-mid', ['CRM', 'c2'], 250),
        query('q-before', ['CRM', 'c1'], 150),
        query('q-bridge-end', ['CRM', 'c1'], 300),
        query('q-bridge-start', ['CRM', 'c1'], 200),
        query('q-post', ['CRM', 'c1'], 500),
        query('q-before-b', ['HOTLINE', 'h1'], 150),
        query('q-bridge-mid-dup', ['CRM', 'c2'], 250),
        query('q-pre', ['CRM', 'c1'], 99),
        query('q-after', ['APPT', 'a1'], 450),
    ]
    return document(sources, records, links, queries)


SIDE_A_FIELDS = {'display_name': field('D-APPT-1', 'APPT', 'a1'),
                 'email': field('e-c1@x', 'CRM', 'c1'),
                 'phone': field('p-a1', 'APPT', 'a1')}
SIDE_B_FIELDS = {'display_name': field('D-HOT-1', 'HOTLINE', 'h1'),
                 'email': field('e-h1@x', 'HOTLINE', 'h1'),
                 'phone': field('p-c2', 'CRM', 'c2')}


def expected_bridge_rows():
    """Hand-computed expectations for every bridge query."""
    return {
        'q-pre': {'canonical_record': ['CRM', 'c1'], 'member_count': 1,
                  'status': 'clean', 'conflict_sources': [],
                  'fields': {'display_name': field('D-CRM-1', 'CRM', 'c1'),
                             'email': field('e-c1@x', 'CRM', 'c1'),
                             'phone': None}},
        'q-before': {'canonical_record': ['APPT', 'a1'], 'member_count': 2,
                     'status': 'clean', 'conflict_sources': [],
                     'fields': SIDE_A_FIELDS},
        'q-before-b': {'canonical_record': ['CRM', 'c2'], 'member_count': 2,
                       'status': 'clean', 'conflict_sources': [],
                       'fields': SIDE_B_FIELDS},
        'q-bridge-start': {'canonical_record': ['APPT', 'a1'],
                           'member_count': 4, 'status': 'quarantined',
                           'conflict_sources': ['CRM'], 'fields': None},
        'q-bridge-mid': {'canonical_record': ['APPT', 'a1'],
                         'member_count': 4, 'status': 'quarantined',
                         'conflict_sources': ['CRM'], 'fields': None},
        'q-bridge-mid-dup': {'canonical_record': ['APPT', 'a1'],
                             'member_count': 4, 'status': 'quarantined',
                             'conflict_sources': ['CRM'], 'fields': None},
        'q-bridge-end': {'canonical_record': ['APPT', 'a1'],
                         'member_count': 2, 'status': 'clean',
                         'conflict_sources': [], 'fields': SIDE_A_FIELDS},
        'q-after': {'canonical_record': ['APPT', 'a1'], 'member_count': 2,
                    'status': 'clean', 'conflict_sources': [],
                    'fields': SIDE_A_FIELDS},
        'q-post': {'canonical_record': ['CRM', 'c1'], 'member_count': 1,
                   'status': 'clean', 'conflict_sources': [],
                   'fields': {'display_name': field('D-CRM-1', 'CRM', 'c1'),
                              'email': field('e-c1@x', 'CRM', 'c1'),
                              'phone': None}},
    }


class ModelTest(unittest.TestCase):
    def check(self, doc):
        """Run production, compare the whole body with the oracle and
        re-verify the contract's structural invariants directly."""
        report = resolve(doc)
        self.assertEqual(set(report), {'queries'})
        self.assertEqual(report, oracle.solve(doc))
        rows = report['queries']
        self.assertEqual([row['id'] for row in rows],
                         sorted(row['id'] for row in rows))
        for row in rows:
            self.assertEqual(set(row), {'id', 'at_tick', 'record',
                                        'canonical_record',
                                        'member_count', 'status',
                                        'conflict_sources', 'fields'})
            self.assertIsInstance(row['record'], list)
            self.assertEqual(len(row['record']), 2)
            self.assertIsInstance(row['canonical_record'], list)
            self.assertEqual(len(row['canonical_record']), 2)
            self.assertGreaterEqual(row['member_count'], 1)
            self.assertIn(row['status'], ('clean', 'quarantined'))
            conflicts = row['conflict_sources']
            self.assertEqual(conflicts, sorted(conflicts))
            if row['status'] == 'clean':
                self.assertEqual(conflicts, [])
                self.assertEqual(set(row['fields']),
                                 {'display_name', 'email', 'phone'})
                for value in row['fields'].values():
                    if value is not None:
                        self.assertEqual(set(value), {'value', 'record'})
                        self.assertEqual(len(value['record']), 2)
            else:
                self.assertNotEqual(conflicts, [])
                self.assertIsNone(row['fields'])
        return report

    # ---- acceptance: bridge quarantine and recovery ------------------
    def test_bridge_quarantine_and_recovery(self):
        report = self.check(bridge_document())
        expected = expected_bridge_rows()
        rows = {row['id']: row for row in report['queries']}
        self.assertEqual(set(rows), set(expected))
        for query_id, want in expected.items():
            row = rows[query_id]
            for key, value in want.items():
                self.assertEqual(row[key], value, '%s.%s' % (query_id, key))
        # The duplicated point query yields an identical business row.
        mid = rows['q-bridge-mid']
        dup = rows['q-bridge-mid-dup']
        self.assertEqual({k: v for k, v in mid.items() if k != 'id'},
                         {k: v for k, v in dup.items() if k != 'id'})
        # The bridge tick boundaries are exact: active at from_tick,
        # gone at to_tick, and the quarantine does not leak past 300.
        self.assertEqual(rows['q-bridge-start']['at_tick'], 200)
        self.assertEqual(rows['q-bridge-start']['status'], 'quarantined')
        self.assertEqual(rows['q-bridge-end']['at_tick'], 300)
        self.assertEqual(rows['q-bridge-end']['status'], 'clean')
        # Field lineage before and after the bridge is identical: the
        # departed members stopped contributing.
        self.assertEqual(rows['q-before']['fields'],
                         rows['q-bridge-end']['fields'])

    # ---- exact boundaries --------------------------------------------
    def test_boundary_ticks_and_same_tick_handover(self):
        # L1 ends exactly at 20, L2 starts exactly at 20.
        doc = document(
            [source('SA', 1), source('SB', 2), source('SC', 3)],
            [record('SA', 'a', display_name='DA'),
             record('SB', 'b', display_name='DB'),
             record('SC', 'c', display_name='DC')],
            [link('L1', ['SA', 'a'], ['SB', 'b'], 10, 20),
             link('L2', ['SB', 'b'], ['SC', 'c'], 20, 30)],
            [query('q9', ['SB', 'b'], 9),
             query('q10', ['SB', 'b'], 10),
             query('q19', ['SB', 'b'], 19),
             query('q20a', ['SA', 'a'], 20),
             query('q20b', ['SB', 'b'], 20),
             query('q29', ['SB', 'b'], 29),
             query('q30', ['SB', 'b'], 30)])
        report = self.check(doc)
        rows = {row['id']: row for row in report['queries']}
        self.assertEqual(rows['q9']['member_count'], 1)   # before from_tick
        self.assertEqual(rows['q10']['member_count'], 2)  # from_tick active
        self.assertEqual(rows['q19']['member_count'], 2)
        # At tick 20 L1 is off and L2 is on: a is alone, b joins c.
        self.assertEqual(rows['q20a']['member_count'], 1)
        self.assertEqual(rows['q20a']['canonical_record'], ['SA', 'a'])
        self.assertEqual(rows['q20b']['member_count'], 2)
        self.assertEqual(rows['q20b']['canonical_record'], ['SB', 'b'])
        self.assertEqual(rows['q29']['member_count'], 2)
        self.assertEqual(rows['q30']['member_count'], 1)  # to_tick inactive

    def test_parallel_links_and_cycle_alternative_path(self):
        # A 4-cycle plus two parallel r0--r1 links; C2 expires early but
        # the cycle keeps an alternative path, and the parallel links
        # never inflate the member count.
        doc = document(
            [source('S0', 0), source('S1', 1), source('S2', 2),
             source('S3', 3)],
            [record('S0', 'r0', display_name='N0'),
             record('S1', 'r1', email='e1@x'),
             record('S2', 'r2', phone='p2'),
             record('S3', 'r3', display_name='N3')],
            [link('C1', ['S0', 'r0'], ['S1', 'r1'], 10, 50),
             link('C2', ['S1', 'r1'], ['S2', 'r2'], 10, 40),
             link('C3', ['S2', 'r2'], ['S3', 'r3'], 10, 50),
             link('C4', ['S3', 'r3'], ['S0', 'r0'], 10, 50),
             link('P1', ['S0', 'r0'], ['S1', 'r1'], 10, 30),
             link('P2', ['S0', 'r0'], ['S1', 'r1'], 10, 60)],
            [query('t25', ['S2', 'r2'], 25),
             query('t35', ['S2', 'r2'], 35),
             query('t45', ['S2', 'r2'], 45),
             query('t55', ['S0', 'r0'], 55),
             query('t55-lone', ['S2', 'r2'], 55),
             query('t65', ['S0', 'r0'], 65)])
        report = self.check(doc)
        rows = {row['id']: row for row in report['queries']}
        all_four = {'display_name': field('N0', 'S0', 'r0'),
                    'email': field('e1@x', 'S1', 'r1'),
                    'phone': field('p2', 'S2', 'r2')}
        for query_id in ('t25', 't35', 't45'):
            # Parallel links and the cycle's alternative path keep all
            # four records in one component, counted once each.
            self.assertEqual(rows[query_id]['member_count'], 4, query_id)
            self.assertEqual(rows[query_id]['status'], 'clean')
            self.assertEqual(rows[query_id]['canonical_record'],
                             ['S0', 'r0'])
            self.assertEqual(rows[query_id]['fields'], all_four)
        # After the real expiry of C1/C3/C4 only the P2 pair survives.
        self.assertEqual(rows['t55']['member_count'], 2)
        self.assertEqual(rows['t55']['canonical_record'], ['S0', 'r0'])
        self.assertEqual(rows['t55']['fields'],
                         {'display_name': field('N0', 'S0', 'r0'),
                          'email': field('e1@x', 'S1', 'r1'),
                          'phone': None})
        # r2 sat on the expired side of the split and is now alone.
        self.assertEqual(rows['t55-lone']['member_count'], 1)
        self.assertEqual(rows['t55-lone']['canonical_record'],
                         ['S2', 'r2'])
        self.assertEqual(rows['t65']['member_count'], 1)

    # ---- key handling -------------------------------------------------
    def test_cross_source_same_key_and_separator_safety(self):
        doc = document(
            [source('SA', 1), source('SB', 2), source('a', 3),
             source('a:b', 4)],
            [record('SA', 'k', display_name='D-SA'),
             record('SB', 'k', display_name='D-SB'),
             record('a', 'b:c', display_name='D-LEFT'),
             record('a:b', 'c', display_name='D-RIGHT')],
            [link('L1', ['SA', 'k'], ['SB', 'k'], 5, 100),
             link('L2', ['a', 'b:c'], ['a:b', 'c'], 5, 100)],
            [query('same-key', ['SB', 'k'], 10),
             query('separator', ['a:b', 'c'], 10)])
        report = self.check(doc)
        rows = {row['id']: row for row in report['queries']}
        # The same key in two sources stays two distinct records.
        self.assertEqual(rows['same-key']['member_count'], 2)
        self.assertEqual(rows['same-key']['canonical_record'], ['SA', 'k'])
        self.assertEqual(rows['same-key']['status'], 'clean')
        # ('a','b:c') and ('a:b','c') must not collide through any
        # separator-joined identity string.
        self.assertEqual(rows['separator']['member_count'], 2)
        self.assertEqual(rows['separator']['canonical_record'],
                         ['a', 'b:c'])
        self.assertEqual(rows['separator']['conflict_sources'], [])

    def test_canonical_key_ignores_priority(self):
        # ZEBRA has the best priority, but ALPHA wins the canonical key
        # by plain string tuple order.
        doc = document(
            [source('ZEBRA', 1), source('ALPHA', 9)],
            [record('ZEBRA', 'z', display_name='D-ZEBRA'),
             record('ALPHA', 'a', display_name='D-ALPHA')],
            [link('L', ['ZEBRA', 'z'], ['ALPHA', 'a'], 0, 10)],
            [query('q', ['ZEBRA', 'z'], 5)])
        report = self.check(doc)
        row = report['queries'][0]
        self.assertEqual(row['canonical_record'], ['ALPHA', 'a'])
        self.assertEqual(row['fields']['display_name'],
                         field('D-ZEBRA', 'ZEBRA', 'z'))

    # ---- governance ---------------------------------------------------
    def test_priority_tie_broken_by_source(self):
        doc = document(
            [source('S1', 7), source('S2', 7), source('S3', 3)],
            [record('S1', 'k1', display_name='D1', email='e1@x'),
             record('S2', 'k2', display_name='D2', email='e2@x',
                    phone='p2'),
             record('S3', 'k3', display_name='D3')],
            [link('L1', ['S1', 'k1'], ['S2', 'k2'], 0, 10),
             link('L2', ['S2', 'k2'], ['S3', 'k3'], 0, 10)],
            [query('q', ['S1', 'k1'], 5)])
        report = self.check(doc)
        fields = report['queries'][0]['fields']
        # display_name: S3 has the strictly best priority.
        self.assertEqual(fields['display_name'], field('D3', 'S3', 'k3'))
        # email: S1 and S2 tie on priority 7, source id decides.
        self.assertEqual(fields['email'], field('e1@x', 'S1', 'k1'))
        # phone: only S2 declares one.
        self.assertEqual(fields['phone'], field('p2', 'S2', 'k2'))

    def test_fields_from_three_sources_and_null_fallback(self):
        doc = document(
            [source('A', 1), source('B', 2), source('C', 3)],
            [record('A', 'a', display_name='DA'),
             record('B', 'b', display_name='DB', email='eb@x'),
             record('C', 'c', email='ec@x', phone='pc'),
             record('A', 'lone')],
            [link('L1', ['A', 'a'], ['B', 'b'], 0, 10),
             link('L2', ['B', 'b'], ['C', 'c'], 0, 10)],
            [query('joined', ['C', 'c'], 5),
             query('isolated', ['A', 'lone'], 5)])
        report = self.check(doc)
        rows = {row['id']: row for row in report['queries']}
        # Each field comes from a different source; the best-priority
        # source A declares no email, so email falls through to B.
        self.assertEqual(rows['joined']['fields'], {
            'display_name': field('DA', 'A', 'a'),
            'email': field('eb@x', 'B', 'b'),
            'phone': field('pc', 'C', 'c')})
        # An isolated record with all-null fields publishes nulls.
        self.assertEqual(rows['isolated']['member_count'], 1)
        self.assertEqual(rows['isolated']['status'], 'clean')
        self.assertEqual(rows['isolated']['fields'],
                         {'display_name': None, 'email': None,
                          'phone': None})

    def test_multiple_conflict_sources(self):
        # SA's two records carry identical field values and still
        # conflict; SB's carry different ones; three sources conflict
        # at once and are reported in string order.
        doc = document(
            [source('SA', 1), source('SB', 2), source('SC', 3),
             source('SD', 4)],
            [record('SA', 'a1', display_name='SAME', email='same@x'),
             record('SA', 'a2', display_name='SAME', email='same@x'),
             record('SB', 'b1', display_name='D-B1'),
             record('SB', 'b2', display_name='D-B2'),
             record('SC', 'c1'),
             record('SC', 'c2'),
             record('SD', 'd1')],
            [link('L1', ['SA', 'a1'], ['SB', 'b1'], 0, 10),
             link('L2', ['SB', 'b1'], ['SC', 'c1'], 0, 10),
             link('L3', ['SC', 'c1'], ['SD', 'd1'], 0, 10),
             link('L4', ['SA', 'a2'], ['SB', 'b2'], 0, 10),
             link('L5', ['SB', 'b2'], ['SC', 'c2'], 0, 10),
             link('L6', ['SC', 'c2'], ['SD', 'd1'], 0, 10)],
            [query('q', ['SD', 'd1'], 5)])
        report = self.check(doc)
        row = report['queries'][0]
        self.assertEqual(row['member_count'], 7)
        self.assertEqual(row['status'], 'quarantined')
        self.assertEqual(row['conflict_sources'], ['SA', 'SB', 'SC'])
        self.assertIsNone(row['fields'])
        self.assertEqual(row['canonical_record'], ['SA', 'a1'])

    def test_quarantine_does_not_pollute_split_components(self):
        # While the bridge is up the whole component is quarantined;
        # once the bridge lapses the two sides split and both are clean
        # again with their own field winners.
        doc = bridge_document()
        report = resolve(doc)
        rows = {row['id']: row for row in report['queries']}
        self.assertEqual(rows['q-bridge-mid']['status'], 'quarantined')
        self.assertEqual(rows['q-after']['status'], 'clean')
        self.assertEqual(rows['q-after']['conflict_sources'], [])
        self.assertEqual(rows['q-after']['fields'], SIDE_A_FIELDS)

    # ---- degenerate shapes --------------------------------------------
    def test_zero_links_zero_queries_empty_records(self):
        doc = document([source('S', 1)],
                       [record('S', 'a', display_name='DA'),
                        record('S', 'b')],
                       [], [query('q', ['S', 'a'], 0)])
        report = self.check(doc)
        self.assertEqual(report['queries'][0]['member_count'], 1)
        # No links: two records of one source stay two components.
        doc = document([source('S', 1)], [record('S', 'a')], [], [])
        self.assertEqual(resolve(doc), {'queries': []})
        # No records at all is legal when nothing references them.
        doc = document([source('S', 1)], [], [], [])
        self.assertEqual(resolve(doc), {'queries': []})
        self.check(document([source('S', 1), source('T', 2)],
                            [record('S', 'x'), record('T', 'y')],
                            [link('L', ['S', 'x'], ['T', 'y'], 3, 4)],
                            []))

    def test_unqueried_links_still_validated(self):
        base_sources = [source('S', 1)]
        base_records = [record('S', 'a'), record('S', 'b')]
        base_queries = [query('q', ['S', 'a'], 0)]
        # A link to an unknown record rejects even though no query
        # touches the component it would belong to.
        doc = document(base_sources, base_records,
                       [link('BAD', ['S', 'a'], ['S', 'ghost'], 0, 10)],
                       base_queries)
        with self.assertRaises(ValueError):
            resolve(doc)
        # A link with an empty validity interval rejects likewise.
        doc = document(base_sources, base_records,
                       [link('BAD', ['S', 'a'], ['S', 'b'], 5, 5)],
                       base_queries)
        with self.assertRaises(ValueError):
            resolve(doc)
        # A duplicate link id rejects even with identical content.
        doc = document(base_sources, base_records,
                       [link('DUP', ['S', 'a'], ['S', 'b'], 0, 10),
                        link('DUP', ['S', 'a'], ['S', 'b'], 0, 10)],
                       base_queries)
        with self.assertRaises(ValueError):
            resolve(doc)
        # A valid but unqueried link is accepted and harmless.
        doc = document(base_sources, base_records,
                       [link('OK', ['S', 'a'], ['S', 'b'], 0, 10)],
                       base_queries)
        self.assertEqual(resolve(doc)['queries'][0]['member_count'], 2)

    # ---- randomized cross-check ---------------------------------------
    def test_randomized_against_oracle(self):
        rng = random.Random(20261118)
        for _ in range(150):
            sources = [source('S%d' % s, rng.randint(0, 3))
                       for s in range(rng.randint(1, 4))]
            source_ids = [row['id'] for row in sources]
            records = []
            for r in range(rng.randint(0, 8)):
                records.append(record(
                    rng.choice(source_ids), 'k%d' % r,
                    display_name=rng.choice([None, 'D%d' % r]),
                    email=rng.choice([None, 'e%d@x' % r]),
                    phone=rng.choice([None, 'p%d' % r])))
            keys = [(row['source'], row['key']) for row in records]
            links = []
            for n in range(rng.randint(0, 10)):
                if len(keys) < 2:
                    break
                left, right = rng.sample(keys, 2)
                low = rng.randint(0, 30)
                links.append(link('L%d' % n, list(left), list(right),
                                  low, low + rng.randint(1, 20)))
            queries = []
            for n in range(rng.randint(0, 12)):
                if not keys:
                    break
                queries.append(query('q%02d' % n,
                                     list(rng.choice(keys)),
                                     rng.randint(0, 50)))
            self.check(document(sources, records, links, queries))

    def test_medium_interleaved_against_oracle(self):
        # Interleaved activations/expiries over a huge time span with
        # many distinct query ticks; still small enough for the
        # per-query oracle.
        rng = random.Random(155)
        sources = [source('S%d' % s, rng.randint(0, 100))
                   for s in range(6)]
        records = [record('S%d' % (r % 6), 'k%03d' % r,
                          display_name='D%03d' % r if r % 3 == 0 else None,
                          email='e%03d@x' % r if r % 5 == 0 else None)
                   for r in range(60)]
        keys = [(row['source'], row['key']) for row in records]
        links = []
        for n in range(400):
            left, right = rng.sample(keys, 2)
            low = rng.randint(0, TICK_LIMIT - 1)
            high = min(TICK_LIMIT, low + rng.randint(1, 10 ** 6))
            links.append(link('L%03d' % n, list(left), list(right),
                              low, high))
        queries = [query('q%04d' % n, list(rng.choice(keys)),
                         rng.randint(0, TICK_LIMIT))
                   for n in range(800)]
        self.check(document(sources, records, links, queries))

    def test_determinism_under_input_reordering(self):
        doc = bridge_document()
        first = resolve(doc)
        rng = random.Random(9)
        for _ in range(5):
            shuffled = copy.deepcopy(doc)
            rng.shuffle(shuffled['sources'])
            rng.shuffle(shuffled['records'])
            rng.shuffle(shuffled['links'])
            rng.shuffle(shuffled['queries'])
            self.assertEqual(resolve(shuffled), first)


class ScaleTest(unittest.TestCase):
    """Near-limit constructed case: alternating pair phases, one grand
    merge, then a final split, over a huge time span."""

    SOURCES = 64
    CHAIN = 7500
    RECORDS = 10000
    QUERIES = 30000

    @staticmethod
    def source_id(index):
        return 'S%02d' % (index % ScaleTest.SOURCES)

    @staticmethod
    def record_key(index):
        return 'K%05d' % index

    @classmethod
    def build(cls):
        sources = [source('S%02d' % j, (j * 401) % 1009)
                   for j in range(cls.SOURCES)]
        records = []
        for i in range(cls.RECORDS):
            records.append(record(
                cls.source_id(i), cls.record_key(i),
                display_name='N%05d' % i if i % 7 == 0 else None,
                email='e%05d@x' % i if i % 11 == 0 else None,
                phone='p%05d' % i if i % 13 == 0 else None))
        links = []
        number = 0
        for i in range(1, cls.CHAIN):
            left = [cls.source_id(i - 1), cls.record_key(i - 1)]
            right = [cls.source_id(i), cls.record_key(i)]
            for phase in range(6):
                if (i + phase) % 2 == 0:
                    links.append(link('L%05d' % number, left, right,
                                      phase * W, (phase + 1) * W))
                    number += 1
            links.append(link('L%05d' % number, left, right, 6 * W, 7 * W))
            number += 1
        ticks = []
        for phase in range(6):
            ticks += [phase * W, phase * W + 1, phase * W + W // 2,
                      (phase + 1) * W - 1]
        ticks += [6 * W, 6 * W + 1, 6 * W + W // 2, 7 * W - 1,
                  7 * W, 7 * W + 1, TICK_LIMIT]
        queries = []
        for n in range(cls.QUERIES):
            index = (n * 7919) % cls.RECORDS
            queries.append(query('Q%05d' % n,
                                 [cls.source_id(index),
                                  cls.record_key(index)],
                                 ticks[n % len(ticks)]))
        return document(sources, records, links, queries), len(links)

    @classmethod
    def members_of(cls, index, tick):
        """Component members of record ``index`` at ``tick`` by
        construction: alternating pairs per phase, one grand component
        in [6W,7W), singletons elsewhere and after the final split.
        The grand component is returned as ``None`` (it is the whole
        chain) so callers never materialise 7500 indices per query."""
        if 6 * W <= tick < 7 * W and index < cls.CHAIN:
            return None
        if tick < 6 * W and index < cls.CHAIN:
            phase = tick // W
            if index >= 1 and (index + phase) % 2 == 0:
                return [index - 1, index]
            if index + 1 < cls.CHAIN and (index + 1 + phase) % 2 == 0:
                return [index, index + 1]
        return [index]

    @classmethod
    def expected_row(cls, context, query_row):
        """The provable expectation for one constructed query.

        ``context`` carries the precomputed priority map, record table
        and the shared grand-window summary so each query is O(1).
        """
        index = int(query_row['record'][1][1:])
        tick = query_row['at_tick']
        members = cls.members_of(index, tick)
        if members is None:
            canonical, status, conflicts, fields = context['grand']
            member_count = cls.CHAIN
        else:
            keys = [(cls.source_id(i), cls.record_key(i))
                    for i in members]
            canonical = min(keys)
            status = 'clean'      # pairs always mix two sources
            conflicts = []
            member_count = len(members)
            fields = {}
            for name in ('display_name', 'email', 'phone'):
                best = None
                for src, key in keys:
                    value = context['records'][(src, key)][name]
                    if value is None:
                        continue
                    candidate = (context['priorities'][src], src, key)
                    if best is None or candidate < best[0]:
                        best = (candidate, value)
                fields[name] = None if best is None else {
                    'value': best[1],
                    'record': [best[0][1], best[0][2]]}
        return {'id': query_row['id'], 'at_tick': tick,
                'record': list(query_row['record']),
                'canonical_record': list(canonical),
                'member_count': member_count, 'status': status,
                'conflict_sources': conflicts, 'fields': fields}

    def test_large_scale_constructed(self):
        doc, link_count = self.build()
        self.assertEqual(link_count, 29996)
        self.assertEqual(len(doc['records']), 10000)
        self.assertEqual(len(doc['queries']), 30000)
        distinct_ticks = {row['at_tick'] for row in doc['queries']}
        self.assertGreaterEqual(len(distinct_ticks), 30)
        context = {
            'priorities': {row['id']: row['priority']
                           for row in doc['sources']},
            'records': {(row['source'], row['key']): row
                        for row in doc['records']},
            # The grand window merges the whole chain: every one of the
            # 64 sources appears more than 100 times, so the component
            # is quarantined with all sources listed.
            'grand': (('S00', 'K00000'), 'quarantined',
                      ['S%02d' % j for j in range(64)], None),
        }
        report = resolve(doc)
        self.assertEqual(len(report['queries']), 30000)
        self.assertEqual([row['id'] for row in report['queries']],
                         sorted(row['id'] for row in report['queries']))
        by_id = {row['id']: row for row in report['queries']}
        statuses = set()
        counts = set()
        for query_row in doc['queries']:
            want = self.expected_row(context, query_row)
            got = by_id[query_row['id']]
            self.assertEqual(got, want, query_row['id'])
            statuses.add(got['status'])
            counts.add(got['member_count'])
        # The construction really exercised merges, quarantine, splits.
        self.assertEqual(statuses, {'clean', 'quarantined'})
        self.assertIn(7500, counts)
        self.assertIn(2, counts)
        self.assertIn(1, counts)
        # Spot-check the grand-merge window and the final split.
        grand = [row for row in report['queries']
                 if 6 * W <= row['at_tick'] < 7 * W
                 and int(row['record'][1][1:]) < 7500]
        self.assertTrue(grand)
        for row in grand:
            self.assertEqual(row['member_count'], 7500)
            self.assertEqual(row['status'], 'quarantined')
            self.assertEqual(row['conflict_sources'],
                             ['S%02d' % j for j in range(64)])
            self.assertIsNone(row['fields'])
            self.assertEqual(row['canonical_record'], ['S00', 'K00000'])
        after = [row for row in report['queries']
                 if row['at_tick'] >= 7 * W]
        self.assertTrue(after)
        for row in after:
            self.assertEqual(row['member_count'], 1)
            self.assertEqual(row['status'], 'clean')


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.good = document(
            [source('S1', 1), source('S2', 2)],
            [record('S1', 'a', display_name='DA'),
             record('S2', 'b', email='eb@x')],
            [link('L1', ['S1', 'a'], ['S2', 'b'], 0, 10)],
            [query('q1', ['S1', 'a'], 5)])

    def reject(self, doc):
        with self.assertRaises((ValueError, TypeError)):
            resolve(doc)

    def changed(self, **changes):
        doc = copy.deepcopy(self.good)
        doc.update(changes)
        return doc

    def test_top_level_shape(self):
        self.reject({})
        self.reject([])
        self.reject(None)
        self.reject('text')
        self.reject(self.changed(extra=1))
        for key in ('sources', 'records', 'links', 'queries'):
            doc = copy.deepcopy(self.good)
            del doc[key]
            self.reject(doc)

    def test_sources_validation(self):
        self.reject(self.changed(sources=[]))            # need 1..64
        # 64 sources pass, 65 are rejected.
        resolve(document([source('S%d' % i, 0) for i in range(64)],
                         [], [], []))
        self.reject(document([source('S%d' % i, 0) for i in range(65)],
                             [], [], []))
        self.reject(self.changed(sources=[source('S', 1),
                                          source('S', 2)]))
        self.reject(self.changed(sources=[{'id': 'S'}]))
        self.reject(self.changed(sources=[{'id': 'S', 'priority': 1,
                                           'extra': 0}]))
        for bad_priority in (True, False, -1, 10 ** 9 + 1, 1.5, '1',
                             None):
            self.reject(self.changed(
                sources=[{'id': 'S', 'priority': bad_priority}]))
        for bad_id in ('', ' x', 'x ', 'x' * 129, 'a\nb', 5, None):
            self.reject(self.changed(
                sources=[{'id': bad_id, 'priority': 0}]))
        # Equal priorities across sources are legal.
        resolve(document([source('A', 3), source('B', 3)], [], [], []))

    def test_records_validation(self):
        self.reject(self.changed(records={}))
        self.reject(self.changed(records=[None]))
        row = record('S1', 'a')
        row['extra'] = 1
        self.reject(self.changed(records=[row]))
        for key in ('source', 'key', 'display_name', 'email', 'phone'):
            row = record('S1', 'a')
            del row[key]
            self.reject(self.changed(records=[row]))
        self.reject(self.changed(records=[record('GHOST', 'a')]))
        self.reject(self.changed(records=[record('S1', 'a'),
                                          record('S1', 'a')]))
        # The same key under a different source is a different record.
        doc = document([source('S1', 1), source('S2', 2)],
                       [record('S1', 'a'), record('S2', 'a')],
                       [link('L1', ['S1', 'a'], ['S2', 'a'], 0, 10)],
                       [query('q1', ['S2', 'a'], 5)])
        self.assertEqual(resolve(doc)['queries'][0]['member_count'], 2)
        for bad_field in ('', ' x', 'x ', 'x' * 129, 'a\nb', 5, True,
                          [], {}):
            self.reject(self.changed(
                records=[record('S1', 'a', display_name=bad_field)]))
            self.reject(self.changed(
                records=[record('S1', 'a', email=bad_field)]))
            self.reject(self.changed(
                records=[record('S1', 'a', phone=bad_field)]))
        # Values are kept verbatim; inner spaces and symbols are fine.
        doc = document([source('S1', 1)],
                       [record('S1', 'a', display_name='Ann B. = 1')],
                       [], [query('q1', ['S1', 'a'], 0)])
        self.assertEqual(resolve(doc)['queries'][0]['fields']
                         ['display_name']['value'], 'Ann B. = 1')
        too_many = [record('S1', 'k%05d' % i) for i in range(10001)]
        self.reject(self.changed(records=too_many))
        resolve(document([source('S', 1)],
                         [record('S', 'k%05d' % i) for i in range(10000)],
                         [], []))

    def test_links_validation(self):
        self.reject(self.changed(links={}))
        row = link('L1', ['S1', 'a'], ['S2', 'b'], 0, 10)
        row['extra'] = 1
        self.reject(self.changed(links=[row]))
        for key in ('id', 'left', 'right', 'from_tick', 'to_tick'):
            row = link('L1', ['S1', 'a'], ['S2', 'b'], 0, 10)
            del row[key]
            self.reject(self.changed(links=[row]))
        self.reject(self.changed(links=[link('L1', ['S1', 'a'],
                                             ['S1', 'a'], 0, 10)]))
        for bad_endpoint in (['S1'], ['S1', 'a', 'x'], ['S1', 5],
                             'S1', None, ['GHOST', 'a'], ['S1', 'ghost']):
            self.reject(self.changed(links=[link('L1', bad_endpoint,
                                                 ['S2', 'b'], 0, 10)]))
            self.reject(self.changed(links=[link('L1', ['S1', 'a'],
                                                 bad_endpoint, 0, 10)]))
        self.reject(self.changed(links=[link('L1', ['S1', 'a'],
                                             ['S2', 'b'], 10, 10)]))
        self.reject(self.changed(links=[link('L1', ['S1', 'a'],
                                             ['S2', 'b'], 11, 10)]))
        for bad_tick in (True, False, -1, TICK_LIMIT + 1, 1.5, '1',
                         None):
            self.reject(self.changed(links=[link('L1', ['S1', 'a'],
                                                 ['S2', 'b'], bad_tick,
                                                 10)]))
            self.reject(self.changed(links=[link('L1', ['S1', 'a'],
                                                 ['S2', 'b'], 0,
                                                 bad_tick)]))
        resolve(self.changed(links=[link('L1', ['S1', 'a'], ['S2', 'b'],
                                         0, TICK_LIMIT)]))
        # Parallel links over the same pair and period are legal.
        resolve(self.changed(links=[link('L1', ['S1', 'a'], ['S2', 'b'],
                                         0, 10),
                                    link('L2', ['S1', 'a'], ['S2', 'b'],
                                         0, 10)]))
        # A link joining two records of one source is legal input.
        doc = document([source('S', 1)],
                       [record('S', 'a'), record('S', 'b')],
                       [link('L', ['S', 'a'], ['S', 'b'], 0, 10)],
                       [query('q', ['S', 'a'], 5)])
        row = resolve(doc)['queries'][0]
        self.assertEqual(row['status'], 'quarantined')
        self.assertEqual(row['conflict_sources'], ['S'])
        too_many = [link('L%05d' % i, ['S1', 'a'], ['S2', 'b'], 0, 10)
                    for i in range(30001)]
        self.reject(self.changed(links=too_many))

    def test_queries_validation(self):
        self.reject(self.changed(queries={}))
        row = query('q1', ['S1', 'a'], 5)
        row['extra'] = 1
        self.reject(self.changed(queries=[row]))
        for key in ('id', 'record', 'at_tick'):
            row = query('q1', ['S1', 'a'], 5)
            del row[key]
            self.reject(self.changed(queries=[row]))
        self.reject(self.changed(queries=[query('q1', ['S1', 'a'], 5),
                                          query('q1', ['S2', 'b'], 5)]))
        for bad_record in (['S1'], ['S1', 'a', 'x'], ['S1', 5], 'S1',
                           None, ['GHOST', 'a'], ['S1', 'ghost']):
            self.reject(self.changed(queries=[query('q2', bad_record,
                                                    5)]))
        for bad_tick in (True, False, -1, TICK_LIMIT + 1, 1.5, '1',
                         None):
            self.reject(self.changed(queries=[query('q2', ['S1', 'a'],
                                                    bad_tick)]))
        resolve(self.changed(queries=[query('q2', ['S1', 'a'], 0),
                                      query('q3', ['S1', 'a'],
                                            TICK_LIMIT)]))
        too_many = [query('q%05d' % i, ['S1', 'a'], 0)
                    for i in range(30001)]
        self.reject(self.changed(queries=too_many))

    def test_id_namespaces_are_independent(self):
        doc = document([source('SAME', 1)],
                       [record('SAME', 'a'), record('SAME', 'b')],
                       [link('SAME', ['SAME', 'a'], ['SAME', 'b'],
                             0, 10)],
                       [query('SAME', ['SAME', 'a'], 5)])
        row = resolve(doc)['queries'][0]
        self.assertEqual(row['member_count'], 2)
        self.assertEqual(row['status'], 'quarantined')


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.document = bridge_document()
        self.source = write_json(self.tmp, 'source', self.document)
        self.output = self.tmp / 'report.json'

    def call(self, source_path, output_path):
        return subprocess.run(
            [sys.executable, '-m', 'queue_identity.cli', '--source',
             str(source_path), '--output', str(output_path)],
            capture_output=True, text=True, env=ENV)

    def test_success_and_digest(self):
        result = self.call(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'queries'})
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(self.source.read_bytes())
                         .hexdigest())
        self.assertEqual(len(report['queries']), 9)
        first = self.output.read_bytes()
        self.assertEqual(self.call(self.source, self.output).returncode,
                         0)
        self.assertEqual(first, self.output.read_bytes())

    def test_failure_preserves_old_report(self):
        self.output.write_text('prior')
        bad = self.tmp / 'bad.json'
        for body in ('{}', '{"a":1,"a":2}', 'NaN', '[]', '', 'null',
                     '{"sources":[],"records":[],"links":[],"queries"'
                     ':[]}',
                     '{"sources":[{"id":"S","priority":true}],"records"'
                     ':[],"links":[],"queries":[]}',
                     '{"sources":[{"id":"S","priority":0}],"records":[]'
                     ',"links":[],"queries":[],"extra":0}'):
            bad.write_text(body)
            self.assertEqual(self.call(bad, self.output).returncode, 2)
            self.assertEqual(self.output.read_text(), 'prior')
        doc = copy.deepcopy(self.document)
        doc['links'][0]['from_tick'] = True
        bad.write_text(json.dumps(doc))
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')
        doc = copy.deepcopy(self.document)
        doc['queries'][0]['record'] = ['CRM', 'ghost']
        bad.write_text(json.dumps(doc))
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_duplicate_json_keys_rejected(self):
        self.output.write_text('prior')
        bad = self.tmp / 'dup.json'
        bad.write_text('{"sources": [{"id": "S", "priority": 0}], '
                       '"records": [], "records": [], "links": [], '
                       '"queries": []}')
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_output_path_safety(self):
        before = self.source.read_bytes()
        self.assertEqual(self.call(self.source, self.source).returncode,
                         2)
        self.assertEqual(self.source.read_bytes(), before)
        link_path = self.tmp / 'link.json'
        link_path.symlink_to(self.source)
        self.assertEqual(self.call(self.source, link_path).returncode, 2)
        self.assertEqual(self.source.read_bytes(), before)
        alias_dir = self.tmp / 'alias-dir'
        alias_dir.symlink_to(self.tmp)
        self.assertEqual(
            self.call(alias_dir / self.source.name, self.source)
            .returncode, 2)
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

    def test_shuffled_rows_byte_identical(self):
        first = self.tmp / 'first.json'
        self.assertEqual(self.call(self.source, first).returncode, 0)
        doc = copy.deepcopy(self.document)
        rng = random.Random(3)
        rng.shuffle(doc['sources'])
        rng.shuffle(doc['records'])
        rng.shuffle(doc['links'])
        rng.shuffle(doc['queries'])
        shuffled = write_json(self.tmp, 'shuffled', doc)
        second = self.tmp / 'second.json'
        self.assertEqual(self.call(shuffled, second).returncode, 0)
        one = json.loads(first.read_text())
        two = json.loads(second.read_text())
        one.pop('source_sha256')
        two.pop('source_sha256')
        self.assertEqual(json.dumps(one, sort_keys=True),
                         json.dumps(two, sort_keys=True))


class FixtureAndApiTest(unittest.TestCase):
    def test_run_api(self):
        source_path = ROOT / 'fixtures/identity/source.json'
        report = identity_run(source_path)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source_path.read_bytes())
                         .hexdigest())
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(set(report),
                         {'schema_version', 'source_sha256', 'queries'})

    def test_fixture_matches_expected_report(self):
        tmp = Path(tempfile.mkdtemp())
        output = tmp / 'report.json'
        result = subprocess.run(
            [sys.executable, '-m', 'queue_identity.cli', '--source',
             str(ROOT / 'fixtures/identity/source.json'), '--output',
             str(output)], capture_output=True, text=True, env=ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(),
                         (ROOT / 'fixtures/identity/expected-report.json')
                         .read_text())
        doc = json.loads(
            (ROOT / 'fixtures/identity/source.json').read_text())
        report = json.loads(output.read_text())
        self.assertEqual(report['queries'],
                         oracle.solve(doc)['queries'])

    def test_module_uses_only_standard_library(self):
        package = ROOT / 'src' / 'queue_identity'
        for path in package.glob('*.py'):
            text = path.read_text()
            self.assertNotIn('sqlite3', text, path)
            self.assertNotIn('queue_planner.store', text, path)
            self.assertNotIn('queue_planner.service', text, path)


if __name__ == '__main__':
    unittest.main()
