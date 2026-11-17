"""Shared strict contract helpers.

The strict JSON reader, identifier rule and atomic report publication are
reused from queue_replay.common so both commands share exactly the same
input/output safety boundary (duplicate keys, NaN/Infinity, symlink output,
atomic replace).  This module adds the bounded integer check that rejects
bool explicitly, because ``type(True) is int`` is False.
"""
from queue_replay.common import ident, read, require, save

TIME_MAX = 10**12
SERVICE_MAX = 10**9
VALUE_MAX = 10**9
PENDING_LIMIT = 12
LOAD_LIMIT = 12
FINISH_LIMIT = 3
JOB_TOTAL_LIMIT = 10000


def integer(value, minimum, maximum):
    """Bounded integer; bool is never accepted."""
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('integer outside allowed bounds')
    return value


def exact_object(value, fields):
    require(value, fields)
    return value


def server_list(value, servers):
    if not isinstance(value, list) or not value:
        raise ValueError('eligible must be a non-empty array')
    out = []
    for item in value:
        integer(item, 0, servers - 1)
        if item in out:
            raise ValueError('duplicate eligible server')
        out.append(item)
    return out
