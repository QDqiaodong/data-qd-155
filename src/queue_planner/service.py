"""Validation and transactional application services for queue-planner."""
from .common import (FINISH_LIMIT, JOB_TOTAL_LIMIT, LOAD_LIMIT,
                     PENDING_LIMIT, SERVICE_MAX, TIME_MAX, VALUE_MAX,
                     exact_object, ident, integer, server_list)
from . import store
from .planner import optimize

REVISION_MAX = 2**63 - 1


# ---------------------------------------------------------------- sources
def validate_init(data):
    exact_object(data, 'servers now')
    servers = integer(data['servers'], 1, 3)
    now = integer(data['now'], 0, TIME_MAX)
    return servers, now


def validate_envelope(data):
    exact_object(data, 'request_id expected_revision op payload')
    request_id = ident(data['request_id'])
    expected_revision = integer(data['expected_revision'], 0, REVISION_MAX)
    op = data['op']
    if op not in ('load', 'observe', 'start'):
        raise ValueError('op must be load, observe or start')
    if not isinstance(data['payload'], dict):
        raise ValueError('payload must be an object')
    return request_id, expected_revision, op, data['payload']


# ---------------------------------------------------------------- snapshot
def _index(snap):
    jobs = {j['id']: j for j in snap['jobs']}
    pending = [j for j in snap['jobs'] if j['status'] == 'pending']
    running = {j['server']: (j['actual_start'], j['service'])
               for j in snap['jobs'] if j['status'] == 'running'}
    return jobs, pending, running


# ---------------------------------------------------------------- load
def _validate_load(payload, snap, jobs_by_id):
    exact_object(payload, 'jobs')
    rows = payload['jobs']
    if not isinstance(rows, list) or not 1 <= len(rows) <= LOAD_LIMIT:
        raise ValueError('load requires 1..12 jobs')
    seen = set()
    prepared = []
    for row in rows:
        exact_object(row, 'id arrival service deadline value eligible')
        jid = ident(row['id'])
        if jid in seen or jid in jobs_by_id:
            raise ValueError('duplicate or reused job id: ' + jid)
        seen.add(jid)
        arrival = integer(row['arrival'], 0, TIME_MAX)
        deadline = integer(row['deadline'], 0, TIME_MAX)
        if arrival > deadline:
            raise ValueError('arrival must be <= deadline')
        service = integer(row['service'], 0, SERVICE_MAX)
        value = integer(row['value'], 1, VALUE_MAX)
        eligible = server_list(row['eligible'], snap['servers'])
        prepared.append({'id': jid, 'arrival': arrival, 'service': service,
                         'deadline': deadline, 'value': value,
                         'eligible': eligible})
    pending_now = sum(1 for j in snap['jobs'] if j['status'] == 'pending')
    if pending_now + len(prepared) > PENDING_LIMIT:
        raise ValueError('pending queue capacity is 12')
    if len(snap['jobs']) + len(prepared) > JOB_TOTAL_LIMIT:
        raise ValueError('database job capacity is 10000')
    return prepared


# ---------------------------------------------------------------- observe
def _validate_observe(payload, snap, jobs_by_id):
    exact_object(payload, 'now finishes')
    new_now = integer(payload['now'], 0, TIME_MAX)
    if new_now < snap['now']:
        raise ValueError('observe now must be monotonic')
    finishes = payload['finishes']
    if not isinstance(finishes, list) or len(finishes) > FINISH_LIMIT:
        raise ValueError('finishes must be an array of 0..3 items')
    seen = set()
    prepared = []
    for row in finishes:
        exact_object(row, 'id actual_finish')
        jid = row['id']
        if not isinstance(jid, str) or jid in seen:
            raise ValueError('finish id missing or duplicated')
        seen.add(jid)
        job = jobs_by_id.get(jid)
        if job is None or job['status'] != 'running':
            raise ValueError('finish reported for a non-running job: ' + str(jid))
        actual_finish = integer(row['actual_finish'], 0, TIME_MAX)
        if not job['actual_start'] <= actual_finish <= new_now:
            raise ValueError('actual_finish outside [actual_start, now]')
        prepared.append((jid, actual_finish))
    return new_now, prepared


