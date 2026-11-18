"""Strict validation of the observation log (LOG.json).

Structural defects (unknown/missing fields, references to undeclared clocks,
duplicate event ids with conflicting values, duplicate (job, kind), ...) are
input errors and must exit 2.  Nothing here decides feasibility.
"""
from .common import (CALIBRATION_LIMIT, CLOCK_LIMIT, EVENT_LIMIT,
                     EVENT_LINE_LIMIT, OFFSET_MAX, OFFSET_MIN, QUERY_LIMIT,
                     SERVICE_SEQ_MAX, TIME_MAX, bounded_list, exact_object,
                     ident, integer)

LOG_FIELDS = 'reference_clock clocks events calibrations queries'
KINDS = ('arrival', 'start', 'finish')
METRICS = ('wait', 'service', 'sojourn')
EVENT_FIELDS = 'event_id job kind clock tick server service_seq'


def _clock_bounds(value, label):
    if not isinstance(value, dict) or set(value) != {'id', 'min_offset', 'max_offset'}:
        raise ValueError('%s must contain exactly id, min_offset, max_offset' % label)
    ident(value['id'])
    minimum = integer(value['min_offset'], OFFSET_MIN, OFFSET_MAX)
    maximum = integer(value['max_offset'], OFFSET_MIN, OFFSET_MAX)
    if minimum > maximum:
        raise ValueError('%s has min_offset > max_offset' % label)
    return value['id'], minimum, maximum


def _parse_clocks(raw):
    items = bounded_list(raw, CLOCK_LIMIT, 'clocks')
    if not 1 <= len(items) <= CLOCK_LIMIT:
        raise ValueError('clocks must contain 1..%d entries' % CLOCK_LIMIT)
    clocks = {}
    for index, item in enumerate(items):
        cid, low, high = _clock_bounds(item, 'clock #%d' % index)
        if cid == '@reference':
            raise ValueError('clock id must not equal the fixed node @reference')
        if cid in clocks:
            raise ValueError('duplicate clock id: ' + cid)
        clocks[cid] = (low, high)
    return clocks


def _event_value(kind, server, seq, servers):
    if kind == 'arrival':
        if server is not None or seq is not None:
            raise ValueError('arrival server and service_seq must be null')
        return None, None
    if server is None or seq is None:
        raise ValueError('%s server and service_seq must be integers' % kind)
    return integer(server, 0, servers - 1), integer(seq, 0, SERVICE_SEQ_MAX)


def _parse_event(item, clocks, servers):
    exact_object(item, EVENT_FIELDS)
    event_id = ident(item['event_id'])
    job = ident(item['job'])
    kind = item['kind']
    if kind not in KINDS:
        raise ValueError('event kind must be arrival, start or finish')
    clock = item['clock']
    if not isinstance(clock, str) or clock not in clocks:
        raise ValueError('event references undeclared clock: ' + str(clock))
    tick = integer(item['tick'], -TIME_MAX, TIME_MAX)
    server, seq = _event_value(kind, item['server'], item['service_seq'], servers)
    return {'event_id': event_id, 'job': job, 'kind': kind, 'clock': clock,
            'tick': tick, 'server': server, 'service_seq': seq}


def _merge_events(raw, clocks, servers):
    """Deduplicate identical events; conflicting duplicates exit 2."""
    lines = bounded_list(raw, EVENT_LINE_LIMIT, 'events')
    events = {}
    for item in lines:
        event = _parse_event(item, clocks, servers)
        key = event['event_id']
        if key in events:
            if events[key] != event:
                raise ValueError('conflicting duplicate event_id: ' + key)
            continue
        events[key] = event
    if len(events) > EVENT_LIMIT:
        raise ValueError('events exceed %d unique entries' % EVENT_LIMIT)
    return events


def _parse_calibrations(raw, events):
    items = bounded_list(raw, CALIBRATION_LIMIT, 'calibrations')
    out = []
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {
                'id', 'from_event', 'to_event', 'min_delta', 'max_delta'}:
            raise ValueError('calibration #%d has wrong fields' % index)
        cid = ident(item['id'])
        if cid in seen:
            raise ValueError('duplicate calibration id: ' + cid)
        seen.add(cid)
        source, target = item['from_event'], item['to_event']
        for ref in (source, target):
            if not isinstance(ref, str) or ref not in events:
                raise ValueError('calibration references unknown event: ' + str(ref))
        low = integer(item['min_delta'], -TIME_MAX, TIME_MAX)
        high = integer(item['max_delta'], -TIME_MAX, TIME_MAX)
        if low > high:
            raise ValueError('calibration min_delta > max_delta')
        out.append({'id': cid, 'from_event': source, 'to_event': target,
                    'min_delta': low, 'max_delta': high})
    return out


def _parse_queries(raw, job_ids):
    items = bounded_list(raw, QUERY_LIMIT, 'queries')
    out = []
    seen = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict) or set(item) != {'id', 'job', 'metric'}:
            raise ValueError('query #%d has wrong fields' % index)
        qid = ident(item['id'])
        if qid in seen:
            raise ValueError('duplicate query id: ' + qid)
        seen.add(qid)
        job = item['job']
        if not isinstance(job, str) or job not in job_ids:
            raise ValueError('query references unknown job: ' + str(job))
        metric = item['metric']
        if metric not in METRICS:
            raise ValueError('query metric must be wait, service or sojourn')
        out.append({'id': qid, 'job': job, 'metric': metric})
    return out


def _jobs_and_pairs(events):
    """Group events by job, enforce lifecycle and per-server sequence uniqueness."""
    jobs = {}
    pairs = {}
    for event in events.values():
        kinds = jobs.setdefault(event['job'], {})
        if event['kind'] in kinds:
            raise ValueError('job %s has more than one %s event'
                             % (event['job'], event['kind']))
        kinds[event['kind']] = event
    for job_id, kinds in jobs.items():
        if set(kinds) != set(KINDS):
            raise ValueError('job %s must have arrival, start and finish' % job_id)
        start, finish = kinds['start'], kinds['finish']
        if start['server'] != finish['server'] or start['service_seq'] != finish['service_seq']:
            raise ValueError('job %s start/finish server pair differs' % job_id)
    for event in events.values():
        if event['kind'] == 'arrival':
            continue
        key = (event['server'], event['service_seq'])
        if key in pairs and pairs[key] != event['job']:
            raise ValueError('server/service_seq reused by different jobs')
        pairs[key] = event['job']
    return jobs


def parse_log(data, servers):
    """Return a normalized log model; ``servers`` comes from the baseline."""
    if not isinstance(data, dict) or set(data) != set(LOG_FIELDS.split()):
        raise ValueError('log must contain exactly: ' + LOG_FIELDS)
    clocks = _parse_clocks(data['clocks'])
    reference = data['reference_clock']
    if not isinstance(reference, str) or reference not in clocks:
        raise ValueError('reference_clock must reference a declared clock')
    if clocks[reference] != (0, 0):
        raise ValueError('reference_clock bounds must be exactly [0, 0]')
    events = _merge_events(data['events'], clocks, servers)
    jobs = _jobs_and_pairs(events)
    calibrations = _parse_calibrations(data['calibrations'], events)
    queries = _parse_queries(data['queries'], set(jobs))
    return {'clocks': clocks, 'reference': reference, 'events': events,
            'jobs': jobs, 'calibrations': calibrations, 'queries': queries}
