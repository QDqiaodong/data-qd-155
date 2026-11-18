"""Strict contract helpers local to queue-reconcile.

The byte-bounded duplicate-key reader, identifier rule, currency check,
bool-rejecting bounded integer check and atomic report publication are
shared with the existing packages (queue_replay.common,
queue_planner.common, queue_cost.common).  Nothing here opens the
queue-planner SQLite database or talks to a network.
"""
from queue_cost.common import bounded_list, currency, exact_object
from queue_planner.common import integer
from queue_replay.common import ident, parse_bytes, save

MINOR_UNITS_MAX = 10 ** 36
INVOICE_LIMIT = 300
RECEIPT_LIMIT = 300
ELIGIBLE_LIMIT = 300
BYTE_LIMIT = 8_000_000
SCHEMA_VERSION = 1
