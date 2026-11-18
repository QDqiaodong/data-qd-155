"""Validation and exact two-level optimal receipt reconciliation.

Semantics (see ``docs/reconcile-contract.md``):

* invoices are produced exclusively by :func:`queue_cost.model.analyze`;
  their ``invoice_id``, ``currency`` and ``rounded_minor_units`` become
  the receivable records (never re-rounded per work order, never the
  exact fraction in place of the final rounded amount);
* a receipt can settle one invoice when both currencies agree, the
  absolute difference of minor units does not exceed
  ``tolerance_minor_units`` and the receipt's ``eligible_invoices``
  admits the invoice; a ``None`` invoice currency never has candidates;
* the reconciliation first maximises the number of pairs and then
  minimises the total absolute difference, compared as exact big
  integers with no weight folding and no floating point;
* the published support of every record is the union of its
  counterparties over *all* two-level optimal matchings, computed
  exactly through zero-reduced-cost residual cycles (no enumeration of
  matchings, no expansion by amount units).
"""
from heapq import heappop, heappush

from queue_cost.model import analyze as _analyze_cost

from .common import (ELIGIBLE_LIMIT, INVOICE_LIMIT, MINOR_UNITS_MAX,
                     RECEIPT_LIMIT, bounded_list, currency, exact_object,
                     ident, integer)

STATUS_FIXED = 'fixed'
STATUS_AMBIGUOUS = 'ambiguous'
STATUS_UNMATCHED = 'unmatched'


def _validate(document):
    """Parse and structurally validate one reconciliation source."""
    exact_object(document, 'cost_source receipts tolerance_minor_units')
    tolerance = integer(document['tolerance_minor_units'], 0,
                        MINOR_UNITS_MAX)
    rows = bounded_list(document['receipts'], RECEIPT_LIMIT, 'receipts')
    receipts = []
    seen = set()
    for row in rows:
        exact_object(row, 'receipt_id currency minor_units '
                          'eligible_invoices')
        receipt_id = ident(row['receipt_id'])
        if receipt_id in seen:
            raise ValueError('duplicate receipt_id: ' + receipt_id)
        seen.add(receipt_id)
        receipts.append({'receipt_id': receipt_id,
                         'currency': currency(row['currency']),
                         'minor_units': integer(row['minor_units'], 0,
                                                MINOR_UNITS_MAX),
                         'eligible_invoices': row['eligible_invoices']})
    return document['cost_source'], receipts, tolerance


def _resolve_eligibility(receipts, invoice_ids):
    """Check eligibility references and freeze them into sets (or None).

    ``null`` admits every invoice and stays ``None``; an empty array
    admits none and becomes an empty set.  References to invoices that
    do not exist reject the whole document, as do duplicates.
    """
    for receipt in receipts:
        eligible = receipt['eligible_invoices']
        if eligible is None:
            continue
        bounded_list(eligible, ELIGIBLE_LIMIT, 'eligible_invoices')
        seen = set()
        for item in eligible:
            if not isinstance(item, str) or item not in invoice_ids:
                raise ValueError('eligible_invoices references unknown '
                                 'invoice_id: %r' % (item,))
            if item in seen:
                raise ValueError('duplicate invoice_id in '
                                 'eligible_invoices: ' + item)
            seen.add(item)
        receipt['eligible_invoices'] = seen


def _candidates(invoices, receipts, tolerance):
    """All legally pairable ``(invoice, receipt, difference)`` triples.

    Indices refer to the canonical (identifier-sorted) input lists.
    """
    candidates = []
    for i, invoice in enumerate(invoices):
        code = invoice['currency']
        if code is None:
            continue  # a null-currency invoice never has candidates
        amount = invoice['rounded_minor_units']
        invoice_id = invoice['invoice_id']
        for r, receipt in enumerate(receipts):
            if receipt['currency'] != code:
                continue
            eligible = receipt['eligible_invoices']
            if eligible is not None and invoice_id not in eligible:
                continue
            difference = abs(amount - receipt['minor_units'])
            if difference <= tolerance:
                candidates.append((i, r, difference))
    return candidates


def _add_arc(graph, tail, head, cost):
    """Append a unit-capacity residual arc pair; return the forward arc.

    Arcs are mutable ``[head, reverse_index, capacity, cost]`` lists so
    the flow update can adjust capacities in place.
    """
    forward = [head, len(graph[head]), 1, cost]
    graph[tail].append(forward)
    graph[head].append([tail, len(graph[tail]) - 1, 0, -cost])
    return forward


