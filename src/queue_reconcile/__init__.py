"""Cross-source receipt reconciliation against queue-cost invoices.

Pairs invoices computed by :mod:`queue_cost.model` with an independent
receipt export under a two-level exact objective (maximise pair count,
then minimise total absolute difference), and publishes the full
support of every record over all optimal matchings for manual
write-off.  Standard library only, Python >=3.11.
"""
