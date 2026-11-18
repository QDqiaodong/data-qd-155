"""Validation and point-in-time identity resolution for master data.

Semantics (see ``docs/identity-contract.md``):

* records are static ``(source, key)`` pairs with three optional field
  values; only links carry a validity interval ``[from_tick, to_tick)``;
* each query asks for the connected component of one record in the
  graph of the links active at its tick, the component's minimum
  ``(source, key)`` tuple (plain Python string tuple order, independent
  of source priority) and its record count;
* a component where some source contributes two or more records is
  quarantined: every such source is listed and no master field is
  published; otherwise each of ``display_name``, ``email``, ``phone``
  is resolved independently to the non-null member value with the
  smallest ``(priority, source, key)``, carrying the winning record key
  as lineage.

Algorithm: offline divide and conquer over time.  The distinct query
ticks become the leaves of a segment tree; every link is attached to
the O(log Q) canonical nodes covering the query ticks inside its
validity interval.  A depth-first walk applies the links of a node on
entry and rolls them back on exit, so each leaf is answered from
exactly the links active at its tick -- no per-tick expansion, no
per-query rescan of all links, no per-query component traversal.  The
structure is a rollback union-find whose per-root summary is the full
merge state (member count, minimum key, per-source record counts,
conflict mask and the three field winners), so undoing a link restores
the complete governance state of the split components, not only their
connectivity.  Sibling time branches are isolated because every branch
starts from the state checkpoint taken at its parent.
"""
from bisect import bisect_left

from .common import (LINK_LIMIT, PRIORITY_MAX, QUERY_LIMIT, RECORD_LIMIT,
                     SOURCE_LIMIT, TICK_MAX, bounded_list, exact_object,
                     ident, integer)

FIELDS = ('display_name', 'email', 'phone')
CLEAN = 'clean'
QUARANTINED = 'quarantined'


def _record_key(value, what):
    """A ``[source, key]`` record reference: exactly two strings."""
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('%s must be a [source, key] array' % what)
    source, key = value
    if not isinstance(source, str) or not isinstance(key, str):
        raise ValueError('%s must contain exactly two strings' % what)
    return source, key


def _field_value(value):
    """A master-data field: ``null`` or an identifier-shaped string."""
    if value is None:
        return None
    return ident(value)


def _validate(document):
    """Parse and structurally validate one identity source document.

    Every array may be empty; the whole document -- including links and
    records no query ever references -- is still fully validated.
    """
    exact_object(document, 'sources records links queries')

    rows = bounded_list(document['sources'], SOURCE_LIMIT, 'sources')
    if not rows:
        raise ValueError('sources must be a non-empty array')
    sources = {}
    for row in rows:
        exact_object(row, 'id priority')
        source_id = ident(row['id'])
        if source_id in sources:
            raise ValueError('duplicate source id: ' + source_id)
        sources[source_id] = integer(row['priority'], 0, PRIORITY_MAX)

    records = {}
    for row in bounded_list(document['records'], RECORD_LIMIT, 'records'):
        exact_object(row, 'source key display_name email phone')
        source = row['source']
        if not isinstance(source, str) or source not in sources:
            raise ValueError('record references unknown source: %r'
                             % (source,))
        key = ident(row['key'])
        if (source, key) in records:
            raise ValueError('duplicate record key: %r'
                             % ((source, key),))
        records[(source, key)] = {
            'display_name': _field_value(row['display_name']),
            'email': _field_value(row['email']),
            'phone': _field_value(row['phone'])}

    links = []
    seen_links = set()
    for row in bounded_list(document['links'], LINK_LIMIT, 'links'):
        exact_object(row, 'id left right from_tick to_tick')
        link_id = ident(row['id'])
        if link_id in seen_links:
            raise ValueError('duplicate link id: ' + link_id)
        seen_links.add(link_id)
        left = _record_key(row['left'], 'link left')
        right = _record_key(row['right'], 'link right')
        if left == right:
            raise ValueError('link %s connects a record to itself'
                             % link_id)
        for endpoint in (left, right):
            if endpoint not in records:
                raise ValueError('link %s references unknown record: %r'
                                 % (link_id, endpoint))
        from_tick = integer(row['from_tick'], 0, TICK_MAX)
        to_tick = integer(row['to_tick'], 0, TICK_MAX)
        if from_tick >= to_tick:
            raise ValueError('link %s has from_tick >= to_tick' % link_id)
        links.append({'left': left, 'right': right,
                      'from_tick': from_tick, 'to_tick': to_tick})

    queries = []
    seen_queries = set()
    for row in bounded_list(document['queries'], QUERY_LIMIT, 'queries'):
        exact_object(row, 'id record at_tick')
        query_id = ident(row['id'])
        if query_id in seen_queries:
            raise ValueError('duplicate query id: ' + query_id)
        seen_queries.add(query_id)
        record = _record_key(row['record'], 'query record')
        if record not in records:
            raise ValueError('query %s references unknown record: %r'
                             % (query_id, record))
        at_tick = integer(row['at_tick'], 0, TICK_MAX)
        queries.append({'id': query_id, 'record': record,
                        'at_tick': at_tick})

    return sources, records, links, queries


