"""Strict contract helpers local to queue-cost.

The byte-bounded duplicate-key reader, identifier rule and atomic report
publication are shared with the existing packages; the bool-rejecting bounded
integer check comes from :mod:`queue_planner.common`.  Nothing here opens the
queue-planner SQLite database or talks to a network.
"""
import re

from queue_planner.common import integer
from queue_replay.common import ident, parse_bytes, require, save

TIME_MAX = 10 ** 12
SERVERS_MAX = 100
WORK_LIMIT = 20000
RATE_LIMIT = 100000
MINOR_UNITS_MAX = 10 ** 18
# Bound the published report so an adversarial but in-size source cannot
# expand into an unbounded slice list after parsing.
SEGMENT_LIMIT = 1_000_000
BYTE_LIMIT = 8_000_000
CURRENCY = re.compile(r'[A-Z]{3}\Z')
SCHEMA_VERSION = 1


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


def currency(value):
    """Exactly three uppercase ASCII letters, no whitespace or digits."""
    if not isinstance(value, str) or CURRENCY.fullmatch(value) is None:
        raise ValueError('currency must be three uppercase ASCII letters')
    return value
