"""Independent oracle for queue-observe tests.

This module deliberately re-implements the raw LOG semantics directly from the
input document: no production feasibility code (``queue_observe.solution``) is
imported or consulted.  It is used to (a) enumerate every clock assignment in a
small bounded box, (b) check reported intervals and endpoint witnesses, and
(c) validate a reported conflict cycle against the raw inequalities.
"""
import itertools

REFERENCE = '@reference'
TIME_MAX = 10 ** 12


def normalize_events(document):
    """Deduplicate identical events / reject conflicting ones (raw view)."""
    unique = {}
    for row in document['events']:
        key = row['event_id']
        if key in unique and unique[key] != row:
            raise ValueError('conflicting duplicate event')
        unique[key] = dict(row)
    return unique


def clocks(document):
    return {item['id']: (item['min_offset'], item['max_offset'])
            for item in document['clocks']}


def jobs_by_id(events):
    grouped = {}
    for event in events.values():
        grouped.setdefault(event['job'], {})[event['kind']] = event
    return grouped


def calibrations(document):
    by_id = {}
    for item in document['calibrations']:
        by_id[item['id']] = item
    return by_id


def raw_constraints(document):
    """Every semantic inequality as (kind, payload) for independent checks."""
    events = normalize_events(document)
    return {
        'clocks': clocks(document),
        'reference': document['reference_clock'],
        'events': events,
        'jobs': jobs_by_id(events),
        'calibrations': calibrations(document),
    }


def assignment_ok(document, offsets, servers):
    """Check one complete offset assignment against every raw inequality."""
    data = raw_constraints(document)
    off = offsets
    if set(off) != set(data['clocks']):
        return False
    reference = data['reference']
    if off[reference] != 0:
        return False
    for clock, (low, high) in data['clocks'].items():
        if not isinstance(off[clock], int) or isinstance(off[clock], bool):
            return False
        if not low <= off[clock] <= high:
            return False
    for event in data['events'].values():
        moment = event['tick'] + off[event['clock']]
        if not 0 <= moment <= TIME_MAX:
            return False
    for cal in data['calibrations'].values():
        low_event = data['events'][cal['from_event']]
        high_event = data['events'][cal['to_event']]
        delta = ((high_event['tick'] + off[high_event['clock']])
                 - (low_event['tick'] + off[low_event['clock']]))
        if not cal['min_delta'] <= delta <= cal['max_delta']:
            return False
    for kinds in data['jobs'].values():
        arrival = kinds['arrival']['tick'] + off[kinds['arrival']['clock']]
        start = kinds['start']['tick'] + off[kinds['start']['clock']]
        finish = kinds['finish']['tick'] + off[kinds['finish']['clock']]
        if not arrival <= start <= finish:
            return False
    per_server = {}
    for job_id, kinds in data['jobs'].items():
        per_server.setdefault(kinds['start']['server'], []).append(job_id)
    for server, job_ids in per_server.items():
        ordered = sorted(job_ids,
                         key=lambda jid: data['jobs'][jid]['start']['service_seq'])
        for previous, following in zip(ordered, ordered[1:]):
            prev_finish = (data['jobs'][previous]['finish']['tick']
                           + off[data['jobs'][previous]['finish']['clock']])
            next_start = (data['jobs'][following]['start']['tick']
                          + off[data['jobs'][following]['start']['clock']])
            if prev_finish > next_start:
                return False
    return True


def enumerate_assignments(document, servers):
    """Brute force every offset tuple inside the declared clock box."""
    data = raw_constraints(document)
    names = sorted(data['clocks'])
    ranges = [range(0, 1) if name == data['reference']
              else range(data['clocks'][name][0], data['clocks'][name][1] + 1)
              for name in names]
    feasible = []
    for values in itertools.product(*ranges):
        offsets = dict(zip(names, values))
        if assignment_ok(document, offsets, servers):
            feasible.append(offsets)
    return feasible


