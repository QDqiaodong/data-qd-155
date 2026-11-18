"""Tests for queue-reconcile: cross-source two-level optimal matching,
the exact all-optimal bilateral support, and the strict source contract.

Every small scenario is cross-checked with ``reconcile_oracle``, an
independent enumerator that lists all legal one-to-one matchings and
keeps the two-level optimal ones; it never imports the production
matcher.  The maximum-scale cases are verified against hand-derived
expectations from their symmetric construction instead.
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

import reconcile_oracle as oracle
from queue_reconcile.cli import run as reconcile_run
from queue_reconcile.model import reconcile

ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}

TICK_LIMIT = 10 ** 12


def rate(rate_id, service_code, currency, num=1, den=1,
         low=0, high=TICK_LIMIT):
    return {'rate_id': rate_id, 'service_code': service_code,
            'server': 0, 'from_tick': low, 'to_tick': high,
            'currency': currency, 'minor_units_num': num,
            'minor_units_den': den}


def work(work_id, service_code, invoice, start=0, finish=10):
    return {'work_id': work_id, 'service_code': service_code,
            'server': 0, 'start_tick': start, 'finish_tick': finish,
            'invoice_id': invoice}


def receipt(receipt_id, currency, minor_units, eligible=None):
    return {'receipt_id': receipt_id, 'currency': currency,
            'minor_units': minor_units, 'eligible_invoices': eligible}


def document_for(invoice_specs, receipts, tolerance):
    """Build a valid source document from a compact specification.

    ``invoice_specs`` is a list of ``(invoice_id, currency, amount)``;
    a ``None`` currency yields a zero-length work order and therefore a
    null-currency invoice, a zero amount with a currency uses a zero
    rate so the invoice keeps a definite currency.
    """
    works = []
    rates = []
    seen_services = set()
    for index, (invoice_id, code, amount) in enumerate(invoice_specs):
        if code is None:
            assert amount == 0
            works.append(work('W%d' % index, 'NONE', invoice_id,
                              start=3, finish=3))
            continue
        service = code if amount else 'ZERO-' + code
        if service not in seen_services:
            seen_services.add(service)
            rates.append(rate('RATE-' + service, service, code,
                              num=1 if amount else 0))
        works.append(work('W%d' % index, service, invoice_id,
                          finish=amount if amount else 5))
    cost_source = {'servers': 1, 'work_intervals': works, 'rates': rates}
    return {'cost_source': cost_source, 'receipts': receipts,
            'tolerance_minor_units': tolerance}


def write_json(directory, name, document):
    path = Path(directory) / ('%s-%d.json' % (name, random.randrange(10 ** 9)))
    path.write_text(json.dumps(document, sort_keys=True))
    return path


class ModelTest(unittest.TestCase):
    def check(self, document):
        """Run production, compare every published fact with the oracle
        and re-verify the contract's structural invariants directly."""
        report = reconcile(document)
        fragment, optimal = oracle.solve(document)
        self.assertEqual(set(report),
                         {'objective', 'matching', 'invoices', 'receipts'})
        self.assertEqual(report['objective'], fragment['objective'])
        self.assertEqual(report['invoices'], fragment['invoices'])
        self.assertEqual(report['receipts'], fragment['receipts'])
        oracle.validate_witness(document, report)
        witness = frozenset((edge['invoice_id'], edge['receipt_id'])
                            for edge in report['matching'])
        self.assertIn(witness, optimal)

        # Contract invariants, checked independently of the oracle.
        pairs = [(edge['invoice_id'], edge['receipt_id'])
                 for edge in report['matching']]
        self.assertEqual(pairs, sorted(pairs))
        self.assertEqual([row['invoice_id'] for row in report['invoices']],
                         sorted(row['invoice_id']
                                for row in report['invoices']))
        self.assertEqual([row['receipt_id'] for row in report['receipts']],
                         sorted(row['receipt_id']
                                for row in report['receipts']))
        invoice_rows = {row['invoice_id']: row for row in report['invoices']}
        receipt_rows = {row['receipt_id']: row for row in report['receipts']}
        for row in report['invoices']:
            self.assertEqual(set(row), {'invoice_id', 'currency',
                                        'rounded_minor_units',
                                        'possible_receipt_ids',
                                        'can_be_unmatched', 'status'})
            possible = row['possible_receipt_ids']
            self.assertEqual(possible, sorted(possible))
            self.assertEqual(len(possible), len(set(possible)))
            self.assertIs(type(row['can_be_unmatched']), bool)
        for row in report['receipts']:
            self.assertEqual(set(row), {'receipt_id', 'currency',
                                        'minor_units',
                                        'possible_invoice_ids',
                                        'can_be_unmatched', 'status'})
            possible = row['possible_invoice_ids']
            self.assertEqual(possible, sorted(possible))
            self.assertEqual(len(possible), len(set(possible)))
            self.assertIs(type(row['can_be_unmatched']), bool)
        for row in report['invoices'] + report['receipts']:
            key = 'possible_receipt_ids' if 'invoice_id' in row \
                else 'possible_invoice_ids'
            possible = row[key]
            if not possible:
                self.assertEqual(row['status'], 'unmatched')
                self.assertTrue(row['can_be_unmatched'])
            elif len(possible) == 1 and not row['can_be_unmatched']:
                self.assertEqual(row['status'], 'fixed')
            else:
                self.assertEqual(row['status'], 'ambiguous')
        # Bilateral support symmetry and fixed-pair consistency.
        for invoice_id, row in invoice_rows.items():
            for receipt_id in row['possible_receipt_ids']:
                self.assertIn(invoice_id,
                              receipt_rows[receipt_id]
                              ['possible_invoice_ids'])
            if row['status'] == 'fixed':
                other = receipt_rows[row['possible_receipt_ids'][0]]
                self.assertEqual(other['possible_invoice_ids'],
                                 [invoice_id])
                self.assertFalse(other['can_be_unmatched'])
                self.assertEqual(other['status'], 'fixed')
        for receipt_id, row in receipt_rows.items():
            for invoice_id in row['possible_invoice_ids']:
                self.assertIn(receipt_id,
                              invoice_rows[invoice_id]
                              ['possible_receipt_ids'])
        return report

    def reject(self, document):
        with self.assertRaises((ValueError, TypeError)):
            reconcile(document)

    # ---- optimal-support scenarios ----------------------------------
    def test_candidate_edge_excluded_by_lower_cost_alternative(self):
        # (I, R-CLOSE) is a legal candidate (difference 2 <= 5) but the
        # unique optimal matching uses only (I, R-EXACT).
        document = document_for(
            [('I', 'USD', 10)],
            [receipt('R-CLOSE', 'USD', 12),
             receipt('R-EXACT', 'USD', 10)], 5)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 1,
                          'total_difference_minor_units': 0})
        invoice = report['invoices'][0]
        self.assertEqual(invoice['possible_receipt_ids'], ['R-EXACT'])
        self.assertEqual(invoice['status'], 'fixed')
        close = report['receipts'][0]
        self.assertEqual(close['receipt_id'], 'R-CLOSE')
        self.assertEqual(close['possible_invoice_ids'], [])
        self.assertEqual(close['status'], 'unmatched')
        self.assertTrue(close['can_be_unmatched'])

    def test_cross_edges_only_appear_in_worse_cost_matchings(self):
        # The diagonal costs 0; both cross edges cost 10 and only show
        # up in matchings whose total difference is 20, never optimal.
        document = document_for(
            [('I1', 'USD', 10), ('I2', 'USD', 20)],
            [receipt('R1', 'USD', 10), receipt('R2', 'USD', 20)], 10)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 2,
                          'total_difference_minor_units': 0})
        for row, want in zip(report['invoices'], (['R1'], ['R2'])):
            self.assertEqual(row['possible_receipt_ids'], want)
            self.assertEqual(row['status'], 'fixed')
        self.assertEqual(report['matching'],
                         [{'invoice_id': 'I1', 'receipt_id': 'R1',
                           'difference_minor_units': 0},
                          {'invoice_id': 'I2', 'receipt_id': 'R2',
                           'difference_minor_units': 0}])

    def test_alternating_cycle_ambiguity(self):
        # 2x2 all-zero: both perfect matchings are optimal, so every
        # record has two possible counterparties and none can be left
        # unmatched (a perfect matching always exists).
        document = document_for(
            [('I1', 'USD', 5), ('I2', 'USD', 5)],
            [receipt('R1', 'USD', 5), receipt('R2', 'USD', 5)], 0)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 2,
                          'total_difference_minor_units': 0})
        for row in report['invoices']:
            self.assertEqual(row['possible_receipt_ids'], ['R1', 'R2'])
            self.assertFalse(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')
        _, optimal = oracle.solve(document)
        self.assertEqual(len(optimal), 2)

    def test_exchangeable_witness_support_is_not_concatenatable(self):
        # Same 2x2 instance: the deterministic witness uses only
        # exchangeable edges (each appears in the other optimal
        # matching), yet the bilateral support must not be read as
        # jointly realizable -- I1->R1 and I2->R1 are both supported,
        # but no legal (let alone optimal) matching contains both.
        document = document_for(
            [('I1', 'USD', 5), ('I2', 'USD', 5)],
            [receipt('R1', 'USD', 5), receipt('R2', 'USD', 5)], 0)
        report = self.check(document)
        witness = {(edge['invoice_id'], edge['receipt_id'])
                   for edge in report['matching']}
        self.assertIn(witness, ({('I1', 'R1'), ('I2', 'R2')},
                                {('I1', 'R2'), ('I2', 'R1')}))
        _, optimal = oracle.solve(document)
        for matching in optimal:
            self.assertFalse({('I1', 'R1'), ('I2', 'R1')}
                             <= set(matching))
        # The support union nevertheless contains both of those edges.
        self.assertEqual(report['invoices'][0]['possible_receipt_ids'],
                         ['R1', 'R2'])
        self.assertEqual(report['invoices'][1]['possible_receipt_ids'],
                         ['R1', 'R2'])

    def test_fixed_pairs_coexist_with_ambiguous_component(self):
        document = document_for(
            [('A1', 'USD', 10), ('A2', 'USD', 10), ('F', 'USD', 77),
             ('U', 'USD', 500)],
            [receipt('R1', 'USD', 10), receipt('R2', 'USD', 10),
             receipt('RF', 'USD', 77), receipt('RU', 'USD', 999)], 0)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 3,
                          'total_difference_minor_units': 0})
        rows = {row['invoice_id']: row for row in report['invoices']}
        self.assertEqual(rows['A1']['status'], 'ambiguous')
        self.assertEqual(rows['A2']['status'], 'ambiguous')
        self.assertEqual(rows['F']['possible_receipt_ids'], ['RF'])
        self.assertEqual(rows['F']['status'], 'fixed')
        self.assertEqual(rows['U']['possible_receipt_ids'], [])
        self.assertEqual(rows['U']['status'], 'unmatched')
        receipts = {row['receipt_id']: row for row in report['receipts']}
        self.assertEqual(receipts['RF']['status'], 'fixed')
        self.assertEqual(receipts['RU']['status'], 'unmatched')

    def test_two_receipts_contend_for_one_invoice(self):
        # One optimal pair; each receipt is possible but replaceable,
        # so a single possible counterparty plus can_be_unmatched is
        # still ambiguous, not fixed.
        document = document_for(
            [('I', 'USD', 10)],
            [receipt('R1', 'USD', 10), receipt('R2', 'USD', 10)], 0)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 1,
                          'total_difference_minor_units': 0})
        invoice = report['invoices'][0]
        self.assertEqual(invoice['possible_receipt_ids'], ['R1', 'R2'])
        self.assertFalse(invoice['can_be_unmatched'])
        self.assertEqual(invoice['status'], 'ambiguous')
        for row in report['receipts']:
            self.assertEqual(row['possible_invoice_ids'], ['I'])
            self.assertTrue(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')

    def test_contention_every_record_has_candidate_no_perfect(self):
        # Every one of the 3 invoices and the receipt has a local
        # candidate, yet at most one pair can be reconciled.
        document = document_for(
            [('I1', 'USD', 5), ('I2', 'USD', 5), ('I3', 'USD', 5)],
            [receipt('R', 'USD', 5)], 0)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 1,
                          'total_difference_minor_units': 0})
        for row in report['invoices']:
            self.assertEqual(row['possible_receipt_ids'], ['R'])
            self.assertTrue(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')
        row = report['receipts'][0]
        self.assertEqual(row['possible_invoice_ids'], ['I1', 'I2', 'I3'])
        self.assertFalse(row['can_be_unmatched'])
        self.assertEqual(row['status'], 'ambiguous')

    def test_surplus_on_either_side(self):
        # More invoices than receipts and vice versa, both normal.
        document = document_for(
            [('I1', 'USD', 5), ('I2', 'USD', 5)],
            [receipt('R', 'USD', 5)], 0)
        report = self.check(document)
        self.assertEqual(report['objective']['matched_count'], 1)
        document = document_for(
            [('I', 'USD', 5)],
            [receipt('R1', 'USD', 5), receipt('R2', 'USD', 5),
             receipt('R3', 'USD', 5)], 0)
        report = self.check(document)
        self.assertEqual(report['objective']['matched_count'], 1)
        self.assertEqual(len(report['matching']), 1)

    def test_zero_amounts_zero_tolerance_and_null_currency(self):
        document = document_for(
            [('INV-ZERO', 'USD', 0), ('INV-NULL', None, 0)],
            [receipt('R-ZERO', 'USD', 0),
             receipt('R-EUR-ZERO', 'EUR', 0)], 0)
        report = self.check(document)
        invoices = {row['invoice_id']: row for row in report['invoices']}
        # A definite-currency zero invoice pairs with a legal zero
        # receipt of the same currency even at tolerance zero.
        self.assertEqual(invoices['INV-ZERO']['possible_receipt_ids'],
                         ['R-ZERO'])
        self.assertEqual(invoices['INV-ZERO']['status'], 'fixed')
        # A null-currency invoice never has candidates and cannot
        # borrow a currency from any receipt.
        self.assertIsNone(invoices['INV-NULL']['currency'])
        self.assertEqual(invoices['INV-NULL']['possible_receipt_ids'], [])
        self.assertEqual(invoices['INV-NULL']['status'], 'unmatched')
        self.assertTrue(invoices['INV-NULL']['can_be_unmatched'])
        receipts = {row['receipt_id']: row for row in report['receipts']}
        self.assertEqual(receipts['R-EUR-ZERO']['status'], 'unmatched')

    def test_empty_sides(self):
        document = document_for([('I1', 'USD', 5)], [], 0)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 0,
                          'total_difference_minor_units': 0})
        self.assertEqual(report['matching'], [])
        self.assertEqual(report['invoices'][0]['status'], 'unmatched')
        document = document_for([], [receipt('R1', 'USD', 5)], 0)
        report = self.check(document)
        self.assertEqual(report['invoices'], [])
        self.assertEqual(report['receipts'][0]['status'], 'unmatched')
        document = document_for([], [], 0)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 0,
                          'total_difference_minor_units': 0})
        self.assertEqual(report['invoices'], [])
        self.assertEqual(report['receipts'], [])

    def test_currency_mismatch_is_not_an_error(self):
        document = document_for([('I1', 'USD', 10)],
                                [receipt('R1', 'EUR', 10)], 0)
        report = self.check(document)
        self.assertEqual(report['objective']['matched_count'], 0)
        self.assertEqual(report['invoices'][0]['status'], 'unmatched')
        self.assertEqual(report['receipts'][0]['status'], 'unmatched')

    def test_receipt_id_may_equal_invoice_id_across_namespaces(self):
        document = document_for([('SAME', 'USD', 7)],
                                [receipt('SAME', 'USD', 7)], 0)
        report = self.check(document)
        self.assertEqual(report['matching'],
                         [{'invoice_id': 'SAME', 'receipt_id': 'SAME',
                           'difference_minor_units': 0}])
        self.assertEqual(report['invoices'][0]['status'], 'fixed')
        self.assertEqual(report['receipts'][0]['status'], 'fixed')

    def test_tolerance_boundary_is_inclusive(self):
        document = document_for([('I', 'USD', 10)],
                                [receipt('R', 'USD', 15)], 5)
        report = self.check(document)
        self.assertEqual(report['objective']['matched_count'], 1)
        self.assertEqual(report['matching'][0]['difference_minor_units'], 5)
        document = document_for([('I', 'USD', 10)],
                                [receipt('R', 'USD', 15)], 4)
        report = self.check(document)
        self.assertEqual(report['objective']['matched_count'], 0)

    def test_huge_amounts_exact_integer_difference(self):
        big = 10 ** 36
        cost_source = {'servers': 1,
                       'work_intervals': [work('W1', 'S', 'INV',
                                               finish=TICK_LIMIT)],
                       'rates': [rate('R', 'S', 'USD', num=10 ** 18)]}
        # invoice rounds to 10^30; the gap to 10^36 is exact
        gap = big - 10 ** 30
        document = {'cost_source': cost_source,
                    'receipts': [receipt('R-BIG', 'USD', big)],
                    'tolerance_minor_units': gap}
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 1,
                          'total_difference_minor_units': gap})
        document = {'cost_source': cost_source,
                    'receipts': [receipt('R-BIG', 'USD', big)],
                    'tolerance_minor_units': gap - 1}
        report = self.check(document)
        self.assertEqual(report['objective']['matched_count'], 0)

    def test_invoice_level_rounding_decides_matching(self):
        # Two works of exactly 1/2 each: the invoice rounds once to 1,
        # while per-work rounding would give 2.  Tolerance 0 makes the
        # bill-level rounded amount decisive for the candidate set.
        cost_source = {
            'servers': 1,
            'work_intervals': [
                work('W1', 'S', 'INV', finish=1),
                work('W2', 'S', 'INV', finish=1),
            ],
            'rates': [rate('HALF', 'S', 'USD', num=1, den=2, low=0,
                           high=1)],
        }
        document = {'cost_source': cost_source,
                    'receipts': [receipt('R-ONE', 'USD', 1),
                                 receipt('R-TWO', 'USD', 2)],
                    'tolerance_minor_units': 0}
        report = self.check(document)
        invoice = report['invoices'][0]
        self.assertEqual(invoice['rounded_minor_units'], 1)
        self.assertEqual(invoice['possible_receipt_ids'], ['R-ONE'])
        self.assertEqual(report['matching'],
                         [{'invoice_id': 'INV', 'receipt_id': 'R-ONE',
                           'difference_minor_units': 0}])
        receipts = {row['receipt_id']: row for row in report['receipts']}
        self.assertEqual(receipts['R-TWO']['status'], 'unmatched')

    def test_eligibility_restricts_candidates(self):
        # R-WIDE admits everything, R-NARROW only I2, R-NONE nothing.
        document = document_for(
            [('I1', 'USD', 5), ('I2', 'USD', 5)],
            [receipt('R-NARROW', 'USD', 5, ['I2']),
             receipt('R-NONE', 'USD', 5, []),
             receipt('R-WIDE', 'USD', 5)], 0)
        report = self.check(document)
        receipts = {row['receipt_id']: row for row in report['receipts']}
        self.assertEqual(receipts['R-NARROW']['possible_invoice_ids'],
                         ['I2'])
        self.assertEqual(receipts['R-NONE']['possible_invoice_ids'], [])
        self.assertEqual(receipts['R-NONE']['status'], 'unmatched')
        self.assertEqual(report['objective']['matched_count'], 2)

    def test_eligibility_may_reference_null_currency_invoice(self):
        # Referencing an existing null-currency invoice is legal input;
        # it simply never becomes a candidate.
        document = document_for([('I1', None, 0)],
                                [receipt('R', 'USD', 0, ['I1'])], 0)
        report = self.check(document)
        self.assertEqual(report['receipts'][0]['possible_invoice_ids'],
                         [])
        self.assertEqual(report['objective']['matched_count'], 0)

    def test_unmatchable_needs_zero_arc_not_just_shared_component(self):
        # Subtle case: in the zero reduced-cost residual graph I2 ends
        # up in the source's strong component via other nodes, yet its
        # own backward arc to the source has reduced cost 2 -- so no
        # zero-cost cycle unmatches I2 and can_be_unmatched must stay
        # false.  A component-only check would get this wrong; the
        # brute-force oracle confirms the exact answer.
        document = document_for(
            [('I0', 'USD', 9), ('I1', 'USD', 2), ('I2', 'USD', 4),
             ('I3', 'USD', 0), ('I4', 'USD', 8)],
            [receipt('R0', 'USD', 3),
             receipt('R1', 'USD', 1, ['I1', 'I2', 'I4']),
             receipt('R2', 'USD', 5)], 4)
        report = self.check(document)
        self.assertEqual(report['objective'],
                         {'matched_count': 3,
                          'total_difference_minor_units': 5})
        rows = {row['invoice_id']: row for row in report['invoices']}
        self.assertEqual(rows['I2']['possible_receipt_ids'], ['R0', 'R2'])
        self.assertFalse(rows['I2']['can_be_unmatched'])
        self.assertEqual(rows['I2']['status'], 'ambiguous')
        self.assertEqual(rows['I1']['status'], 'fixed')
        self.assertEqual(rows['I0']['status'], 'unmatched')

    def test_randomized_against_oracle(self):
        rng = random.Random(20261118)
        for _ in range(150):
            n = rng.randint(0, 6)
            m = rng.randint(0, 6)
            specs = []
            for i in range(n):
                code = rng.choice(['USD', 'EUR', None])
                amount = rng.randint(0, 8) if code else 0
                specs.append(('I%d' % i, code, amount))
            ids = [spec[0] for spec in specs]
            receipts = []
            for r in range(m):
                if rng.random() < 0.4:
                    eligible = None
                else:
                    eligible = [invoice_id for invoice_id in ids
                                if rng.random() < 0.5]
                receipts.append(receipt('R%d' % r,
                                        rng.choice(['USD', 'EUR']),
                                        rng.randint(0, 10), eligible))
            self.check(document_for(specs, receipts,
                                    rng.randint(0, 4)))

    def test_randomized_degenerate_against_oracle(self):
        # Amounts in {0, 1} with tolerance zero: almost every legal
        # edge is cost-tied, maximising interlinked zero-cost cycles.
        rng = random.Random(155)
        for _ in range(120):
            n = rng.randint(0, 6)
            m = rng.randint(0, 6)
            specs = [('I%d' % i, 'USD', rng.randint(0, 1))
                     for i in range(n)]
            ids = [spec[0] for spec in specs]
            receipts = []
            for r in range(m):
                if rng.random() < 0.5:
                    eligible = None
                else:
                    eligible = [invoice_id for invoice_id in ids
                                if rng.random() < 0.5]
                receipts.append(receipt('R%d' % r, 'USD',
                                        rng.randint(0, 1), eligible))
            self.check(document_for(specs, receipts, 0))

    def test_determinism_under_input_reordering(self):
        document = document_for(
            [('A1', 'USD', 10), ('A2', 'EUR', 20), ('F', 'USD', 77),
             ('Z', None, 0)],
            [receipt('R1', 'USD', 10), receipt('R2', 'USD', 10),
             receipt('R3', 'EUR', 21, ['A2']),
             receipt('R4', 'USD', 77, ['F', 'A1'])], 3)
        first = reconcile(document)
        rng = random.Random(9)
        for _ in range(5):
            shuffled = copy.deepcopy(document)
            rng.shuffle(shuffled['receipts'])
            rng.shuffle(shuffled['cost_source']['work_intervals'])
            rng.shuffle(shuffled['cost_source']['rates'])
            for row in shuffled['receipts']:
                if isinstance(row['eligible_invoices'], list):
                    rng.shuffle(row['eligible_invoices'])
            self.assertEqual(reconcile(shuffled), first)


