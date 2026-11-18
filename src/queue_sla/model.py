"""Validation and business-hours SLA accounting for service-center cases.

Semantics (see ``docs/sla-contract.md``):

* a calendar is a periodic window union P (period origin fixed at tick
  0) plus absolute open-exception O and closed-exception C interval
  unions; the effective open set is ``E = (P ∪ O) − C`` with closed
  always winning, so every unit interval ``[u, u+1)`` is either open or
  closed -- overlapping or adjacent pieces are one set, never counted
  twice;
* a case accrues, from ``opened_at``, the ticks of E that fall outside
  the union of that case's pause intervals; pauses may overlap and span
  closed stretches, and the accrual can never go negative because it is
  a set measure (``|E| − |E ∩ pauses|``), never a wall-clock subtraction;
* ``due`` is the smallest integer tick at which the accrual first
  reaches the budget, or ``null``; reaching the budget exactly at the
  right end of an open segment still expires at that endpoint.

Closed-form counting.  By set algebra,

    |E ∩ [0,t)| = |P ∩ [0,t)| + |O ∩ [0,t)| − |P∩O ∩ [0,t)|
                  − |C∩P ∩ [0,t)| − |C∩O ∩ [0,t)| + |C∩P∩O ∩ [0,t)|

Every exception term is a prefix over a *finite* merged interval union
whose pieces are each weighted by a closed-form periodic measure (the
number of periodic open ticks in a span), so a prefix query is one
binary search plus a partial piece -- there is no per-tick expansion
and no per-period iteration even when ``period == 1`` and
``horizon == 10**12``.  Per case the same identity is applied to
``pauses ∩ E``: only genuinely-open paused ticks stop the clock, which
is why a closed stretch overlapping a pause is removed at most once and
an open exception hidden inside a pause cannot add time.

Inversion.  Prefix accrual is a non-decreasing integer step function
over ticks computed by the exact same set representation, so the
earliest due tick is a binary search on that function rather than a
second, separately specified rule: counting and reverse lookup always
share one identical open-set semantics and can never drift apart.
"""
from bisect import bisect_left

from .common import (BUDGET_MAX, CALENDAR_LIMIT, CASE_LIMIT,
                     CHECKPOINT_LIMIT, CLOSED, EXCEPTION_LIMIT, OPEN,
                     PAUSE_LIMIT, PERIOD_MAX, TICK_MAX, WINDOW_LIMIT,
                     bounded_list, exact_object, ident, integer)


def _merge_intervals(intervals):
    """Union of half-open ``[a, b)`` integer intervals.

    Overlapping, adjacent and duplicate intervals become one sorted
    list of disjoint, non-adjacent intervals; adjacent pieces are
    merged because the set ``[0,2) ∪ [2,4)`` is the single set
    ``[0,4)`` and must be timed once.
    """
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1][1] = end
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _intersect_against(union, other):
    """Intersection of two disjoint, sorted half-open interval unions.

    For each piece of ``union`` only the pieces of ``other`` it can
    overlap are visited via a binary search, so the work is near-linear
    in the smaller structural size rather than a full cross product.
    """
    out = []
    if not union or not other:
        return out
    other_starts = [start for start, _ in other]
    j = bisect_left(other_starts, union[0][0])
    j = max(0, j - 1)
    for start, end in union:
        k = j
        while k < len(other) and other[k][0] < end:
            low, high = other[k]
            overlap_start = max(start, low)
            overlap_end = min(end, high)
            if overlap_start < overlap_end:
                out.append((overlap_start, overlap_end))
            if high >= end:
                break
            k += 1
        j = k
    return _merge_intervals(out)


class _WeightedTimeline:
    """Prefix measure of a finite union of weighted half-open pieces.

    Each merged piece ``[a, b)`` carries a non-negative per-tick
    weight (a plain length, or the calendar's periodic open indicator
    evaluated over that span).  ``prefix(t)`` sums whole pieces before
    ``t`` and one partial piece, in O(log pieces).
    """

    __slots__ = ('pieces', 'ends', 'prefixes', '_total')

    def __init__(self, pieces, weight):
        merged = _merge_intervals(pieces)
        self.pieces = merged
        ends = []
        prefixes = []
        running = 0
        for start, end in merged:
            ends.append(end)
            prefixes.append(running)
            running += weight(start, end)
        self.ends = ends
        self.prefixes = prefixes
        self._total = running

    def prefix(self, weight, tick):
        """Weighted measure inside ``[0, tick)``."""
        position = bisect_left(self.ends, tick)
        if position == len(self.ends):
            return self._total
        start, end = self.pieces[position]
        before = self.prefixes[position]
        if tick <= start:
            return before
        return before + weight(start, min(tick, end))


