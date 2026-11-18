"""Validation, rate splitting and exact rational cost accounting.

Semantics (see ``docs/cost-contract.md``):

* a work interval is ``[start_tick, finish_tick)``; zero length is legal and
  needs no rate coverage;
* rate periods are ``[from_tick, to_tick)`` denominated in minor currency
  units per tick; for one ``(service_code, server)`` they never overlap;
* a positive work interval must be covered continuously by exactly one rate
  period at every tick; gaps are rejected instead of being extrapolated;
* each slice is priced as ``length * num / den`` using :class:`Fraction`,
  totals are summed exactly, and rounding happens once per invoice.
"""
import bisect
import math
from fractions import Fraction

from .common import (MINOR_UNITS_MAX, RATE_LIMIT, SEGMENT_LIMIT, SERVERS_MAX,
                     TIME_MAX, WORK_LIMIT, bounded_list, currency,
                     exact_object, ident, integer)


def _validate_rate(row, servers):
    exact_object(row, 'rate_id service_code server from_tick to_tick '
                      'currency minor_units_num minor_units_den')
    rate_id = ident(row['rate_id'])
    service_code = ident(row['service_code'])
    server = integer(row['server'], 0, servers - 1)
    from_tick = integer(row['from_tick'], 0, TIME_MAX)
    to_tick = integer(row['to_tick'], 0, TIME_MAX)
    if from_tick >= to_tick:
        raise ValueError('rate period must be non-empty (from_tick < to_tick)')
    code = currency(row['currency'])
    num = integer(row['minor_units_num'], 0, MINOR_UNITS_MAX)
    den = integer(row['minor_units_den'], 1, MINOR_UNITS_MAX)
    return {'rate_id': rate_id, 'service_code': service_code,
            'server': server, 'from_tick': from_tick, 'to_tick': to_tick,
            'currency': code, 'minor_units_num': num,
            'minor_units_den': den}


def validate(document):
    """Parse and structurally validate one cost source document."""
    exact_object(document, 'servers work_intervals rates')
    servers = integer(document['servers'], 1, SERVERS_MAX)

    works = []
    seen_works = set()
    for row in bounded_list(document['work_intervals'], WORK_LIMIT,
                            'work_intervals'):
        exact_object(row, 'work_id service_code server start_tick '
                          'finish_tick invoice_id')
        work_id = ident(row['work_id'])
        if work_id in seen_works:
            raise ValueError('duplicate work_id: ' + work_id)
        seen_works.add(work_id)
        service_code = ident(row['service_code'])
        server = integer(row['server'], 0, servers - 1)
        start_tick = integer(row['start_tick'], 0, TIME_MAX)
        finish_tick = integer(row['finish_tick'], 0, TIME_MAX)
        if start_tick > finish_tick:
            raise ValueError('work interval has start_tick > finish_tick')
        invoice_id = ident(row['invoice_id'])
        works.append({'work_id': work_id, 'service_code': service_code,
                      'server': server, 'start_tick': start_tick,
                      'finish_tick': finish_tick, 'invoice_id': invoice_id})

    rates = []
    seen_rates = set()
    for row in bounded_list(document['rates'], RATE_LIMIT, 'rates'):
        rate = _validate_rate(row, servers)
        if rate['rate_id'] in seen_rates:
            raise ValueError('duplicate rate_id: ' + rate['rate_id'])
        seen_rates.add(rate['rate_id'])
        rates.append(rate)

    _reject_overlaps(rates)
    return works, rates


def _reject_overlaps(rates):
    """For one (service_code, server) no two rate periods may overlap."""
    grouped = {}
    for rate in rates:
        grouped.setdefault((rate['service_code'], rate['server']),
                           []).append(rate)
    for items in grouped.values():
        items.sort(key=lambda r: (r['from_tick'], r['to_tick'], r['rate_id']))
        previous_end = None
        for rate in items:
            if previous_end is not None and rate['from_tick'] < previous_end:
                raise ValueError(
                    'overlapping rate periods for service_code/server: '
                    '%s/%d' % (rate['service_code'], rate['server']))
            previous_end = rate['to_tick']