class _RollbackDsu:
    """Union-find with undo and the full per-component merge state.

    Each root carries the complete governance summary of its component:
    member ``count``, minimum ``(source, key)`` tuple, a per-source
    record-count dictionary, a conflict bitmask (bit ``s`` set iff
    source ``s`` contributes two or more records) and the three field
    winners ``(priority, source, key)``.  Every ``union`` pushes enough
    on the history stack for ``rollback`` to restore all of it, so a
    link expiring between two time branches splits the components back
    with their exact previous summaries -- never only the connectivity.
    """

    __slots__ = ('parent', 'size', 'count', 'min_key', 'winners',
                 'counts', 'conflicts', 'history')

    def __init__(self, members):
        # members: list of ((source, key), source_bit, field_winners).
        n = len(members)
        self.parent = list(range(n))
        self.size = [1] * n
        self.count = [1] * n
        self.min_key = [member[0] for member in members]
        self.winners = [member[2] for member in members]
        self.counts = [{member[1]: 1} for member in members]
        self.conflicts = [0] * n
        self.history = []

    def find(self, item):
        parent = self.parent
        while parent[item] != item:
            item = parent[item]
        return item

    def mark(self):
        return len(self.history)

    def union(self, left, right):
        root_a = self.find(left)
        root_b = self.find(right)
        history = self.history
        if root_a == root_b:
            history.append(None)
            return
        if self.size[root_a] < self.size[root_b]:
            root_a, root_b = root_b, root_a
        history.append((root_b, root_a, self.size[root_a],
                        self.count[root_a], self.min_key[root_a],
                        self.winners[root_a], self.conflicts[root_a]))
        counts_a = self.counts[root_a]
        both = 0
        for source_bit, number in self.counts[root_b].items():
            if source_bit in counts_a:
                counts_a[source_bit] += number
                both |= source_bit
            else:
                counts_a[source_bit] = number
        self.conflicts[root_a] = (self.conflicts[root_a]
                                  | self.conflicts[root_b] | both)
        if self.min_key[root_b] < self.min_key[root_a]:
            self.min_key[root_a] = self.min_key[root_b]
        winners_a = self.winners[root_a]
        winners_b = self.winners[root_b]
        self.winners[root_a] = tuple(
            a if b is None or (a is not None and a <= b) else b
            for a, b in zip(winners_a, winners_b))
        self.count[root_a] += self.count[root_b]
        self.size[root_a] += self.size[root_b]
        self.parent[root_b] = root_a

    def rollback(self, mark):
        history = self.history
        while len(history) > mark:
            entry = history.pop()
            if entry is None:
                continue
            (root_b, root_a, old_size, old_count, old_min_key,
             old_winners, old_conflicts) = entry
            self.parent[root_b] = root_b
            self.size[root_a] = old_size
            self.count[root_a] = old_count
            self.min_key[root_a] = old_min_key
            self.winners[root_a] = old_winners
            self.conflicts[root_a] = old_conflicts
            counts_a = self.counts[root_a]
            for source_bit, number in self.counts[root_b].items():
                remaining = counts_a[source_bit] - number
                if remaining:
                    counts_a[source_bit] = remaining
                else:
                    del counts_a[source_bit]


