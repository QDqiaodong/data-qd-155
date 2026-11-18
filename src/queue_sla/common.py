"""Strict contract helpers local to queue-sla.

The byte-bounded duplicate-key reader, identifier rule (identifiers are
kept as bare strings, never concatenated into a composite identity),
bool-rejecting bounded integer check and atomic report publication are
reused from the existing packages so every queue-* command shares the
same input/output safety boundary.  Nothing here opens a database,
talks to a network or keeps a persistent work-order lifecycle.
"""
from queue_cost.common import bounded_list, exact_object
from queue_planner.common import integer
from queue_replay.common import ident, parse_bytes, save

TICK_MAX = 10 ** 12
PERIOD_MAX = 10 ** 9
BUDGET_MAX = 10 ** 12
CALENDAR_LIMIT = 64
CASE_LIMIT = 5000
WINDOW_LIMIT = 10000
EXCEPTION_LIMIT = 30000
PAUSE_LIMIT = 30000
CHECKPOINT_LIMIT = 30000
BYTE_LIMIT = 8_000_000
SCHEMA_VERSION = 1
OPEN = 'open'
CLOSED = 'closed'
