"""Independent oracle for queue-cost tests.

This module deliberately re-prices the raw source document without importing
``queue_cost.model``: it recomputes coverage by intersecting every rate period
with the work interval, sums with :class:`Fraction`, and rounds once per
invoice.  It is intentionally written from the contract text, not shared with
production code.
"""
from fractions import Fraction


def _rates_for(rates, service_code, server):
    return [rate for rate in rates
            if rate['service_code'] == service_code
            and rate['server'] == server]


def _price_work(rate_list, start, finish):
    """Return ``[(rate_id, from_tick, to_tick, Fraction)]`` sorted by start.

    Raises ``ValueError`` when a positive interval is not continuously
    covered.  Each tick must belong to exactly one rate; overlaps are an input
    error, not silently resolved.
    """
    if start == finish:
        return []
    pieces = []
    for rate in rate_list:
        low = max(start, rate['from_tick'])
        high = min(finish, rate['to_tick'])
        if low < high:
            pieces.append((low, high, rate))
    pieces.sort(key=lambda item: (item[0], item[1]))
    segments = []
    covered_until = start
    for low, high, rate in pieces:
        if low > covered_until:
            raise ValueError('gap')
        if low < covered_until:
            raise ValueError('overlap')
        amount = (Fraction(high - low)
                  * Fraction(rate['minor_units_num'],
                             rate['minor_units_den']))
        segments.append((rate['rate_id'], low, high, amount,
                         rate['currency']))
        covered_until = high
    if covered_until != finish:
        raise ValueError('gap')
    return segments


def round_half_up(value):
    """Nearest integer, ties away from zero (amounts are non-negative)."""
    whole = value.numerator // value.denominator
    remainder = value - whole
    if remainder >= Fraction(1, 2):
        whole += 1
    return whole


def price(document):
    """Return ``(work_orders, invoices)`` in canonical sorted order."""
    works = sorted(document['work_intervals'], key=lambda row: row['work_id'])
    orders = []
    invoices = {}
    for work in works:
        segments = _price_work(
            _rates_for(document['rates'], work['service_code'],
                       work['server']),
            work['start_tick'], work['finish_tick'])
        total = sum((item[3] for item in segments), Fraction(0))
        codes = {item[4] for item in segments}
        if len(codes) > 1:
            raise ValueError('mixed work currency')
        code = codes.pop() if codes else None
        orders.append({
            'work_id': work['work_id'],
            'invoice_id': work['invoice_id'],
            'currency': code,
            'segments': [{'rate_id': item[0], 'from_tick': item[1],
                          'to_tick': item[2],
                          'exact_minor_units': str(item[3])}
                         for item in segments],
            'exact_minor_units': str(total),
        })
        record = invoices.setdefault(work['invoice_id'],
                                     {'currency': None,
                                      'exact': Fraction(0)})
        if code is not None:
            if (record['currency'] is not None
                    and record['currency'] != code):
                raise ValueError('mixed invoice currency')
            record['currency'] = code
        record['exact'] += total
    invoice_rows = []
    for invoice_id in sorted(invoices):
        record = invoices[invoice_id]
        invoice_rows.append({
            'invoice_id': invoice_id,
            'currency': record['currency'],
            'exact_minor_units': str(record['exact']),
            'rounded_minor_units': round_half_up(record['exact']),
        })
    return orders, invoice_rows


def exact_fraction(text):
    return Fraction(text)
