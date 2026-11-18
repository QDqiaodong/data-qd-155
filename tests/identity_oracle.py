"""Independent oracle for queue-identity tests.

Recomputes every query row straight from the contract text: for each
query it collects the links active at that tick, walks the queried
record's connected component with an explicit stack, and derives the
canonical key, member count, conflict sources and per-field winners
directly from the component members.  Nothing here imports
``queue_identity`` production code or any of its aggregation helpers;
the graph is rebuilt from scratch for every single query, which is
only feasible for small instances.
"""

FIELDS = ('display_name', 'email', 'phone')


def _component(document, start, at_tick):
    """The full connected component of ``start`` among active links."""
    adjacency = {}
    for link in document['links']:
        if link['from_tick'] <= at_tick < link['to_tick']:
            left = (link['left'][0], link['left'][1])
            right = (link['right'][0], link['right'][1])
            adjacency.setdefault(left, set()).add(right)
            adjacency.setdefault(right, set()).add(left)
    seen = set()
    stack = [start]
    while stack:
        node = stack.pop()
        if node in seen:
            continue
        seen.add(node)
        stack.extend(adjacency.get(node, ()))
    return seen


def expected_row(document, query):
    """One report row, derived per the contract without production code."""
    priorities = {row['id']: row['priority'] for row in document['sources']}
    records = {(row['source'], row['key']): row
               for row in document['records']}
    start = (query['record'][0], query['record'][1])
    component = _component(document, start, query['at_tick'])
    canonical = min(component)
    per_source = {}
    for source, _ in component:
        per_source[source] = per_source.get(source, 0) + 1
    conflicts = sorted(source for source, number in per_source.items()
                       if number >= 2)
    if conflicts:
        status = 'quarantined'
        fields = None
    else:
        status = 'clean'
        fields = {}
        for field in FIELDS:
            best = None
            for source, key in component:
                value = records[(source, key)][field]
                if value is None:
                    continue
                candidate = (priorities[source], source, key)
                if best is None or candidate < best[0]:
                    best = (candidate, value)
            fields[field] = None if best is None else {
                'value': best[1],
                'record': [best[0][1], best[0][2]]}
    return {'id': query['id'],
            'at_tick': query['at_tick'],
            'record': [start[0], start[1]],
            'canonical_record': [canonical[0], canonical[1]],
            'member_count': len(component),
            'status': status,
            'conflict_sources': conflicts,
            'fields': fields}


def solve(document):
    """The ``{"queries": [...]}`` report body, sorted by query id."""
    rows = [expected_row(document, query) for query in document['queries']]
    rows.sort(key=lambda row: row['id'])
    return {'queries': rows}