def _min_cost_max_flow(n, m, candidates):
    """Exact min-cost max-flow by successive shortest augmenting paths.

    Node layout: invoices ``0..n-1``, receipts ``n..n+m-1``, source
    ``n+m``, sink ``n+m+1``.  Every augmentation raises the flow value
    by one along a shortest reduced-cost path, keeping the invariant
    that the flow is minimum-cost for its value; when no augmenting
    path remains, the flow is a maximum matching with minimum total
    difference.  The lexicographic (count, then total difference)
    objective is therefore honoured structurally, with exact big
    integers and no weight folding.

    Returns ``(graph, potentials, edge_arcs, source_arcs, sink_arcs)``.
    On return every residual arc has a non-negative reduced cost (dual
    feasibility), which the support computation relies on.
    """
    source = n + m
    sink = source + 1
    size = sink + 1
    graph = [[] for _ in range(size)]
    source_arcs = [_add_arc(graph, source, i, 0) for i in range(n)]
    edge_arcs = [(i, r, difference, _add_arc(graph, i, n + r, difference))
                 for i, r, difference in candidates]
    sink_arcs = [_add_arc(graph, n + r, sink, 0) for r in range(m)]

    potentials = [0] * size
    while True:
        dist = [None] * size
        prev = [None] * size
        dist[source] = 0
        heap = [(0, source)]
        while heap:
            d, u = heappop(heap)
            if d != dist[u]:
                continue  # stale heap entry
            if u == sink:
                break
            for ai, arc in enumerate(graph[u]):
                if not arc[2]:
                    continue
                v = arc[0]
                nd = d + arc[3] + potentials[u] - potentials[v]
                if dist[v] is None or nd < dist[v]:
                    dist[v] = nd
                    prev[v] = (u, ai)
                    heappush(heap, (nd, v))
        if dist[sink] is None:
            return graph, potentials, edge_arcs, source_arcs, sink_arcs
        dt = dist[sink]
        # Feasible potential update that stays exact while allowing the
        # Dijkstra loop to stop as soon as the sink is settled: nodes
        # whose final distance is unknown get the sink distance, which
        # is a lower bound on it, so reduced costs stay non-negative.
        for v in range(size):
            reached = dist[v]
            potentials[v] += dt if reached is None else min(reached, dt)
        v = sink
        while v != source:
            u, ai = prev[v]
            arc = graph[u][ai]
            arc[2] -= 1
            graph[v][arc[1]][2] += 1
            v = u


def _strong_components(zero_adj):
    """Kosaraju strong-component ids for a directed graph (iterative)."""
    size = len(zero_adj)
    seen = [False] * size
    order = []
    for start in range(size):
        if seen[start]:
            continue
        seen[start] = True
        stack = [(start, 0)]
        while stack:
            v, ai = stack[-1]
            if ai < len(zero_adj[v]):
                w = zero_adj[v][ai]
                stack[-1] = (v, ai + 1)
                if not seen[w]:
                    seen[w] = True
                    stack.append((w, 0))
            else:
                order.append(v)
                stack.pop()
    reverse = [[] for _ in range(size)]
    for v, outs in enumerate(zero_adj):
        for w in outs:
            reverse[w].append(v)
    comp = [-1] * size
    count = 0
    for start in reversed(order):
        if comp[start] != -1:
            continue
        comp[start] = count
        stack = [start]
        while stack:
            v = stack.pop()
            for w in reverse[v]:
                if comp[w] == -1:
                    comp[w] = count
                    stack.append(w)
        count += 1
    return comp