def _plain_weight(start, end):
    return end - start


class Calendar:
    """One validated business calendar with closed-form open prefixes."""

    __slots__ = ('id', 'period', 'windows', 'window_prefix',
                 'period_open', 'open_union', 'closed_union',
                 '_timelines')

    def __init__(self, calendar_id, period, windows, exceptions):
        self.id = calendar_id
        self.period = period
        merged_windows = _merge_intervals(windows)
        self.windows = merged_windows
        self.window_prefix = []
        running = 0
        for start, end in merged_windows:
            self.window_prefix.append((start, end, running))
            running += end - start
        self.period_open = running

        open_pieces = []
        closed_pieces = []
        for exception in exceptions:
            piece = (exception['from_tick'], exception['to_tick'])
            if exception['kind'] == OPEN:
                open_pieces.append(piece)
            else:
                closed_pieces.append(piece)
        self.open_union = _merge_intervals(open_pieces)
        self.closed_union = _merge_intervals(closed_pieces)

        # Every timeline is a finite exception union (or an
        # intersection of two finite unions) whose pieces are weighted
        # by a closed-form measure.  The periodic set P is never
        # materialised: it only appears through the periodic span
        # weight, so a 10^12-wide exception costs one interval, not
        # 10^12 periods.
        closed_and_open = _intersect_against(self.closed_union,
                                             self.open_union)
        span = self._periodic_span
        self._timelines = {
            'O': _WeightedTimeline(self.open_union, _plain_weight),
            'CP': _WeightedTimeline(self.closed_union, span),
            'CO': _WeightedTimeline(closed_and_open, _plain_weight),
            'CPO': _WeightedTimeline(closed_and_open, span),
            'PO': _WeightedTimeline(self.open_union, span)}

    def _window_measure(self, offset):
        """Open ticks of the periodic windows inside [0, offset)."""
        position = bisect_left(self.window_prefix, offset,
                               key=lambda item: item[1])
        if position == len(self.window_prefix):
            return self.period_open
        start, end, before = self.window_prefix[position]
        if offset <= start:
            return before
        return before + min(offset, end) - start

    def _periodic_span(self, start, end):
        """Measure of P inside the finite span ``[start, end)``."""
        return self._periodic_prefix(end) - self._periodic_prefix(start)

    def _periodic_prefix(self, tick):
        """Measure of the raw periodic set P inside ``[0, tick)``."""
        whole, offset = divmod(tick, self.period)
        return whole * self.period_open + self._window_measure(offset)

    def open_prefix(self, tick):
        """Number of effective open unit ticks inside ``[0, tick)``.

        Implements the inclusion-exclusion identity documented at the
        module head; closed precedence and union semantics are exact
        set measures, never raw interval lengths off the wall clock.
        """
        lines = self._timelines
        return (
            self._periodic_prefix(tick)
            + lines['O'].prefix(_plain_weight, tick)
            - lines['PO'].prefix(self._periodic_span, tick)
            - lines['CP'].prefix(self._periodic_span, tick)
            - lines['CO'].prefix(_plain_weight, tick)
            + lines['CPO'].prefix(self._periodic_span, tick))

    def paused_open_timeline(self, pause_union):
        """Weighted timelines measuring ``pauses ∩ E`` over the pauses.

        Applying the same inclusion-exclusion identity to the finite
        pause union yields exactly the open ticks a pause removes;
        closed-and-paused ticks are never deducted twice.  The periodic
        set enters only through the closed-form span weight, never by
        enumerating periods.
        """
        pause_and_open = _intersect_against(pause_union, self.open_union)
        pause_and_closed = _intersect_against(pause_union,
                                              self.closed_union)
        pause_closed_open = _intersect_against(pause_and_closed,
                                               self.open_union)
        span = self._periodic_span
        return {
            'P': _WeightedTimeline(pause_union, span),
            'O': _WeightedTimeline(pause_and_open, _plain_weight),
            'PO': _WeightedTimeline(pause_and_open, span),
            'CP': _WeightedTimeline(pause_and_closed, span),
            'CO': _WeightedTimeline(pause_closed_open, _plain_weight),
            'CPO': _WeightedTimeline(pause_closed_open, span)}

    def paused_open_prefix(self, lines, tick):
        """Measure of ``pauses ∩ E`` inside ``[0, tick)``."""
        return (lines['P'].prefix(self._periodic_span, tick)
                + lines['O'].prefix(_plain_weight, tick)
                - lines['PO'].prefix(self._periodic_span, tick)
                - lines['CP'].prefix(self._periodic_span, tick)
                - lines['CO'].prefix(_plain_weight, tick)
                + lines['CPO'].prefix(self._periodic_span, tick))


