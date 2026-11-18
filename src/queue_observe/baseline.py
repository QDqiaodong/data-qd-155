"""Validate a queue-replay report strictly, then recompute it with analyze().

The baseline is only a comparison anchor and a source of server count and
predicted durations.  It never adds actual clock constraints: predicted times
stay out of the difference-constraint graph.
"""
from fractions import Fraction

from queue_replay.calculation import simulate
from queue_replay.common import ident
from queue_replay.model import analyze

from .common import (DERIVED_MAX, JOB_LIMIT, SERVER_LIMIT, TIME_MAX,
                     bounded_list, canonical_fraction, integer, sha256_hex)

RESULT_FIELDS = 'jobs mean_wait max_wait horizon_start horizon_end servers'
JOB_FIELDS = 'id server arrival start finish wait service'
SERVER_FIELDS = 'server busy_ticks utilization'


def require_top(data):
    if not isinstance(data, dict) or set(data) != {
            'schema_version', 'source_sha256', 'result'}:
        raise ValueError('baseline must contain schema_version, '
                         'source_sha256 and result')
    integer(data['schema_version'], 1, 1)
    sha256_hex(data['source_sha256'])


def _job_fields(result):
    jobs = bounded_list(result['jobs'], JOB_LIMIT, 'baseline jobs')
    job_ids = set()
    previous = None
    for job in jobs:
        if not isinstance(job, dict) or set(job) != set(JOB_FIELDS.split()):
            raise ValueError('baseline job fields must be: ' + JOB_FIELDS)
        ident(job['id'])
        if job['id'] in job_ids:
            raise ValueError('duplicate baseline job id')
        job_ids.add(job['id'])
        integer(job['arrival'], 0, TIME_MAX)
        integer(job['service'], 0, 10 ** 9)
        integer(job['server'], 0, SERVER_LIMIT - 1)
        integer(job['start'], 0, DERIVED_MAX)
        integer(job['finish'], 0, DERIVED_MAX)
        integer(job['wait'], 0, DERIVED_MAX)
        if job['start'] != job['arrival'] + job['wait']:
            raise ValueError('baseline wait is inconsistent')
        if job['finish'] != job['start'] + job['service']:
            raise ValueError('baseline finish is inconsistent')
        key = (job['arrival'], job['id'])
        if previous is not None and key <= previous:
            raise ValueError('baseline jobs must follow FIFO arrival/id order')
        previous = key


def _summary_fields(result):
    jobs = result['jobs']
    if jobs:
        integer(result['max_wait'], 0, DERIVED_MAX)
        if result['mean_wait'] is None:
            raise ValueError('baseline mean_wait must not be null with jobs')
        mean = canonical_fraction(result['mean_wait'])
        if mean != Fraction(sum(job['wait'] for job in jobs), len(jobs)):
            raise ValueError('baseline mean_wait is inconsistent')
    elif result['mean_wait'] is not None or result['max_wait'] is not None:
        raise ValueError('baseline summary must be null without jobs')
    for name in ('horizon_start', 'horizon_end'):
        if result[name] is not None:
            integer(result[name], 0, DERIVED_MAX)
    if (result['horizon_start'] is None) != (result['horizon_end'] is None):
        raise ValueError('baseline horizon bounds must both be null or both set')
    if jobs and result['horizon_start'] is None:
        raise ValueError('baseline horizon must be set with jobs')
    if not jobs and result['horizon_start'] is not None:
        raise ValueError('baseline horizon must be null without jobs')
    if (result['horizon_start'] is not None
            and result['horizon_start'] > result['horizon_end']):
        raise ValueError('baseline horizon is inverted')


def _server_fields(result):
    servers = result['servers']
    if not isinstance(servers, list) or not 1 <= len(servers) <= SERVER_LIMIT:
        raise ValueError('baseline servers must hold 1..%d entries'
                         % SERVER_LIMIT)
    seen = set()
    for entry in servers:
        if not isinstance(entry, dict) or set(entry) != set(SERVER_FIELDS.split()):
            raise ValueError('baseline server fields must be: ' + SERVER_FIELDS)
        integer(entry['server'], 0, SERVER_LIMIT - 1)
        if entry['server'] in seen:
            raise ValueError('duplicate baseline server id')
        seen.add(entry['server'])
        integer(entry['busy_ticks'], 0, DERIVED_MAX)
        if entry['utilization'] is not None:
            canonical_fraction(entry['utilization'])
    if seen != set(range(len(servers))):
        raise ValueError('baseline servers must be numbered 0..n-1')


def validate_replay(data):
    """Return ``(server_count, result)`` after exact structural + FIFO checks."""
    require_top(data)
    result = data['result']
    if not isinstance(result, dict) or set(result) != set(RESULT_FIELDS.split()):
        raise ValueError('baseline result fields must be: ' + RESULT_FIELDS)
    _job_fields(result)
    _summary_fields(result)
    _server_fields(result)

    rebuilt = {'servers': len(result['servers']),
               'jobs': [{'id': job['id'], 'arrival': job['arrival'],
                         'service': job['service']}
                        for job in result['jobs']]}
    if analyze(rebuilt) != result:
        raise ValueError('baseline result is not the current FIFO contract')
    # A second, independent route through the simulator itself.
    if simulate(len(result['servers']), rebuilt['jobs']) != result:
        raise ValueError('baseline result is not reproducible')
    return len(result['servers']), result