# ---------------------------------------------------------------- start
def _validate_start(payload, snap, jobs_by_id, running):
    exact_object(payload, 'jobs')
    ids = payload['jobs']
    if not isinstance(ids, list) or not ids:
        raise ValueError('start requires a non-empty id array')
    if len(set(ids)) != len(ids):
        raise ValueError('start ids must be unique')
    pending = [j for j in snap['jobs'] if j['status'] == 'pending']
    plan = optimize(snap['now'], snap['servers'], pending, running)
    by_id = {s['id']: s for s in plan['suggestions']}
    now = snap['now']
    prepared = []
    positive_on_server = set()
    for jid in ids:
        if not isinstance(jid, str):
            raise ValueError('start ids must be strings')
        suggestion = by_id.get(jid)
        job = jobs_by_id.get(jid)
        if job is None or job['status'] != 'pending':
            raise ValueError('job not startable: ' + jid)
        if suggestion is None or suggestion['start'] != now:
            raise ValueError('job not allowed to start at now by the '
                             'current optimal plan: ' + jid)
        sid = suggestion['server']
        if sid in running:
            raise ValueError('server still occupied by a running job')
        if job['service'] > 0:
            if sid in positive_on_server:
                raise ValueError('at most one positive-service job per '
                                 'server per tick')
            positive_on_server.add(sid)
        prepared.append((jid, sid, job['service']))
    return prepared


# ---------------------------------------------------------------- apply
def apply_request(conn, data, digest):
    request_id, expected_revision, op, payload = validate_envelope(data)
    store.begin_write(conn)
    try:
        replay = store.lookup_request(conn, request_id)
        if replay is not None:
            stored = conn.execute(
                'SELECT source_sha256 FROM requests WHERE request_id = ?',
                (request_id,)).fetchone()[0]
            if stored != digest:
                raise ValueError('request_id reused with different source bytes')
            conn.commit()
            return {'request_id': request_id, 'revision': replay['revision']}

        snap = store.snapshot(conn)
        if snap['revision'] != expected_revision:
            raise ValueError('expected_revision does not match current revision')
        jobs_by_id, _pending, running = _index(snap)

        if op == 'load':
            prepared = _validate_load(payload, snap, jobs_by_id)
            for job in prepared:
                store.insert_job(conn, job)
        elif op == 'observe':
            new_now, prepared = _validate_observe(payload, snap, jobs_by_id)
            for jid, actual_finish in prepared:
                store.mark_finished(conn, jid, actual_finish)
            store.advance_now(conn, new_now)
        else:
            prepared = _validate_start(payload, snap, jobs_by_id, running)
            for jid, sid, service in prepared:
                if service == 0:
                    store.mark_zero_done(conn, jid, sid, snap['now'])
                else:
                    store.mark_started(conn, jid, sid, snap['now'])

        new_revision = store.bump_revision(conn)
        store.insert_request(conn, request_id, digest, op, new_revision)
        conn.commit()
        return {'request_id': request_id, 'revision': new_revision}
    except BaseException:
        conn.rollback()
        raise


# ---------------------------------------------------------------- plan
def build_report(conn):
    snap = store.snapshot(conn)
    pending = [j for j in snap['jobs'] if j['status'] == 'pending']
    running = {j['server']: (j['actual_start'], j['service'])
               for j in snap['jobs'] if j['status'] == 'running'}
    plan = optimize(snap['now'], snap['servers'], pending, running)

    report_jobs = []
    for job in sorted(snap['jobs'], key=lambda j: j['id']):
        report_jobs.append({
            'id': job['id'],
            'arrival': job['arrival'],
            'service': job['service'],
            'deadline': job['deadline'],
            'value': job['value'],
            'eligible': sorted(job['eligible']),
            'status': job['status'],
            'server': job['server'],
            'actual_start': job['actual_start'],
            'actual_finish': job['actual_finish'],
        })
    return {
        'schema_version': 1,
        'revision': snap['revision'],
        'now': snap['now'],
        'servers': snap['servers'],
        'jobs': report_jobs,
        'suggestions': plan['suggestions'],
        'unselected': plan['unselected'],
        'value_total': plan['value_total'],
        'wait_total': plan['wait_total'],
    }
