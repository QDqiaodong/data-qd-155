"""Tests for queue-cost: strict source contract, rate-boundary splitting,
exact rational work-order costs and single per-invoice half-up rounding.

Every numeric expectation is cross-checked with ``cost_oracle``, an
independent implementation written from the contract text that does not
import the production pricing code.
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
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))

import cost_oracle as oracle
from queue_cost.cli import run as cost_run
from queue_cost.model import analyze

ENV = {**os.environ, 'PYTHONPATH': str(ROOT / 'src')}


def rate(rate_id, service_code='S', server=0, low=0, high=10,
         currency='USD', num=1, den=1):
    return {'rate_id': rate_id, 'service_code': service_code,
            'server': server, 'from_tick': low, 'to_tick': high,
            'currency': currency, 'minor_units_num': num,
            'minor_units_den': den}


def work(work_id, service_code='S', server=0, start=0, finish=10,
         invoice='INV'):
    return {'work_id': work_id, 'service_code': service_code,
            'server': server, 'start_tick': start, 'finish_tick': finish,
            'invoice_id': invoice}


def source(works, rates, servers=2):
    return {'servers': servers, 'work_intervals': works, 'rates': rates}


class ModelTest(unittest.TestCase):
    def check(self, document):
        """Run production, compare every published fact with the oracle."""
        result = analyze(document)
        expected_orders, expected_invoices = oracle.price(document)
        self.assertEqual(result['work_orders'], expected_orders)
        self.assertEqual(result['invoices'], expected_invoices)
        # Invariants from the contract, checked independently of the oracle.
        works = {w['work_id']: w for w in document['work_intervals']}
        by_invoice = {}
        for order in result['work_orders']:
            self.assertEqual(
                [segment['from_tick'] for segment in order['segments']],
                sorted(segment['from_tick']
                       for segment in order['segments']))
            self.assertEqual(Fraction(order['exact_minor_units']),
                             sum((Fraction(segment['exact_minor_units'])
                                  for segment in order['segments']),
                                 Fraction(0)))
            self.assertEqual(order['invoice_id'],
                             works[order['work_id']]['invoice_id'])
            by_invoice.setdefault(order['invoice_id'], []).append(order)
        for invoice in result['invoices']:
            orders = by_invoice.get(invoice['invoice_id'], [])
            self.assertEqual(Fraction(invoice['exact_minor_units']),
                             sum((Fraction(o['exact_minor_units'])
                                  for o in orders), Fraction(0)))
            exact = Fraction(invoice['exact_minor_units'])
            expected = (exact.numerator * 2 + exact.denominator) // (
                exact.denominator * 2)
            self.assertEqual(invoice['rounded_minor_units'], expected)
        return result

    def reject(self, document):
        with self.assertRaises((ValueError, TypeError)):
            analyze(document)

    # ---- boundary semantics -----------------------------------------
    def test_split_on_rate_boundaries_and_boundary_tick_ownership(self):
        # [0,10) at 1/3 and [10,20) at 1/2; a work starting exactly at 10
        # uses only the new rate; a work ending exactly at 10 uses only the
        # old rate.
        rates = [rate('a', low=0, high=10, num=1, den=3),
                 rate('b', low=10, high=20, num=1, den=2)]
        result = self.check(source([
            work('W1', start=0, finish=20),
            work('W2', start=10, finish=12),
            work('W3', start=8, finish=10),
            work('W4', start=10, finish=10),
        ], rates))
        orders = {o['work_id']: o for o in result['work_orders']}
        self.assertEqual(
            [(s['rate_id'], s['from_tick'], s['to_tick'])
             for s in orders['W1']['segments']],
            [('a', 0, 10), ('b', 10, 20)])
        self.assertEqual(orders['W1']['exact_minor_units'], '25/3')
        self.assertEqual([s['rate_id'] for s in orders['W2']['segments']],
                         ['b'])
        self.assertEqual(orders['W2']['exact_minor_units'], '1')
        self.assertEqual([s['rate_id'] for s in orders['W3']['segments']],
                         ['a'])
        self.assertEqual(orders['W3']['exact_minor_units'], '2/3')
        self.assertEqual(orders['W4']['segments'], [])
        self.assertEqual(orders['W4']['exact_minor_units'], '0')

    def test_gap_rejected_not_extrapolated(self):
        rates = [rate('a', low=0, high=10),
                 rate('b', low=20, high=30)]
        self.reject(source([work('W1', start=5, finish=25)], rates))

    def test_overlapping_rates_rejected(self):
        self.reject(source([work('W1', start=0, finish=5)],
                           [rate('a', low=0, high=10),
                            rate('b', low=9, high=20)]))

    def test_touching_rates_do_not_overlap(self):
        self.check(source([work('W1', start=0, finish=20)],
                          [rate('a', low=0, high=10),
                           rate('b', low=10, high=20)]))

    def test_same_service_code_other_server_is_not_a_substitute(self):
        works = [work('W1', server=1, start=0, finish=5)]
        self.reject(source(works, [rate('a', server=0)]))
        result = self.check(source(works, [rate('a', server=0),
                                           rate('b', server=1,
                                                num=2, den=1)]))
        self.assertEqual(result['work_orders'][0]['exact_minor_units'], '10')

    def test_other_service_code_is_not_a_substitute(self):
        self.reject(source([work('W1', service_code='T')],
                           [rate('a', service_code='S')]))

    def test_zero_rate_and_zero_duration(self):
        result = self.check(source([
            work('Z', start=3, finish=3),
            work('F', start=0, finish=5),
        ], [rate('r', low=0, high=5, num=0, den=1)]))
        orders = {o['work_id']: o for o in result['work_orders']}
        self.assertEqual(orders['Z']['exact_minor_units'], '0')
        self.assertEqual(orders['Z']['segments'], [])
        self.assertIsNone(orders['Z']['currency'])
        self.assertEqual(orders['F']['exact_minor_units'], '0')
        self.assertEqual(orders['F']['segments'][0]['exact_minor_units'], '0')

    def test_large_numerator_and_denominator(self):
        big = 10 ** 18
        result = self.check(source(
            [work('W1', start=0, finish=10 ** 12)],
            [rate('a', low=0, high=10 ** 12, num=big, den=big - 1)]))
        self.assertEqual(Fraction(result['work_orders'][0]
                                  ['exact_minor_units']),
                         Fraction(10 ** 12 * big, big - 1))

    def test_full_time_range(self):
        result = self.check(source(
            [work('W1', start=0, finish=10 ** 12)],
            [rate('a', low=0, high=10 ** 12, num=1, den=1)]))
        self.assertEqual(result['work_orders'][0]['exact_minor_units'],
                         str(Fraction(10 ** 12)))

    # ---- invoices / rounding ----------------------------------------
    def test_invoice_exact_sum_and_half_up(self):
        # 25/3 + 25/6 = 25/2 = 12.5 -> 13, but rounding each order first
        # would give 8 + 4 = 12.
        rates = [rate('a', low=0, high=10, num=1, den=3),
                 rate('b', low=10, high=20, num=1, den=2)]
        result = self.check(source([
            work('W1', start=0, finish=20, invoice='INV'),
            work('W2', start=5, finish=15, invoice='INV'),
        ], rates))
        invoice = result['invoices'][0]
        self.assertEqual(invoice['exact_minor_units'], '25/2')
        self.assertEqual(invoice['rounded_minor_units'], 13)
        early = sum((oracle.round_half_up(Fraction(o['exact_minor_units']))
                     for o in result['work_orders']))
        self.assertEqual(early, 12)
        self.assertNotEqual(early, invoice['rounded_minor_units'])

    def test_half_exactly_rounds_up(self):
        result = self.check(source(
            [work('W1', start=0, finish=1, invoice='INV')],
            [rate('a', low=0, high=1, num=1, den=2)]))
        self.assertEqual(result['invoices'][0]['exact_minor_units'], '1/2')
        self.assertEqual(result['invoices'][0]['rounded_minor_units'], 1)

    def test_just_below_half_rounds_down(self):
        result = self.check(source(
            [work('W1', start=0, finish=1, invoice='INV')],
            [rate('a', low=0, high=10, num=49, den=100)]))
        self.assertEqual(result['invoices'][0]['rounded_minor_units'], 0)

    def test_two_orders_rounding_early_changes_total(self):
        # Each work is exactly 1/2: an invoice must round once to 1, while
        # per-work rounding would give 1 + 1 = 2.
        rates = [rate('half', low=0, high=1, num=1, den=2)]
        result = self.check(source([
            work('W1', start=0, finish=1, invoice='INV'),
            work('W2', start=0, finish=1, invoice='INV'),
        ], rates))
        self.assertEqual(result['invoices'][0]['exact_minor_units'], '1')
        self.assertEqual(result['invoices'][0]['rounded_minor_units'], 1)
        early = sum(oracle.round_half_up(Fraction(o['exact_minor_units']))
                    for o in result['work_orders'])
        self.assertEqual(early, 2)
        self.assertNotEqual(early, result['invoices'][0]['rounded_minor_units'])

    def test_zero_duration_needs_no_rate_coverage(self):
        result = self.check(source([
            work('Z', start=7, finish=7, invoice='INV'),
        ], []))
        self.assertEqual(result['work_orders'], [{
            'work_id': 'Z', 'invoice_id': 'INV', 'currency': None,
            'segments': [], 'exact_minor_units': '0'}])
        self.assertEqual(result['invoices'], [{
            'invoice_id': 'INV', 'currency': None,
            'exact_minor_units': '0', 'rounded_minor_units': 0}])

    def test_report_field_sets_are_exact(self):
        result = self.check(source([
            work('W1', start=0, finish=20, invoice='INV'),
        ], [rate('a', low=0, high=10, num=1, den=3),
            rate('b', low=10, high=20, num=1, den=2)]))
        self.assertEqual(set(result), {'work_orders', 'invoices'})
        self.assertEqual(set(result['work_orders'][0]),
                         {'work_id', 'invoice_id', 'currency', 'segments',
                          'exact_minor_units'})
        self.assertEqual(set(result['work_orders'][0]['segments'][0]),
                         {'rate_id', 'from_tick', 'to_tick',
                          'exact_minor_units'})
        self.assertEqual(set(result['invoices'][0]),
                         {'invoice_id', 'currency', 'exact_minor_units',
                          'rounded_minor_units'})

    def test_zero_duration_work_does_not_invent_a_currency(self):
        result = self.check(source([
            work('Z', start=5, finish=5, invoice='EMPTY'),
            work('P', start=0, finish=2, invoice='PAID'),
        ], [rate('r', low=0, high=2, currency='USD')]))
        orders = {o['work_id']: o for o in result['work_orders']}
        self.assertIsNone(orders['Z']['currency'])
        invoices = {i['invoice_id']: i for i in result['invoices']}
        self.assertIsNone(invoices['EMPTY']['currency'])
        self.assertEqual(invoices['EMPTY']['exact_minor_units'], '0')
        self.assertEqual(invoices['EMPTY']['rounded_minor_units'], 0)
        self.assertEqual(invoices['PAID']['currency'], 'USD')

    def test_mixed_currency_invoice_rejected(self):
        rates = [rate('u', currency='USD', low=0, high=10),
                 rate('e', currency='EUR', low=10, high=20)]
        self.reject(source([work('W1', start=0, finish=20,
                                 invoice='INV')], rates))

    def test_mixed_currency_across_works_in_one_invoice_rejected(self):
        rates = [rate('u', server=0, currency='USD'),
                 rate('e', server=1, currency='EUR')]
        self.reject(source([
            work('W1', server=0, invoice='INV'),
            work('W2', server=1, invoice='INV'),
        ], rates))

    def test_report_segment_cap_rejected(self):
        from unittest import mock
        import queue_cost.model as model
        rates = [rate('a', low=0, high=5), rate('b', low=5, high=10)]
        document = source([work('W1', start=0, finish=10)], rates)
        with mock.patch.object(model, 'SEGMENT_LIMIT', 1):
            self.reject(document)
        self.check(document)

    def test_invoices_sorted_and_independent(self):
        rates = [rate('u', server=0, currency='USD'),
                 rate('e', server=1, currency='EUR')]
        result = self.check(source([
            work('W1', server=1, start=0, finish=2, invoice='B'),
            work('W2', server=0, start=0, finish=2, invoice='A'),
        ], rates))
        self.assertEqual([i['invoice_id'] for i in result['invoices']],
                         ['A', 'B'])
        self.assertEqual([o['work_id'] for o in result['work_orders']],
                         ['W1', 'W2'])
        self.assertEqual([i['currency'] for i in result['invoices']],
                         ['USD', 'EUR'])

    # ---- structural rejection ---------------------------------------
    def test_top_level_fields(self):
        good = source([work('W1', start=0, finish=1)], [rate('a')])
        self.check(good)
        for bad in ({'servers': 2, 'work_intervals': [],
                     'rates': [], 'extra': 1},
                    {'work_intervals': [], 'rates': []},
                    {'servers': 2, 'rates': []}):
            self.reject(bad)
        self.reject([])
        self.reject(None)

    def test_servers_bounds_and_bool(self):
        for servers in (0, 101, True, 2.0, '2', None):
            self.reject(source([], [], servers=servers))

    def test_work_fields_bounds(self):
        base = source([work('W1')], [rate('a')])
        self.check(base)
        cases = []
        row = work('W1'); row['extra'] = 1; cases.append({'work_intervals':
                                                          [row]})
        row = work('W1'); del row['invoice_id']; cases.append({'work_intervals':
                                                               [row]})
        row = work('W1'); row['work_id'] = ' '; cases.append(
            {'work_intervals': [row]})
        row = work('W1'); row['work_id'] = 'x' * 129; cases.append(
            {'work_intervals': [row]})
        row = work('W1'); row['server'] = True; cases.append(
            {'work_intervals': [row]})
        row = work('W1'); row['server'] = 2; cases.append(
            {'work_intervals': [row]})
        row = work('W1'); row['start_tick'] = -1; cases.append(
            {'work_intervals': [row]})
        row = work('W1'); row['start_tick'] = 10 ** 12 + 1; cases.append(
            {'work_intervals': [row]})
        row = work('W1'); row['start_tick'] = 11; row['finish_tick'] = 10
        cases.append({'work_intervals': [row]})
        row = work('W1'); row['invoice_id'] = ''; cases.append(
            {'work_intervals': [row]})
        for change in cases:
            document = source([], [rate('a')])
            document.update(change)
            self.reject(document)

    def test_duplicate_work_and_rate_ids(self):
        self.reject(source([work('W1', finish=5), work('W1', finish=5)],
                           [rate('a')]))
        self.reject(source([work('W1', finish=5)],
                           [rate('a', low=0, high=5),
                            rate('a', low=5, high=10)]))

    def test_rate_fields_bounds(self):
        cases = []
        row = rate('r'); row['extra'] = 1; cases.append(row)
        row = rate('r'); del row['currency']; cases.append(row)
        row = rate('r'); row['rate_id'] = '  '; cases.append(row)
        row = rate('r'); row['server'] = -1; cases.append(row)
        row = rate('r'); row['server'] = True; cases.append(row)
        row = rate('r'); row['from_tick'] = -1; cases.append(row)
        row = rate('r'); row['to_tick'] = 10 ** 12 + 1; cases.append(row)
        row = rate('r'); row['from_tick'] = 10; row['to_tick'] = 10
        cases.append(row)
        row = rate('r'); row['currency'] = 'usd'; cases.append(row)
        row = rate('r'); row['currency'] = 'US'; cases.append(row)
        row = rate('r'); row['currency'] = 'U1D'; cases.append(row)
        row = rate('r'); row['currency'] = 'USD\n'; cases.append(row)
        row = rate('r'); row['minor_units_num'] = -1; cases.append(row)
        row = rate('r'); row['minor_units_num'] = 10 ** 18 + 1; cases.append(row)
        row = rate('r'); row['minor_units_num'] = True; cases.append(row)
        row = rate('r'); row['minor_units_den'] = 0; cases.append(row)
        row = rate('r'); row['minor_units_den'] = 10 ** 18 + 1; cases.append(row)
        row = rate('r'); row['minor_units_den'] = False; cases.append(row)
        for bad in cases:
            self.reject(source([work('W1')], [bad]))

    def test_empty_inputs(self):
        result = self.check(source([], []))
        self.assertEqual(result, {'work_orders': [], 'invoices': []})
        result = self.check(source([], [rate('a')]))
        self.assertEqual(result, {'work_orders': [], 'invoices': []})

    def test_list_limits(self):
        self.reject(source([work('W')] * 20001, [rate('a')]))
        self.reject(source([], [rate('r%d' % i, low=0, high=1)
                                for i in range(100001)]))

    # ---- determinism -------------------------------------------------
    def test_reordering_input_rows_does_not_change_result(self):
        rates = [rate('a', low=0, high=10, num=1, den=3),
                 rate('b', low=10, high=20, num=1, den=2),
                 rate('c', service_code='T', low=0, high=20)]
        works = [work('W1', start=0, finish=20),
                 work('W2', start=5, finish=15),
                 work('W3', service_code='T', start=1, finish=3,
                      invoice='OTHER')]
        first = analyze(source(works, rates))
        rng = random.Random(7)
        for _ in range(5):
            shuffled_works = copy.deepcopy(works)
            shuffled_rates = copy.deepcopy(rates)
            rng.shuffle(shuffled_works)
            rng.shuffle(shuffled_rates)
            self.assertEqual(
                json.dumps(analyze(source(shuffled_works, shuffled_rates)),
                           sort_keys=True),
                json.dumps(first, sort_keys=True))

    def test_randomized_against_oracle(self):
        rng = random.Random(155)
        for _ in range(40):
            servers = rng.randint(1, 4)
            rates = []
            by_key = {}
            for index in range(rng.randint(0, 8)):
                code = rng.choice(('S', 'T', 'U'))
                server = rng.randrange(servers)
                key = (code, server)
                low = rng.randrange(0, 60)
                high = low + rng.randrange(1, 20)
                if any(not (high <= a or low >= b)
                       for a, b in by_key.get(key, [])):
                    continue
                by_key.setdefault(key, []).append((low, high))
                rates.append(rate('r%d' % index, service_code=code,
                                  server=server, low=low, high=high,
                                  currency=rng.choice(('USD', 'EUR')),
                                  num=rng.randrange(0, 20),
                                  den=rng.randrange(1, 20)))
            works = []
            for index in range(rng.randint(0, 10)):
                code = rng.choice(('S', 'T', 'U'))
                server = rng.randrange(servers)
                if rng.random() < 0.3:
                    start = rng.randrange(0, 80)
                    finish = start
                else:
                    start = rng.randrange(0, 60)
                    finish = start + rng.randrange(1, 20)
                works.append(work('W%d' % index, service_code=code,
                                  server=server, start=start, finish=finish,
                                  invoice='I%d' % rng.randrange(1, 4)))
            document = source(works, rates, servers=servers)
            try:
                self.check(document)
            except ValueError:
                with self.assertRaises(ValueError):
                    analyze(document)


class CommandTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = write_json(self.tmp, 'source', source(
            [work('W1', start=0, finish=20)],
            [rate('a', low=0, high=10, num=1, den=3),
             rate('b', low=10, high=20, num=1, den=2)]))
        self.output = self.tmp / 'report.json'

    def call(self, source_path, output_path):
        return subprocess.run(
            [sys.executable, '-m', 'queue_cost.cli', '--source',
             str(source_path), '--output', str(output_path)],
            capture_output=True, text=True, env=ENV)

    def test_success_and_digest(self):
        result = self.call(self.source, self.output)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(self.output.read_text())
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(report['work_orders'][0]['exact_minor_units'],
                         '25/3')
        first = self.output.read_bytes()
        self.assertEqual(self.call(self.source, self.output).returncode, 0)
        self.assertEqual(first, self.output.read_bytes())

    def test_failure_preserves_old_report(self):
        self.output.write_text('prior')
        bad = self.tmp / 'bad.json'
        for body in ('{}', '{"a":1,"a":2}', 'NaN', '[]', '',
                     'null', '{"servers":true,"work_intervals":[],'
                     '"rates":[]}', '{"servers":2,"work_intervals":[],'
                     '"rates":[],"extra":0}'):
            bad.write_text(body)
            self.assertEqual(self.call(bad, self.output).returncode, 2)
            self.assertEqual(self.output.read_text(), 'prior')
        document = json.loads(self.source.read_text())
        document['work_intervals'][0]['server'] = True
        bad.write_text(json.dumps(document))
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_duplicate_json_keys_rejected(self):
        self.output.write_text('prior')
        bad = self.tmp / 'dup.json'
        bad.write_text('{"servers": 2, "servers": 3, '
                       '"work_intervals": [], "rates": []}')
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        self.assertEqual(self.output.read_text(), 'prior')

    def test_gap_and_overlap_exit_two(self):
        self.output.write_text('prior')
        document = json.loads(self.source.read_text())
        document['work_intervals'][0]['finish_tick'] = 30
        bad = write_json(self.tmp, 'gap', document)
        self.assertEqual(self.call(bad, self.output).returncode, 2)
        document = json.loads(self.source.read_text())
        document['rates'][1]['from_tick'] = 9
        bad = write_json(self.tmp, 'overlap', document)
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
            self.call(alias_dir / self.source.name, self.source).returncode, 2)
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
        document = json.loads(self.source.read_text())
        random.Random(3).shuffle(document['work_intervals'])
        document['rates'].reverse()
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
        source_path = ROOT / 'fixtures/cost/source.json'
        report = cost_run(source_path)
        self.assertEqual(report['source_sha256'],
                         hashlib.sha256(source_path.read_bytes()).hexdigest())
        self.assertEqual(report['schema_version'], 1)

    def test_fixture_matches_expected_report(self):
        tmp = Path(tempfile.mkdtemp())
        output = tmp / 'report.json'
        result = subprocess.run(
            [sys.executable, '-m', 'queue_cost.cli', '--source',
             str(ROOT / 'fixtures/cost/source.json'), '--output',
             str(output)], capture_output=True, text=True, env=ENV)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(output.read_text(),
                         (ROOT / 'fixtures/cost/expected-report.json')
                         .read_text())
        document = json.loads(
            (ROOT / 'fixtures/cost/source.json').read_text())
        expected_orders, expected_invoices = oracle.price(document)
        report = json.loads(output.read_text())
        self.assertEqual(report['work_orders'], expected_orders)
        self.assertEqual(report['invoices'], expected_invoices)

    def test_module_uses_only_standard_library(self):
        package = ROOT / 'src' / 'queue_cost'
        for path in package.glob('*.py'):
            text = path.read_text()
            self.assertNotIn('sqlite3', text, path)
            self.assertNotIn('queue_planner.store', text, path)
            self.assertNotIn('queue_planner.service', text, path)


def write_json(directory, name, document):
    path = Path(directory) / ('%s-%d.json' % (name, random.randrange(10 ** 9)))
    path.write_text(json.dumps(document, sort_keys=True))
    return path


if __name__ == '__main__':
    unittest.main()
