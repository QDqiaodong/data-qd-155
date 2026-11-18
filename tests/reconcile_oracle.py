"""Independent oracle for queue-reconcile tests.

Brute-force enumerates every legal one-to-one pairing for small
instances, keeps exactly the two-level optimal ones (maximum pair
count, then minimum total absolute difference), and derives bilateral
supports, ``can_be_unmatched`` and ``status`` straight from the
contract text.  Invoice amounts are priced by ``cost_oracle`` (itself
independent of ``queue_cost.model``); nothing here imports
``queue_reconcile`` production code.
"""
from cost_oracle import price


def _is_candidate(invoice, receipt, tolerance):
    """The contract's candidate predicate, restated independently."""
    code = invoice['currency']
    if code is None or code != receipt['currency']:
        return False
    eligible = receipt['eligible_invoices']
    if eligible is not None and invoice['invoice_id'] not in eligible:
        return False
    return (abs(invoice['rounded_minor_units'] - receipt['minor_units'])
            <= tolerance)


def _status(possible, can_be_unmatched):
    if not possible:
        return 'unmatched'
    if len(possible) == 1 and not can_be_unmatched:
        return 'fixed'
    return 'ambiguous'


def solve(document):
    """Return ``(fragment, optimal_matchings)`` for a valid document.

    ``fragment`` mirrors the ``objective``/``invoices``/``receipts``
    part of the production report; ``optimal_matchings`` lists every
    two-level optimal matching as a frozenset of
    ``(invoice_id, receipt_id)`` pairs for witness validation.
    """
    _, invoice_rows = price(document['cost_source'])
    invoices = invoice_rows  # already sorted by invoice_id
    receipts = sorted(document['receipts'],
                      key=lambda row: row['receipt_id'])
    tolerance = document['tolerance_minor_units']
    n = len(invoices)
    m = len(receipts)
    cand = []
    for invoice in invoices:
        row = []
        for receipt in receipts:
            if _is_candidate(invoice, receipt, tolerance):
                row.append(abs(invoice['rounded_minor_units']
                               - receipt['minor_units']))
            else:
                row.append(None)
        cand.append(row)

    best = None  # (count, negated_cost), maximised
    optimal = []
    used = [False] * m
    current = []

    def visit(i, count, cost):
        nonlocal best, optimal
        if best is not None and count + (n - i) < best[0]:
            return  # cannot reach the best pair count any more
        if i == n:
            key = (count, -cost)
            if best is None or key > best:
                best = key
                optimal = [frozenset(current)]
            elif key == best:
                optimal.append(frozenset(current))
            return
        visit(i + 1, count, cost)  # leave invoice i unmatched
        for r in range(m):
            if cand[i][r] is not None and not used[r]:
                used[r] = True
                current.append((i, r))
                visit(i + 1, count + 1, cost + cand[i][r])
                current.pop()
                used[r] = False

    visit(0, 0, 0)
    matched_count, negated_cost = best
    total = -negated_cost

    invoice_possible = [set() for _ in range(n)]
    receipt_possible = [set() for _ in range(m)]
    invoice_free = [False] * n
    receipt_free = [False] * m
    for matching in optimal:
        covered_i = {i for i, _ in matching}
        covered_r = {r for _, r in matching}
        for i, r in matching:
            invoice_possible[i].add(r)
            receipt_possible[r].add(i)
        for i in range(n):
            if i not in covered_i:
                invoice_free[i] = True
        for r in range(m):
            if r not in covered_r:
                receipt_free[r] = True

    invoice_report = []
    for i, invoice in enumerate(invoices):
        possible = sorted(receipts[r]['receipt_id']
                          for r in invoice_possible[i])
        invoice_report.append({
            'invoice_id': invoice['invoice_id'],
            'currency': invoice['currency'],
            'rounded_minor_units': invoice['rounded_minor_units'],
            'possible_receipt_ids': possible,
            'can_be_unmatched': invoice_free[i],
            'status': _status(possible, invoice_free[i]),
        })
    receipt_report = []
    for r, receipt in enumerate(receipts):
        possible = sorted(invoices[i]['invoice_id']
                          for i in receipt_possible[r])
        receipt_report.append({
            'receipt_id': receipt['receipt_id'],
            'currency': receipt['currency'],
            'minor_units': receipt['minor_units'],
            'possible_invoice_ids': possible,
            'can_be_unmatched': receipt_free[r],
            'status': _status(possible, receipt_free[r]),
        })
    named_optimal = [frozenset((invoices[i]['invoice_id'],
                                receipts[r]['receipt_id'])
                               for i, r in matching)
                     for matching in optimal]
    fragment = {
        'objective': {'matched_count': matched_count,
                      'total_difference_minor_units': total},
        'invoices': invoice_report,
        'receipts': receipt_report,
    }
    return fragment, named_optimal


def validate_witness(document, report):
    """Assert the published witness is a legal two-level optimal matching.

    Raises ``AssertionError`` on any inconsistency; checks legality of
    every edge (candidate predicate and exact difference), the
    one-to-one property, agreement with ``objective``, and membership
    of every witness edge in both published support sets.
    """
    _, invoice_rows = price(document['cost_source'])
    amounts = {row['invoice_id']: row['rounded_minor_units']
               for row in invoice_rows}
    currencies = {row['invoice_id']: row['currency']
                  for row in invoice_rows}
    receipts = {row['receipt_id']: row for row in document['receipts']}
    seen_invoices = set()
    seen_receipts = set()
    total = 0
    for edge in report['matching']:
        assert set(edge) == {'invoice_id', 'receipt_id',
                             'difference_minor_units'}, edge
        invoice_id = edge['invoice_id']
        receipt_id = edge['receipt_id']
        assert invoice_id not in seen_invoices, invoice_id
        assert receipt_id not in seen_receipts, receipt_id
        seen_invoices.add(invoice_id)
        seen_receipts.add(receipt_id)
        receipt = receipts[receipt_id]
        assert currencies[invoice_id] is not None
        assert currencies[invoice_id] == receipt['currency']
        eligible = receipt['eligible_invoices']
        assert eligible is None or invoice_id in eligible
        difference = abs(amounts[invoice_id] - receipt['minor_units'])
        assert difference <= document['tolerance_minor_units']
        assert edge['difference_minor_units'] == difference
        total += difference
    assert len(report['matching']) == report['objective']['matched_count']
    assert total == report['objective']['total_difference_minor_units']
    invoice_support = {row['invoice_id']: set(row['possible_receipt_ids'])
                       for row in report['invoices']}
    receipt_support = {row['receipt_id']: set(row['possible_invoice_ids'])
                       for row in report['receipts']}
    for edge in report['matching']:
        assert edge['receipt_id'] in invoice_support[edge['invoice_id']]
        assert edge['invoice_id'] in receipt_support[edge['receipt_id']]
