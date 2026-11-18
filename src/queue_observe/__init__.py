"""Cross-device clock log observation (queue-observe).

Reconstructs provable intervals for wait/service/sojourn from asynchronously
timestamped arrival, start and finish logs, anchored on a verified queue-replay
baseline.  Standard library only, Python >=3.11.
"""