def _rate_index(rates):
    """Per (service_code, server): rates sorted by boundary plus starts."""
    index = {}
    for rate in rates:
        index.setdefault((rate['service_code'], rate['server']),
                         []).append(rate)
    for key, items in index.items():
        items.sort(key=lambda r: (r['from_tick'], r['to_tick'], r['rate_id']))
        index[key] = (items, [rate['from_tick'] for rate in items])
    return index


def _covering_rate(items, starts, tick):
    """The unique left-closed rate containing ``tick``, or ``None``."""
    position = bisect.bisect_right(starts, tick) - 1
    if position < 0:
        return None
    rate = items[position]
    if rate['from_tick'] <= tick < rate['to_tick']:
        return rate
    return None


def split_work(work, rates):
    """Split one work interval into priced rate-boundary slices.

    Returns ``(segments, total, currency)`` with ``segments`` ordered by
    ``from_tick``, ``total`` their exact rational sum, and ``currency`` the
    single currency of the slices (``None`` when the work is zero length).
    """
    start, finish = work['start_tick'], work['finish_tick']
    segments = []
    total = Fraction(0)
    if start == finish:
        return segments, total, None
    entry = rates.get((work['service_code'], work['server']))
    if not entry:
        raise ValueError('no rate coverage for work_id: ' + work['work_id'])
    items, starts = entry
    currencies = set()
    cursor = start
    while cursor < finish:
        rate = _covering_rate(items, starts, cursor)
        if rate is None:
            raise ValueError('rate coverage gap at tick %d for work_id: %s'
                             % (cursor, work['work_id']))
        end = min(rate['to_tick'], finish)
        amount = (Fraction(end - cursor)
                  * Fraction(rate['minor_units_num'],
                             rate['minor_units_den']))
        segments.append({'rate_id': rate['rate_id'],
                         'from_tick': cursor, 'to_tick': end,
                         'exact_minor_units': str(amount)})
        currencies.add(rate['currency'])
        total += amount
        cursor = end
    if len(currencies) != 1:
        raise ValueError('work_id spans multiple currencies: '
                         + work['work_id'])
    return segments, total, currencies.pop()


def _round_half_up(value):
    """Nearest integer minor unit; an exact half rounds up (value >= 0)."""
    if value < 0:
        raise ValueError('cannot round a negative amount')
    return math.floor(value + Fraction(1, 2))


def analyze(document):
    """Validate a source document and return work-order and invoice detail."""
    works, rates = validate(document)
    index = _rate_index(rates)

    work_orders = []
    invoices = {}
    total_segments = 0
    for work in sorted(works, key=lambda item: item['work_id']):
        segments, total, code = split_work(work, index)
        total_segments += len(segments)
        if total_segments > SEGMENT_LIMIT:
            raise ValueError('report would contain more than %d rate slices'
                             % SEGMENT_LIMIT)
        record = invoices.setdefault(work['invoice_id'],
                                     {'currency': None, 'exact': Fraction(0)})
        if code is not None:
            if record['currency'] is not None and record['currency'] != code:
                raise ValueError('invoice_id mixes currencies: '
                                 + work['invoice_id'])
            record['currency'] = code
        record['exact'] += total
        work_orders.append({'work_id': work['work_id'],
                            'invoice_id': work['invoice_id'],
                            'currency': code,
                            'segments': segments,
                            'exact_minor_units': str(total)})
    invoice_rows = []
    for invoice_id in sorted(invoices):
        record = invoices[invoice_id]
        invoice_rows.append({
            'invoice_id': invoice_id,
            'currency': record['currency'],
            'exact_minor_units': str(record['exact']),
            'rounded_minor_units': _round_half_up(record['exact']),
        })

    return {'work_orders': work_orders, 'invoices': invoice_rows}
