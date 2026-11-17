"""SQLite persistence for the rolling dispatch workbench.

One database file holds the current clock (now), the monotonically
increasing revision, every job fact (pending/running/done) and the
irreversible dispatch facts (server, actual_start, actual_finish) plus the
idempotent request ledger.  All mutations happen in one deferred-to-IMMEDIATE
transaction so a process can be closed and reopened and continue, and two
concurrent writers with the same expected_revision cannot both commit.
"""
import json
import os
import sqlite3
from pathlib import Path

APPLICATION_ID = 0x7170_6C6E  # 'qpln'

SCHEMA = """
CREATE TABLE meta(
    id INTEGER PRIMARY KEY CHECK (id = 1),
    servers INTEGER NOT NULL,
    now INTEGER NOT NULL,
    revision INTEGER NOT NULL
);
CREATE TABLE jobs(
    id TEXT PRIMARY KEY,
    arrival INTEGER NOT NULL,
    service INTEGER NOT NULL,
    deadline INTEGER NOT NULL,
    value INTEGER NOT NULL,
    eligible TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','running','done')),
    server INTEGER,
    actual_start INTEGER,
    actual_finish INTEGER
);
CREATE TABLE requests(
    request_id TEXT PRIMARY KEY,
    source_sha256 TEXT NOT NULL,
    op TEXT NOT NULL,
    revision INTEGER NOT NULL
);
"""


def ensure_distinct_path(db_path, other_path):
    if other_path is not None and Path(db_path) == Path(other_path):
        raise ValueError('database path must differ from input/report path')


def reject_symlink(db_path):
    if Path(db_path).is_symlink():
        raise ValueError('database path cannot be a symlink')


def initialize(db_path, servers, now):
    """Create a brand new database; existing paths and symlinks are refused."""
    reject_symlink(db_path)
    path = Path(db_path)
    if path.exists():
        raise ValueError('database already exists')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        # PRAGMA values cannot be parameter-bound; APPLICATION_ID is a fixed
        # internal constant.
        conn.execute(f'PRAGMA application_id = {APPLICATION_ID}')
        conn.executescript(SCHEMA)
        conn.execute('INSERT INTO meta(id, servers, now, revision) VALUES (1, ?, ?, 0)',
                     (servers, now))
        conn.commit()
    except BaseException:
        conn.close()
        try:
            path.unlink()
        except OSError:
            pass
        raise
    conn.close()


def connect(db_path):
    reject_symlink(db_path)
    path = Path(db_path)
    if not path.exists():
        raise ValueError('database does not exist')
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout = 10000')
    conn.execute('PRAGMA foreign_keys = ON')
    app_id = conn.execute('PRAGMA application_id').fetchone()[0]
    if app_id != APPLICATION_ID:
        conn.close()
        raise ValueError('not a queue-planner database')
    return conn


def connect_readonly(db_path):
    reject_symlink(db_path)
    path = Path(db_path)
    if not path.exists():
        raise ValueError('database does not exist')
    uri = 'file:' + str(path.resolve()).replace('?', '%3F').replace('#', '%23')
    conn = sqlite3.connect(uri + '?mode=ro', uri=True, timeout=10.0)
    conn.row_factory = sqlite3.Row
    app_id = conn.execute('PRAGMA application_id').fetchone()[0]
    if app_id != APPLICATION_ID:
        conn.close()
        raise ValueError('not a queue-planner database')
    return conn


def snapshot(conn):
    """Atomic read of the whole workbench state."""
    servers, now, revision = conn.execute(
        'SELECT servers, now, revision FROM meta WHERE id = 1').fetchone()
    jobs = []
    for row in conn.execute('SELECT * FROM jobs ORDER BY id'):
        job = dict(row)
        job['eligible'] = json.loads(job['eligible'])
        jobs.append(job)
    return {'servers': servers, 'now': now, 'revision': revision, 'jobs': jobs}


def begin_write(conn):
    conn.execute('BEGIN IMMEDIATE')


def lookup_request(conn, request_id):
    row = conn.execute(
        'SELECT request_id, revision FROM requests WHERE request_id = ?',
        (request_id,)).fetchone()
    return None if row is None else dict(row)


def insert_request(conn, request_id, digest, op, revision):
    conn.execute(
        'INSERT INTO requests(request_id, source_sha256, op, revision) '
        'VALUES (?, ?, ?, ?)', (request_id, digest, op, revision))


def insert_job(conn, job):
    conn.execute(
        'INSERT INTO jobs(id, arrival, service, deadline, value, eligible, '
        'status, server, actual_start, actual_finish) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)',
        (job['id'], job['arrival'], job['service'], job['deadline'],
         job['value'], json.dumps(job['eligible'], separators=(',', ':')),
         'pending'))


def advance_now(conn, now):
    conn.execute('UPDATE meta SET now = ? WHERE id = 1', (now,))


def mark_finished(conn, jid, actual_finish):
    conn.execute(
        "UPDATE jobs SET status = 'done', actual_finish = ? WHERE id = ?",
        (actual_finish, jid))


def mark_started(conn, jid, server, now):
    conn.execute(
        "UPDATE jobs SET status = 'running', server = ?, actual_start = ? "
        'WHERE id = ?', (server, now, jid))


def mark_zero_done(conn, jid, server, now):
    conn.execute(
        "UPDATE jobs SET status = 'done', server = ?, actual_start = ?, "
        'actual_finish = ? WHERE id = ?', (server, now, now, jid))


def bump_revision(conn):
    conn.execute('UPDATE meta SET revision = revision + 1 WHERE id = 1')
    return conn.execute('SELECT revision FROM meta WHERE id = 1').fetchone()[0]