def _optimal_support(n, m, graph, potentials, edge_arcs, source_arcs,
                     sink_arcs):
    """Exact support over all two-level optimal matchings.

    With dual-feasible potentials every residual arc has a
    non-negative reduced cost, so another matching of the same
    cardinality is optimal iff it differs from the computed one by a
    circulation of zero-reduced-cost residual arcs, i.e. by directed
    cycles in the zero reduced-cost residual graph.  Hence:

    * an unmatched candidate edge appears in some optimal matching iff
      its arc has zero reduced cost and closes a zero-cost cycle (its
      endpoints share a strong component);
    * a matched invoice can be left unmatched iff its backward arc to
      the source has zero reduced cost and shares a strong component
      with the source (the arc then lies on a zero-cost cycle);
      symmetrically a matched receipt uses its backward arc from the
      sink;
    * edges of the computed optimum are supported by that optimum
      itself.
    """
    source = n + m
    sink = source + 1
    size = sink + 1
    zero_adj = [[] for _ in range(size)]
    for u in range(size):
        potential = potentials[u]
        for arc in graph[u]:
            if arc[2] and arc[3] + potential - potentials[arc[0]] == 0:
                zero_adj[u].append(arc[0])
    comp = _strong_components(zero_adj)

    pairs = []
    support = set()
    matched_invoices = set()
    matched_receipts = set()
    for i, r, difference, arc in edge_arcs:
        if arc[2] == 0:  # the computed optimum itself uses this edge
            pairs.append((i, r, difference))
            matched_invoices.add(i)
            matched_receipts.add(r)
            support.add((i, r))
        elif (arc[3] + potentials[i] - potentials[n + r] == 0
              and comp[i] == comp[n + r]):
            support.add((i, r))
    free_invoices = set(range(n)) - matched_invoices
    free_receipts = set(range(m)) - matched_receipts
    for i in matched_invoices:
        backward = graph[i][source_arcs[i][1]]
        if (backward[2]
                and backward[3] + potentials[i] - potentials[source] == 0
                and comp[i] == comp[source]):
            free_invoices.add(i)
    for r in matched_receipts:
        backward = graph[sink][sink_arcs[r][1]]
        if (backward[2]
                and backward[3] + potentials[sink] - potentials[n + r] == 0
                and comp[n + r] == comp[sink]):
            free_receipts.add(r)
    return pairs, support, free_invoices, free_receipts


def _status(possible, can_be_unmatched):
    """Classification fully determined by the all-optimal support."""
    if not possible:
        return STATUS_UNMATCHED
    if len(possible) == 1 and not can_be_unmatched:
        return STATUS_FIXED
    return STATUS_AMBIGUOUS


def reconcile(document):
    """Validate a source document and return the reconciliation report.

    The returned dictionary has exactly the keys ``objective``,
    ``matching``, ``invoices`` and ``receipts``; the CLI adds
    ``schema_version`` and ``source_sha256`` on publication.
    """
    cost_source, receipts, tolerance = _validate(document)
    result = _analyze_cost(cost_source)
    invoices = result['invoices']  # sorted by invoice_id, bill-rounded
    if len(invoices) > INVOICE_LIMIT:
        raise ValueError('more than %d distinct invoice_id values'
                         % INVOICE_LIMIT)
    _resolve_eligibility(receipts,
                         {row['invoice_id'] for row in invoices})
    receipts = sorted(receipts, key=lambda row: row['receipt_id'])
    candidates = _candidates(invoices, receipts, tolerance)
    n = len(invoices)
    m = len(receipts)
    flow = _min_cost_max_flow(n, m, candidates)
    pairs, support, free_i, free_r = _optimal_support(n, m, *flow)
    invoice_possible = [set() for _ in range(n)]
    receipt_possible = [set() for _ in range(m)]
    for i, r in support:
        invoice_possible[i].add(r)
        receipt_possible[r].add(i)

    matching = [{'invoice_id': invoices[i]['invoice_id'],
                 'receipt_id': receipts[r]['receipt_id'],
                 'difference_minor_units': difference}
                for i, r, difference in pairs]
    matching.sort(key=lambda row: (row['invoice_id'], row['receipt_id']))
    objective = {
        'matched_count': len(pairs),
        'total_difference_minor_units':
            sum(row['difference_minor_units'] for row in matching),
    }

    invoice_rows = []
    for i, invoice in enumerate(invoices):
        possible = sorted(receipts[r]['receipt_id']
                          for r in invoice_possible[i])
        can_be_unmatched = i in free_i
        invoice_rows.append({
            'invoice_id': invoice['invoice_id'],
            'currency': invoice['currency'],
            'rounded_minor_units': invoice['rounded_minor_units'],
            'possible_receipt_ids': possible,
            'can_be_unmatched': can_be_unmatched,
            'status': _status(possible, can_be_unmatched)})
    receipt_rows = []
    for r, receipt in enumerate(receipts):
        possible = sorted(invoices[i]['invoice_id']
                          for i in receipt_possible[r])
        can_be_unmatched = r in free_r
        receipt_rows.append({
            'receipt_id': receipt['receipt_id'],
            'currency': receipt['currency'],
            'minor_units': receipt['minor_units'],
            'possible_invoice_ids': possible,
            'can_be_unmatched': can_be_unmatched,
            'status': _status(possible, can_be_unmatched)})
    return {'objective': objective, 'matching': matching,
            'invoices': invoice_rows, 'receipts': receipt_rows}