def metric_value(document, offsets, job_id, metric):
    kinds = jobs_by_id(normalize_events(document))[job_id]
    arrival = kinds['arrival']['tick'] + offsets[kinds['arrival']['clock']]
    start = kinds['start']['tick'] + offsets[kinds['start']['clock']]
    finish = kinds['finish']['tick'] + offsets[kinds['finish']['clock']]
    if metric == 'wait':
        return start - arrival
    if metric == 'service':
        return finish - start
    return finish - arrival


def expected_bounds(document, servers, queries):
    feasible = enumerate_assignments(document, servers)
    if not feasible:
        return None
    bounds = {}
    for query in queries:
        values = [metric_value(document, offsets, query['job'],
                               query['metric']) for offsets in feasible]
        bounds[query['id']] = (min(values), max(values))
    return bounds


def expected_predicted(baseline, job_id, metric):
    jobs = {job['id']: job for job in baseline['result']['jobs']}
    job = jobs[job_id]
    if metric == 'wait':
        return job['wait']
    if metric == 'service':
        return job['service']
    return job['finish'] - job['arrival']


def _inequality(document, constraint_id):
    """Recompute the (from, to, bound) implied by one raw constraint id."""
    data = raw_constraints(document)
    kind = constraint_id[0]
    if kind == 'clock':
        _tag, clock, side = constraint_id
        low, high = data['clocks'][clock]
        if side == 'min':
            return clock, REFERENCE, -low
        if side == 'max':
            return REFERENCE, clock, high
    elif kind == 'event':
        _tag, event_id, side = constraint_id
        event = data['events'][event_id]
        if side == 'min':
            return event['clock'], REFERENCE, event['tick']
        if side == 'max':
            return REFERENCE, event['clock'], TIME_MAX - event['tick']
    elif kind == 'cal':
        _tag, cal_id, side = constraint_id
        cal = data['calibrations'][cal_id]
        source = data['events'][cal['from_event']]
        target = data['events'][cal['to_event']]
        delta = target['tick'] - source['tick']
        if side == 'min':
            return target['clock'], source['clock'], delta - cal['min_delta']
        if side == 'max':
            return source['clock'], target['clock'], cal['max_delta'] - delta
    elif kind == 'job':
        _tag, job_id, side = constraint_id
        kinds = data['jobs'][job_id]
        if side == 'arrival_start':
            return (kinds['start']['clock'], kinds['arrival']['clock'],
                    kinds['start']['tick'] - kinds['arrival']['tick'])
        if side == 'start_finish':
            return (kinds['finish']['clock'], kinds['start']['clock'],
                    kinds['finish']['tick'] - kinds['start']['tick'])
    elif kind == 'server':
        _tag, server, previous, following = constraint_id
        prev_finish = data['jobs'][previous]['finish']
        next_start = data['jobs'][following]['start']
        if next_start['server'] != server:
            raise AssertionError('server evidence names the wrong server')
        return (next_start['clock'], prev_finish['clock'],
                next_start['tick'] - prev_finish['tick'])
    raise AssertionError('unknown constraint id: %r' % (constraint_id,))


def verify_cycle(document, cycle):
    """Independently check a reported conflict cycle; returns its sum."""
    assert cycle, 'cycle must not be empty'
    for edge in cycle:
        expected_from, expected_to, expected_bound = _inequality(
            document, edge['constraint_id'])
        assert edge['from'] == expected_from, edge
        assert edge['to'] == expected_to, edge
        assert edge['bound'] == expected_bound, edge
    for position, edge in enumerate(cycle):
        following = cycle[(position + 1) % len(cycle)]
        assert edge['to'] == following['from'], (edge, following)
    total = sum(edge['bound'] for edge in cycle)
    assert total < 0, total
    return total


def verify_witness(document, offsets, servers, job_id, metric, expected):
    assert set(offsets) == set(clocks(document))
    assert all(type(value) is int for value in offsets.values())
    assert assignment_ok(document, offsets, servers)
    assert metric_value(document, offsets, job_id, metric) == expected
