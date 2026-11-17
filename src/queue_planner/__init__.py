"""Persistent rolling dispatch workbench (queue-planner).

Strict JSON/SQLite workbench that keeps pending, running and finished jobs
apart, replays apply requests idempotently and publishes exact, undoable
on-time acceptance suggestions.  Standard library only, Python >=3.11.
"""