def _window_pair(value, period, what):
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError('%s must be a [from, to] array' % what)
    start = integer(value[0], 0, period)
    end = integer(value[1], 0, period)
    if start >= end:
        raise ValueError('%s has from_tick >= to_tick' % what)
    return start, end


def _validate(document):
    """Parse and structurally validate one SLA source document.

    Every calendar -- including calendars no case references -- and
    every pause/checkpoint are fully validated; duplicate identities
    are rejected even when the content is identical.
    """
    exact_object(document, 'horizon calendars cases')
    horizon = integer(document['horizon'], 0, TICK_MAX)

    calendar_rows = bounded_list(document['calendars'], CALENDAR_LIMIT,
                                 'calendars')
    if not calendar_rows:
        raise ValueError('calendars must be a non-empty array')
    total_windows = 0
    total_exceptions = 0
    calendars = {}
    for row in calendar_rows:
        exact_object(row, 'id period windows exceptions')
        calendar_id = ident(row['id'])
        if calendar_id in calendars:
            raise ValueError('duplicate calendar id: ' + calendar_id)
        period = integer(row['period'], 1, PERIOD_MAX)
        window_rows = bounded_list(row['windows'], WINDOW_LIMIT,
                                   'windows')
        total_windows += len(window_rows)
        if total_windows > WINDOW_LIMIT:
            raise ValueError('windows exceed %d items in total'
                             % WINDOW_LIMIT)
        windows = [_window_pair(item, period, 'window')
                   for item in window_rows]

        exception_rows = bounded_list(row['exceptions'], EXCEPTION_LIMIT,
                                      'exceptions')
        total_exceptions += len(exception_rows)
        if total_exceptions > EXCEPTION_LIMIT:
            raise ValueError('exceptions exceed %d items in total'
                             % EXCEPTION_LIMIT)
        seen_exceptions = set()
        exceptions = []
        for item in exception_rows:
            exact_object(item, 'id kind from_tick to_tick')
            exception_id = ident(item['id'])
            if exception_id in seen_exceptions:
                raise ValueError('duplicate exception id: ' + exception_id)
            seen_exceptions.add(exception_id)
            kind = item['kind']
            if kind not in (OPEN, CLOSED):
                raise ValueError('exception kind must be open or closed')
            from_tick = integer(item['from_tick'], 0, horizon)
            to_tick = integer(item['to_tick'], 0, horizon)
            if from_tick >= to_tick:
                raise ValueError('exception %s has from_tick >= to_tick'
                                 % exception_id)
            exceptions.append({'id': exception_id, 'kind': kind,
                               'from_tick': from_tick, 'to_tick': to_tick})
        calendars[calendar_id] = Calendar(calendar_id, period, windows,
                                          exceptions)

    case_rows = bounded_list(document['cases'], CASE_LIMIT, 'cases')
    total_pauses = 0
    total_checkpoints = 0
    cases = []
    seen_cases = set()
    for row in case_rows:
        exact_object(row, 'id calendar opened_at budget pauses '
                          'checkpoints')
        case_id = ident(row['id'])
        if case_id in seen_cases:
            raise ValueError('duplicate case id: ' + case_id)
        seen_cases.add(case_id)
        calendar_id = row['calendar']
        if not isinstance(calendar_id, str) or calendar_id not in calendars:
            raise ValueError('case %s references unknown calendar: %r'
                             % (case_id, calendar_id))
        opened_at = integer(row['opened_at'], 0, horizon)
        budget = integer(row['budget'], 0, BUDGET_MAX)

        pause_rows = bounded_list(row['pauses'], PAUSE_LIMIT, 'pauses')
        total_pauses += len(pause_rows)
        if total_pauses > PAUSE_LIMIT:
            raise ValueError('pauses exceed %d items in total'
                             % PAUSE_LIMIT)
        seen_pauses = set()
        pauses = []
        for item in pause_rows:
            exact_object(item, 'id from_tick to_tick')
            pause_id = ident(item['id'])
            if pause_id in seen_pauses:
                raise ValueError('duplicate pause id: ' + pause_id)
            seen_pauses.add(pause_id)
            from_tick = integer(item['from_tick'], opened_at, horizon)
            to_tick = integer(item['to_tick'], opened_at, horizon)
            if from_tick >= to_tick:
                raise ValueError('pause %s has from_tick >= to_tick'
                                 % pause_id)
            pauses.append((from_tick, to_tick))

        checkpoint_rows = bounded_list(row['checkpoints'],
                                       CHECKPOINT_LIMIT, 'checkpoints')
        total_checkpoints += len(checkpoint_rows)
        if total_checkpoints > CHECKPOINT_LIMIT:
            raise ValueError('checkpoints exceed %d items in total'
                             % CHECKPOINT_LIMIT)
        seen_checkpoints = set()
        checkpoints = []
        for item in checkpoint_rows:
            exact_object(item, 'id at_tick')
            checkpoint_id = ident(item['id'])
            if checkpoint_id in seen_checkpoints:
                raise ValueError('duplicate checkpoint id: '
                                 + checkpoint_id)
            seen_checkpoints.add(checkpoint_id)
            at_tick = integer(item['at_tick'], opened_at, horizon)
            checkpoints.append({'id': checkpoint_id, 'at_tick': at_tick})
        cases.append({'id': case_id, 'calendar': calendar_id,
                      'opened_at': opened_at, 'budget': budget,
                      'pauses': pauses, 'checkpoints': checkpoints})
    return horizon, calendars, cases


