"""Exact work-order cost accounting against contract rate periods.

Splits every completed service interval on its contract rate boundaries,
prices each slice as an exact rational number of minor currency units, and
rounds only once per invoice.  Standard library only, Python >=3.11.
"""
