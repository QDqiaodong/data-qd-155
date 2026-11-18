"""Strict contract helpers shared with the existing replay/planner packages.

The byte-bounded duplicate-key JSON reader, identifier rule and atomic report
publication are imported from :mod:`queue_replay.common`; the bool-rejecting
bounded integer check is imported from :mod:`queue_planner.common`.  Nothing
here opens the queue-planner SQLite database.
"""
import re
from fractions import Fraction

from queue_planner.common import integer
from queue_replay.common import ident, require

TIME_MAX = 10 ** 12
OFFSET_MIN = -10 ** 12
OFFSET_MAX = 10 ** 12
SERVICE_SEQ_MAX = 10 ** 9
CLOCK_LIMIT = 64
EVENT_LINE_LIMIT = 10000
EVENT_LIMIT = 6000
CALIBRATION_LIMIT = 512
QUERY_LIMIT = 128
JOB_LIMIT = 20000
SERVER_LIMIT = 100
# Derived replay values may legitimately exceed 10**12 (queued service time).
DERIVED_MAX = 10 ** 18

HEX64 = re.compile(r'[0-9a-f]{64}\Z')


def exact_object(value, fields):
    """Require exactly the named fields and return the object."""
    require(value, fields)
    return value


def bounded_list(value, limit, what):
    if not isinstance(value, list):
        raise ValueError('%s must be an array' % what)
    if len(value) > limit:
        raise ValueError('%s exceeds %d items' % (what, limit))
    return value


def sha256_hex(value):
    if not isinstance(value, str) or HEX64.fullmatch(value) is None:
        raise ValueError('expected a 64-character lowercase SHA-256 hex string')
    return value


def canonical_fraction(value):
    """Accept only the canonical ``str(Fraction(...))`` spelling.

    Rejects bool/int mixed types, ``+1``, ``01``, ``1.0``, ``10/12``, ``2/2``
    and negative denominators: the text must be exactly what Python's
    ``Fraction`` prints for its reduced value.
    """
    if not isinstance(value, str):
        raise ValueError('expected a canonical fraction string')
    try:
        parsed = Fraction(value)
    except (ValueError, ZeroDivisionError) as error:
        raise ValueError('expected a canonical fraction string') from error
    if str(parsed) != value:
        raise ValueError('non-canonical fraction string')
    return parsed