class ScaleTest(unittest.TestCase):
    def test_max_scale_alternating_cycle(self):
        # 300 invoices and 300 receipts on one alternating cycle: every
        # receipt Rj admits exactly Ij and I(j+1 mod 300), all amounts
        # equal, tolerance zero.  Exactly two perfect matchings exist
        # and both are optimal, so by symmetry every candidate edge is
        # in the support and no record can be unmatched.
        n = 300
        specs = [('I%03d' % i, 'USD', 100) for i in range(n)]
        receipts = [receipt('R%03d' % j, 'USD', 100,
                            ['I%03d' % j, 'I%03d' % ((j + 1) % n)])
                    for j in range(n)]
        report = reconcile(document_for(specs, receipts, 0))
        self.assertEqual(report['objective'],
                         {'matched_count': n,
                          'total_difference_minor_units': 0})
        for i, row in enumerate(report['invoices']):
            self.assertEqual(row['possible_receipt_ids'],
                             sorted(('R%03d' % ((i - 1) % n),
                                     'R%03d' % i)))
            self.assertFalse(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')
        for j, row in enumerate(report['receipts']):
            self.assertEqual(row['possible_invoice_ids'],
                             sorted(('I%03d' % j,
                                     'I%03d' % ((j + 1) % n))))
            self.assertFalse(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')
        # The witness is one of the two perfect matchings.
        even = frozenset(('I%03d' % j, 'R%03d' % j) for j in range(n))
        odd = frozenset(('I%03d' % ((j + 1) % n), 'R%03d' % j)
                        for j in range(n))
        witness = frozenset((edge['invoice_id'], edge['receipt_id'])
                            for edge in report['matching'])
        self.assertIn(witness, (even, odd))

    def test_max_scale_dense_complete_support(self):
        # 300x300 complete, all amounts equal: every perfect matching
        # is optimal, so every candidate edge is in the support.
        n = 300
        specs = [('I%03d' % i, 'USD', 7) for i in range(n)]
        receipts = [receipt('R%03d' % j, 'USD', 7) for j in range(n)]
        report = reconcile(document_for(specs, receipts, 0))
        self.assertEqual(report['objective'],
                         {'matched_count': n,
                          'total_difference_minor_units': 0})
        all_receipts = ['R%03d' % j for j in range(n)]
        all_invoices = ['I%03d' % i for i in range(n)]
        for row in report['invoices']:
            self.assertEqual(row['possible_receipt_ids'], all_receipts)
            self.assertFalse(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')
        for row in report['receipts']:
            self.assertEqual(row['possible_invoice_ids'], all_invoices)
            self.assertFalse(row['can_be_unmatched'])
            self.assertEqual(row['status'], 'ambiguous')
        self.assertEqual(len(report['matching']), n)

    def test_record_count_boundaries(self):
        # 300 invoices pass, 301 are rejected.
        specs = [('I%03d' % i, 'USD', 1) for i in range(300)]
        report = reconcile(document_for(specs, [], 0))
        self.assertEqual(len(report['invoices']), 300)
        specs = [('I%03d' % i, 'USD', 1) for i in range(301)]
        with self.assertRaises(ValueError):
            reconcile(document_for(specs, [], 0))
        # 300 receipts pass, 301 are rejected.
        document = document_for([], [receipt('R%03d' % j, 'USD', 1)
                                     for j in range(300)], 0)
        self.assertEqual(len(reconcile(document)['receipts']), 300)
        document = document_for([], [receipt('R%03d' % j, 'USD', 1)
                                     for j in range(301)], 0)
        with self.assertRaises(ValueError):
            reconcile(document)
        # 300 eligibility entries pass, 301 are rejected.
        specs = [('I%03d' % i, 'USD', 1) for i in range(300)]
        eligible = ['I%03d' % i for i in range(300)]
        document = document_for(specs, [receipt('R', 'USD', 1, eligible)],
                                0)
        self.assertEqual(reconcile(document)['objective']
                         ['matched_count'], 1)
        document = document_for(specs,
                                [receipt('R', 'USD', 1, eligible + ['I000'])],
                                0)
        with self.assertRaises(ValueError):
            reconcile(document)


class ValidationTest(unittest.TestCase):
    def setUp(self):
        self.good = document_for([('I1', 'USD', 5)],
                                 [receipt('R1', 'USD', 5)], 0)

    def reject(self, document):
        with self.assertRaises((ValueError, TypeError)):
            reconcile(document)

    def changed(self, **changes):
        document = copy.deepcopy(self.good)
        document.update(changes)
        return document

    def test_top_level_shape(self):
        self.reject({})
        self.reject([])
        self.reject(None)
        self.reject('text')
        self.reject(self.changed(extra=1))
        for key in ('cost_source', 'receipts', 'tolerance_minor_units'):
            document = copy.deepcopy(self.good)
            del document[key]
            self.reject(document)

    def test_tolerance_validation(self):
        for bad in (True, -1, 10 ** 36 + 1, 1.5, '5', None):
            self.reject(self.changed(tolerance_minor_units=bad))
        for good in (0, 10 ** 36):
            reconcile(self.changed(tolerance_minor_units=good))

    def test_receipts_container(self):
        self.reject(self.changed(receipts={}))
        self.reject(self.changed(receipts='R1'))
        self.reject(self.changed(receipts=[None]))
        self.reject(self.changed(receipts=['R1']))

    def test_receipt_fields(self):
        def with_receipt(row):
            return self.changed(receipts=[row])

        row = receipt('R1', 'USD', 5)
        row['extra'] = 1
        self.reject(with_receipt(row))
        for key in ('receipt_id', 'currency', 'minor_units',
                    'eligible_invoices'):
            row = receipt('R1', 'USD', 5)
            del row[key]
            self.reject(with_receipt(row))
        for bad_id in ('', ' x', 'x ', 'x' * 129, 'a\nb', 5, None):
            self.reject(with_receipt(receipt(bad_id, 'USD', 5)))
        for bad_currency in ('usd', 'US', 'USDD', 'U1D', 'USD\n', 5,
                             None):
            self.reject(with_receipt(receipt('R1', bad_currency, 5)))
        for bad_amount in (True, False, -1, 10 ** 36 + 1, 2.5, '3',
                           None):
            self.reject(with_receipt(receipt('R1', 'USD', bad_amount)))
        reconcile(with_receipt(receipt('R1', 'USD', 10 ** 36)))

    def test_duplicate_receipt_ids(self):
        self.reject(self.changed(receipts=[receipt('R1', 'USD', 5),
                                           receipt('R1', 'USD', 5)]))

    def test_eligible_invoices_validation(self):
        def with_eligible(value):
            return self.changed(receipts=[receipt('R1', 'USD', 5,
                                                  value)])

        self.reject(with_eligible('I1'))       # not an array
        self.reject(with_eligible(5))
        self.reject(with_eligible(['NOPE']))   # unknown invoice
        self.reject(with_eligible(['I1', 'I1']))  # duplicate
        self.reject(with_eligible([5]))
        self.reject(with_eligible([None]))
        self.reject(with_eligible([{}]))
        self.reject(with_eligible(['I1'] * 301))  # too many entries
        reconcile(with_eligible(None))         # all admitted
        reconcile(with_eligible([]))           # none admitted
        reconcile(with_eligible(['I1']))

    def test_cost_source_errors_reject_whole_document(self):
        for bad in (5, {}, [], None,
                    {'servers': 1, 'work_intervals': [], 'rates': [],
                     'extra': 1},
                    {'servers': True, 'work_intervals': [], 'rates': []},
                    {'servers': 0, 'work_intervals': [], 'rates': []}):
            self.reject(self.changed(cost_source=bad))
        # A rate-coverage gap inside cost_source rejects everything.
        cost_source = {'servers': 1,
                       'work_intervals': [work('W1', 'S', 'I1', finish=5)],
                       'rates': [rate('R', 'S', 'USD', low=10, high=20)]}
        self.reject(self.changed(cost_source=cost_source))
        # Mixed currencies inside one invoice reject everything.
        cost_source = {
            'servers': 1,
            'work_intervals': [work('W1', 'S', 'I1', finish=1),
                               work('W2', 'T', 'I1', finish=1)],
            'rates': [rate('R1', 'S', 'USD', low=0, high=1),
                      rate('R2', 'T', 'EUR', low=0, high=1)]}
        self.reject(self.changed(cost_source=cost_source))


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        document = document_for(
            [('A1', 'USD', 10), ('A2', 'USD', 10), ('F', 'EUR', 20)],
            [receipt('R1', 'USD', 10), receipt('R2', 'USD', 10),
             receipt('R3', 'EUR', 21, ['F'])], 3)
        self.document = document
        self.source = write_json(self.tmp, 'source', document)
        self.output = self.tmp / 'report.json'

    def call(self, source_path, output_path):
        return subprocess.run(
            [sys.executable, '-m', 'queue_reconcile.cli', '--source',
             str(source_path), '--output', str(output_path)],
            capture_output=True, text=True, env=ENV)

    def test_success_and_digest(self):
        result = self.call(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(set(report), {'schema_version', 'source_sha256',
                                       'objective', 'matching', 'invoices',
                                       'receipts'})
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(self.source.read_bytes())
                         .hexdigest())
        self.assertEqual(report['objective'],
                         {'matched_count': 3,
                          'total_difference_minor_units': 1})
        first = self.output.read_bytes()
        self.assertEqual(self.call(self.source, self.output).returncode, 0)
        self.assertEqual(first, self.output.read_bytes())

    def test_failure_preserves_old_report(self):
        self.output.write_text('prior')
        bad = self.tmp / 'bad.json'
        for body in ('{}', '{"a":1,"a":2}', 'NaN', '[]', '', 'null',
                     '{"cost_source":null,"receipts":[],'
                     '"tolerance_minor_units":0}',
                     '{"cost_source":{"servers":1,"work_intervals":[],'
                     '"rates":[]},"receipts":[],"tolerance_minor_units"'
                     ':true}',
                     '{"cost_source":{"servers":1,"work_intervals":[],'
                     '"rates":[]},"receipts":[],"tolerance_minor_units"'
                     ':0,"extra":0}'):
            bad.write_text(body)
            self.assertEqual(self.call(bad, self.output).returncode, 2)
            self.assertEqual(self.output.read_text(), 'prior')
        document = copy.deepcopy(self.document)
        document['receipts'][0]['minor_units'] = True
        bad.write_text(json.dumps(document))
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')
        document = copy.deepcopy(self.document)
        document['receipts'][0]['eligible_invoices'] = ['NOPE']
        bad.write_text(json.dumps(document))
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')
        document = copy.deepcopy(self.document)
        document['cost_source']['rates'] = []
        bad.write_text(json.dumps(document))
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_duplicate_json_keys_rejected(self):
        self.output.write_text('prior')
        bad = self.tmp / 'dup.json'
        bad.write_text('{"cost_source": {"servers": 1, "work_intervals":'
                       ' [], "rates": []}, "receipts": [], "receipts":'
                       ' [], "tolerance_minor_units": 0}')
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_output_path_safety(self):
        before = self.source.read_bytes()
        self.assertEqual(self.call(self.source, self.source).returncode, 2)
        self.assertEqual(self.source.read_bytes(), before)
        link = self.tmp / 'link.json'
        link.symlink_to(self.source)
        self.assertEqual(self.call(self.source, link).returncode, 2)
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
        document = copy.deepcopy(self.document)
        rng = random.Random(3)
        rng.shuffle(document['receipts'])
        rng.shuffle(document['cost_source']['work_intervals'])
        rng.shuffle(document['cost_source']['rates'])
        shuffled = write_json(self.tmp, 'shuffled', document)
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
        source_path = ROOT / 'fixtures/reconcile/source.json'
        report = reconcile_run(source_path)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source_path.read_bytes())
                         .hexdigest())
        self.assertEqual(report['schema_version'], 1)

    def test_fixture_matches_expected_report(self):
        tmp = Path(tempfile.mkdtemp())
        output = tmp / 'report.json'
        result = subprocess.run(
            [sys.executable, '-m', 'queue_reconcile.cli', '--source',
             str(ROOT / 'fixtures/reconcile/source.json'), '--output',
             str(output)], capture_output=True, text=True, env=ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(),
                         (ROOT / 'fixtures/reconcile/expected-report.json')
                         .read_text())
        document = json.loads(
            (ROOT / 'fixtures/reconcile/source.json').read_text())
        report = json.loads(output.read_text())
        fragment, optimal = oracle.solve(document)
        self.assertEqual(report['objective'], fragment['objective'])
        self.assertEqual(report['invoices'], fragment['invoices'])
        self.assertEqual(report['receipts'], fragment['receipts'])
        oracle.validate_witness(document, report)

    def test_module_uses_only_standard_library(self):
        package = ROOT / 'src' / 'queue_reconcile'
        for path in package.glob('*.py'):
            text = path.read_text()
            self.assertNotIn('sqlite3', text, path)
            self.assertNotIn('queue_planner.store', text, path)
            self.assertNotIn('queue_planner.service', text, path)


if __name__ == '__main__':
    unittest.main()