def _accrued(calendar, opened_at, paused_lines, tick):
    """Open, non-paused ticks of one case inside [opened_at, tick).

    Both the open measure and the pause measure are derived from the
    same effective open set, so the result is a non-negative set
    measure: closed ticks inside a pause were never in the open set and
    are therefore removed at most once.
    """
    open_ticks = calendar.open_prefix(tick) - \
        calendar.open_prefix(opened_at)
    paused_ticks = calendar.paused_open_prefix(
        paused_lines, tick) - calendar.paused_open_prefix(
        paused_lines, opened_at)
    return open_ticks - paused_ticks


def _earliest_due(calendar, opened_at, paused_lines, budget, horizon):
    """Smallest tick in [opened_at, horizon] reaching ``budget``.

    The accrual is a non-decreasing integer step function; binary
    search on the same prefix representation used for checkpoints
    keeps counting and reverse lookup on one identical set semantics.
    """
    if budget == 0:
        return opened_at
    if _accrued(calendar, opened_at, paused_lines, horizon) < budget:
        return None
    low, high = opened_at, horizon
    while low < high:
        middle = (low + high) // 2
        if _accrued(calendar, opened_at, paused_lines, middle) >= budget:
            high = middle
        else:
            low = middle + 1
    return low


def analyze(document):
    """Validate a source document and return the SLA report body.

    The returned dictionary has exactly the key ``cases``; the CLI
    adds ``schema_version`` and ``source_sha256`` on publication.
    Each case row depends only on its calendar's validated effective
    open set and its own pause union, never on input array order or on
    another case's pauses.
    """
    horizon, calendars, cases = _validate(document)
    rows = []
    for case in cases:
        calendar = calendars[case['calendar']]
        pause_union = _merge_intervals(case['pauses'])
        paused_lines = calendar.paused_open_timeline(pause_union)
        opened_at = case['opened_at']
        budget = case['budget']
        due = _earliest_due(calendar, opened_at, paused_lines, budget,
                            horizon)
        checkpoint_rows = []
        for checkpoint in sorted(case['checkpoints'],
                                 key=lambda item: item['id']):
            elapsed = _accrued(calendar, opened_at, paused_lines,
                               checkpoint['at_tick'])
            checkpoint_rows.append({
                'id': checkpoint['id'],
                'at_tick': checkpoint['at_tick'],
                'elapsed': elapsed,
                'remaining': max(0, budget - elapsed),
                'met': elapsed >= budget})
        rows.append({
            'id': case['id'],
            'calendar': case['calendar'],
            'opened_at': opened_at,
            'budget': budget,
            'due': due,
            'checkpoints': checkpoint_rows})
    rows.sort(key=lambda row: row['id'])
    return {'cases': rows}
