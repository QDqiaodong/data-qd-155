"""Independent per-tick oracle for queue-sla tests.

The oracle rebuilds every answer straight from the contract text: for
each integer unit tick it decides open/closed directly from the
periodic windows and the open/closed exceptions, decides paused/
running directly from the case's raw pause intervals, and walks the
tick line one unit at a time.  It never imports ``queue_sla`` or any
of its calendar, prefix or inversion helpers, and it never merges or
normalizes intervals the way production does, so an agreement is an
independent check rather than a shared derivation.

Only small instances are feasible because the whole horizon is
enumerated tick by tick -- that is the point of the oracle.
"""


def _is_open(document, calendar_id, tick):
    """The effective open indicator for one unit tick ``[tick, tick+1)``."""
    calendar = next(cal for cal in document['calendars']
                    if cal['id'] == calendar_id)
    period = calendar['period']
    if any(item['from_tick'] <= tick < item['to_tick']
           for item in calendar['exceptions']
           if item['kind'] == 'closed'):
        # closed always wins over both periodic windows and open ones.
        return False
    offset = tick % period
    if any(window[0] <= offset < window[1]
           for window in calendar['windows']):
        return True
    return any(item['from_tick'] <= tick < item['to_tick']
               for item in calendar['exceptions']
               if item['kind'] == 'open')


def _is_paused(case, tick):
    return any(pause['from_tick'] <= tick < pause['to_tick']
               for pause in case['pauses'])


def _elapsed_at(document, case, at_tick):
    """Consumed business ticks in [opened_at, at_tick), tick by tick."""
    consumed = 0
    for tick in range(case['opened_at'], at_tick):
        if _is_open(document, case['calendar'], tick) \
                and not _is_paused(case, tick):
            consumed += 1
    return consumed


def _earliest_due(document, case, horizon):
    """Smallest tick in [opened_at, horizon] reaching the budget."""
    budget = case['budget']
    if budget == 0:
        return case['opened_at']
    running = 0
    for tick in range(case['opened_at'], horizon):
        if _is_open(document, case['calendar'], tick) \
                and not _is_paused(case, tick):
            running += 1
            if running >= budget:
                return tick + 1
    return None


def expected_case(document, case, horizon):
    """One full expected case row, including every checkpoint."""
    checkpoints = []
    for checkpoint in case['checkpoints']:
        elapsed = _elapsed_at(document, case, checkpoint['at_tick'])
        checkpoints.append({
            'id': checkpoint['id'],
            'at_tick': checkpoint['at_tick'],
            'elapsed': elapsed,
            'remaining': max(0, case['budget'] - elapsed),
            'met': elapsed >= case['budget']})
    checkpoints.sort(key=lambda row: row['id'])
    return {
        'id': case['id'],
        'calendar': case['calendar'],
        'opened_at': case['opened_at'],
        'budget': case['budget'],
        'due': _earliest_due(document, case, horizon),
        'checkpoints': checkpoints}


def solve(document):
    """The ``{"cases": [...]}`` report body, sorted by case id."""
    rows = [expected_case(document, case, document['horizon'])
            for case in document['cases']]
    rows.sort(key=lambda row: row['id'])
    return {'cases': rows}
