"""Report assembly: feasible interval bounds with witnesses, or a conflict cycle."""
from .baseline import validate_replay
from .log import parse_log
from .solution import REFERENCE, solve

SCHEMA_VERSION = 1


def _offsets(matrix, row, index, node_list):
    """Feasible integer potential with ``offset[@reference] == 0``.

    For difference constraints ``dist(row, v)`` is a feasible potential; after
    subtracting ``dist(row, @reference)`` the fixed node sits at zero and every
    difference (hence every query metric) is unchanged.
    """
    distances = matrix[row]
    shift = distances[index[REFERENCE]]
    result = {name: distances[index[name]] - shift
              for name in node_list if name != REFERENCE}
    if any(abs(value) >= 10 ** 25 for value in result.values()):
        raise RuntimeError('clock graph produced an unbounded potential')
    return result


def metric_clocks(job, metric):
    """Return ``(minus, plus, base)``: metric = base + offset[plus] - offset[minus]."""
    arrival, start, finish = job['arrival'], job['start'], job['finish']
    if metric == 'wait':
        return (arrival['clock'], start['clock'],
                start['tick'] - arrival['tick'])
    if metric == 'service':
        return (start['clock'], finish['clock'],
                finish['tick'] - start['tick'])
    return (arrival['clock'], finish['clock'],
            finish['tick'] - arrival['tick'])


def predicted(baseline_job, metric):
    if metric == 'wait':
        return baseline_job['wait']
    if metric == 'service':
        return baseline_job['service']
    return baseline_job['finish'] - baseline_job['arrival']


def compute_query(log, query, matrix, index, node_list, baseline_jobs):
    job = log['jobs'][query['job']]
    minus, plus, base = metric_clocks(job, query['metric'])
    # offset[plus] - offset[minus] <= dist(minus -> plus); the lower extreme is
    # -dist(plus -> minus).  Both are attained by a shortest-path potential
    # shifted so offset[@reference] == 0, hence each witness satisfies every
    # clock bound, event time bound, calibration, lifecycle and occupancy edge.
    lowest = base - matrix[index[plus]][index[minus]]
    highest = base + matrix[index[minus]][index[plus]]
    low_offsets = _offsets(matrix, index[plus], index, node_list)
    high_offsets = _offsets(matrix, index[minus], index, node_list)
    baseline_job = baseline_jobs[query['job']]
    estimate = predicted(baseline_job, query['metric'])
    return {
        'id': query['id'],
        'job': query['job'],
        'metric': query['metric'],
        'min': lowest,
        'max': highest,
        'min_offsets': low_offsets,
        'max_offsets': high_offsets,
        'predicted': estimate,
        'delta_min': lowest - estimate,
        'delta_max': highest - estimate,
    }


def observe(source_data, log_sha256, baseline_data, baseline_sha256):
    """Build the observation report from already-parsed JSON documents.

    ``log_sha256`` and ``baseline_sha256`` are digests of the actual input
    bytes; ``baseline_source_sha256`` is copied from the baseline's own
    declaration and is *not* re-verified against any original replay source.
    """
    servers, result = validate_replay(baseline_data)
    log = parse_log(source_data, servers)
    baseline_jobs = {job['id']: job for job in result['jobs']}
    for job_id in log['jobs']:
        if job_id not in baseline_jobs:
            raise ValueError('observed job is absent from the baseline: ' + job_id)

    matrix, index, node_list, cycle = solve(log)
    report = {'schema_version': SCHEMA_VERSION,
              'log_sha256': log_sha256,
              'baseline_sha256': baseline_sha256,
              'baseline_source_sha256': baseline_data['source_sha256']}
    if cycle is not None:
        report['feasible'] = False
        report['conflict_cycle'] = cycle
        return report
    report['feasible'] = True
    report['queries'] = [
        compute_query(log, query, matrix, index, node_list, baseline_jobs)
        for query in sorted(log['queries'], key=lambda item: item['id'])
    ]
    return report
