"""Difference-constraint model and exact integer bound solving.

Every clock contributes one integer variable ``offset[clock]``.  A fixed node
``@reference`` stands for true time zero.  Each constraint becomes a directed
edge ``from -> to`` with an integer ``bound`` meaning

    offset[to] - offset[from] <= bound

The feasible set is an integer difference-constraint system.  Tight query
bounds are shortest-path distances in that graph; they are never formed by
combining per-event bounds independently.  A negative cycle is a closed
certificate of infeasibility, and every edge keeps the JSON constraint id of
the source inequality it encodes.
"""
import json

from .common import TIME_MAX

REFERENCE = '@reference'
INF = 10 ** 30


def _edge(source, target, bound, constraint_id):
    return {'from': source, 'to': target, 'bound': bound,
            'constraint_id': constraint_id}


def _time_bound_edges(event):
    """True time ``tick + offset`` must lie in [0, 10**12]."""
    clock, tick = event['clock'], event['tick']
    return [_edge(clock, REFERENCE, tick, ['event', event['event_id'], 'min']),
            _edge(REFERENCE, clock, TIME_MAX - tick,
                  ['event', event['event_id'], 'max'])]


def _clock_edges(clock, low, high):
    return [_edge(clock, REFERENCE, -low, ['clock', clock, 'min']),
            _edge(REFERENCE, clock, high, ['clock', clock, 'max'])]


def _calibration_edges(cal, events):
    source = events[cal['from_event']]
    target = events[cal['to_event']]
    delta = target['tick'] - source['tick']
    return [
        _edge(target['clock'], source['clock'],
              delta - cal['min_delta'], ['cal', cal['id'], 'min']),
        _edge(source['clock'], target['clock'],
              cal['max_delta'] - delta, ['cal', cal['id'], 'max']),
    ]


def _lifecycle_edges(job_id, kinds):
    arrival, start, finish = kinds['arrival'], kinds['start'], kinds['finish']
    return [
        _edge(start['clock'], arrival['clock'],
              start['tick'] - arrival['tick'],
              ['job', job_id, 'arrival_start']),
        _edge(finish['clock'], start['clock'],
              finish['tick'] - start['tick'],
              ['job', job_id, 'start_finish']),
    ]


def _occupancy_edges(jobs):
    """Per server, adjacent jobs by increasing service_seq: finish <= start."""
    per_server = {}
    for job_id, kinds in jobs.items():
        per_server.setdefault(kinds['start']['server'], []).append(job_id)
    edges = []
    for server in sorted(per_server):
        ordered = sorted(per_server[server],
                         key=lambda job_id: jobs[job_id]['start']['service_seq'])
        for previous, following in zip(ordered, ordered[1:]):
            prev_finish = jobs[previous]['finish']
            next_start = jobs[following]['start']
            edges.append(_edge(
                next_start['clock'], prev_finish['clock'],
                next_start['tick'] - prev_finish['tick'],
                ['server', server, previous, following]))
    return edges


def build_edges(log):
    edges = []
    for clock in sorted(log['clocks']):
        low, high = log['clocks'][clock]
        edges.extend(_clock_edges(clock, low, high))
    for event_id in sorted(log['events']):
        edges.extend(_time_bound_edges(log['events'][event_id]))
    for cal in sorted(log['calibrations'], key=lambda item: item['id']):
        edges.extend(_calibration_edges(cal, log['events']))
    for job_id in sorted(log['jobs']):
        edges.extend(_lifecycle_edges(job_id, log['jobs'][job_id]))
    edges.extend(_occupancy_edges(log['jobs']))
    edges.sort(key=lambda edge: (edge['from'], edge['to'], edge['bound'],
                                 json.dumps(edge['constraint_id'],
                                            sort_keys=True)))
    return edges


def nodes(log):
    return sorted(set(log['clocks']) | {REFERENCE})


def node_index(node_list):
    return {name: position for position, name in enumerate(node_list)}


def all_pairs(edges, node_list):
    """Floyd-Warshall over the clock graph (at most 65 nodes)."""
    index = node_index(node_list)
    size = len(node_list)
    matrix = [[INF] * size for _ in range(size)]
    for node in range(size):
        matrix[node][node] = 0
    for edge in edges:
        row = index[edge['from']]
        column = index[edge['to']]
        if edge['bound'] < matrix[row][column]:
            matrix[row][column] = edge['bound']
    for middle in range(size):
        via = matrix[middle]
        for row in range(size):
            left = matrix[row][middle]
            if left == INF:
                continue
            target = matrix[row]
            for column in range(size):
                if via[column] == INF:
                    continue
                candidate = left + via[column]
                if candidate < target[column]:
                    target[column] = candidate
    return matrix, index


def negative_cycle(edges, node_list):
    """Deterministically extract one closed negative cycle, or ``None``.

    Edges are already in a total deterministic order, so the same canonical
    event/calibration set always yields the same certificate.
    """
    index = node_index(node_list)
    size = len(node_list)
    distance = [0] * size
    predecessor = [None] * size
    witness = None
    for _pass in range(size):
        witness = None
        for edge in edges:
            source = index[edge['from']]
            target = index[edge['to']]
            candidate = distance[source] + edge['bound']
            if candidate < distance[target]:
                distance[target] = candidate
                predecessor[target] = (source, edge)
                witness = target
        if witness is None:
            return None
    node = witness
    for _ in range(size):
        if predecessor[node] is None:
            return None
        node = predecessor[node][0]
    # ``node`` now lies on a negative cycle; walk predecessors until repeat.
    # Following predecessor pointers makes every closing loop sum negative.
    loop = [node]
    current = predecessor[node][0]
    while current != node:
        if len(loop) > size:
            return None
        loop.append(current)
        current = predecessor[current][0]
    # predecessor[loop[i]] is loop[i+1]; edges therefore chain
    # loop[i+1] -> loop[i] for every step, closing on loop[0] -> loop[-1].
    cycle = []
    for position, target_node in enumerate(loop):
        source_node, edge = predecessor[target_node]
        if source_node != loop[(position + 1) % len(loop)]:
            return None
        cycle.append(edge)
    if sum(edge['bound'] for edge in cycle) >= 0:
        return None
    # Orient the cycle so each edge's ``to`` is the next edge's
    # ``from``; the predecessor walk may produce either orientation.
    if any(cycle[i]['to'] != cycle[(i + 1) % len(cycle)]['from']
           for i in range(len(cycle))):
        cycle.reverse()
        if any(cycle[i]['to'] != cycle[(i + 1) % len(cycle)]['from']
               for i in range(len(cycle))):
            return None
    # Rotate to the smallest (from, to, id) edge so output is stable.
    key = lambda edge: (edge['from'], edge['to'],
                        json.dumps(edge['constraint_id'], sort_keys=True))
    pivot = min(range(len(cycle)), key=lambda position: key(cycle[position]))
    return cycle[pivot:] + cycle[:pivot]


def solve(log):
    """Return ``(matrix, index, node_list, conflict_cycle)``.

    ``matrix`` is ``None`` exactly when ``conflict_cycle`` is a negative cycle.
    """
    edge_list = build_edges(log)
    node_list = nodes(log)
    matrix, index = all_pairs(edge_list, node_list)
    if any(matrix[node][node] < 0 for node in range(len(node_list))):
        cycle = negative_cycle(edge_list, node_list)
        if cycle is None:
            raise RuntimeError('infeasible system without extracted cycle')
        return None, index, node_list, cycle
    return matrix, index, node_list, None
