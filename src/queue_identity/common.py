"""Strict contract helpers local to queue-identity.

The byte-bounded duplicate-key reader, identifier rule, bool-rejecting
bounded integer check and atomic report publication are shared with the
existing packages (queue_replay.common, queue_planner.common,
queue_cost.common).  Nothing here opens the queue-planner SQLite
database, reads the reports of the other commands or talks to a
network.
"""
from queue_cost.common import bounded_list, exact_object
from queue_planner.common import integer
from queue_replay.common import ident, parse_bytes, save

SOURCE_LIMIT = 64
RECORD_LIMIT = 10000
LINK_LIMIT = 30000
QUERY_LIMIT = 30000
PRIORITY_MAX = 10 ** 9
TICK_MAX = 10 ** 12
BYTE_LIMIT = 8_000_000
SCHEMA_VERSION = 1