def _cover(tree, node, node_low, node_high, low, high, link_index):
    """Attach ``link_index`` to the canonical nodes of [low, high)."""
    if high <= node_low or node_high <= low:
        return
    if low <= node_low and node_high <= high:
        tree[node].append(link_index)
        return
    mid = (node_low + node_high) // 2
    _cover(tree, node * 2, node_low, mid, low, high, link_index)
    _cover(tree, node * 2 + 1, mid, node_high, low, high, link_index)


def resolve(document):
    """Validate a source document and return the identity report body.

    The returned dictionary has exactly the key ``queries``; the CLI
    adds ``schema_version`` and ``source_sha256`` on publication.  Each
    query row is computed from the links active at that query's tick,
    independently of the order queries appear in the input.
    """
    sources, records, links, queries = _validate(document)

    # Canonical record indexing: sorted (source, key) tuples, so the
    # report never depends on the input order of the records array.
    ordered_keys = sorted(records)
    index_of = {record_key: position
                for position, record_key in enumerate(ordered_keys)}
    source_ids = sorted(sources)
    source_bit = {source_id: 1 << position
                  for position, source_id in enumerate(source_ids)}
    members = []
    for record_key in ordered_keys:
        source, key = record_key
        row = records[record_key]
        priority = sources[source]
        winners = tuple(None if row[field] is None
                        else (priority, source, key) for field in FIELDS)
        members.append((record_key, source_bit[source], winners))
    endpoints = [(index_of[link['left']], index_of[link['right']])
                 for link in links]

    # Segment tree over the distinct query ticks: a link is attached to
    # the canonical nodes covering exactly the query ticks inside its
    # [from_tick, to_tick) validity interval.
    ticks = sorted({query['at_tick'] for query in queries})
    tick_index = {tick: position for position, tick in enumerate(ticks)}
    leaf_queries = [[] for _ in ticks]
    for position, query in enumerate(queries):
        leaf_queries[tick_index[query['at_tick']]].append(position)
    # Heap-indexed recursive tree: 4n nodes are the standard safe bound
    # for n leaves when n is not a power of two.
    tree = [[] for _ in range(4 * max(1, len(ticks)))]
    for link_index, link in enumerate(links):
        low = bisect_left(ticks, link['from_tick'])
        high = bisect_left(ticks, link['to_tick'])
        if low < high:
            _cover(tree, 1, 0, len(ticks), low, high, link_index)

    dsu = _RollbackDsu(members)
    rows = [None] * len(queries)

    def answer(position):
        query = queries[position]
        source, key = query['record']
        root = dsu.find(index_of[(source, key)])
        min_source, min_key = dsu.min_key[root]
        mask = dsu.conflicts[root]
        conflict_sources = sorted(
            source_id for source_id in source_ids
            if mask & source_bit[source_id])
        if mask:
            status = QUARANTINED
            fields = None
        else:
            status = CLEAN
            fields = {}
            for field, winner in zip(FIELDS, dsu.winners[root]):
                if winner is None:
                    fields[field] = None
                else:
                    _, win_source, win_key = winner
                    fields[field] = {
                        'value': records[(win_source, win_key)][field],
                        'record': [win_source, win_key]}
        rows[position] = {
            'id': query['id'],
            'at_tick': query['at_tick'],
            'record': [source, key],
            'canonical_record': [min_source, min_key],
            'member_count': dsu.count[root],
            'status': status,
            'conflict_sources': conflict_sources,
            'fields': fields}

    def walk(node, node_low, node_high):
        checkpoint = dsu.mark()
        for link_index in tree[node]:
            left, right = endpoints[link_index]
            dsu.union(left, right)
        if node_high - node_low == 1:
            for position in leaf_queries[node_low]:
                answer(position)
        else:
            mid = (node_low + node_high) // 2
            walk(node * 2, node_low, mid)
            walk(node * 2 + 1, mid, node_high)
        dsu.rollback(checkpoint)

    if ticks:
        walk(1, 0, len(ticks))

    # The report is sorted by query id; every fact in it depends only on
    # the validated business content, never on input array order.
    rows.sort(key=lambda row: row['id'])
    return {'queries': rows}
