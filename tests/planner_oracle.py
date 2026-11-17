"""Independent brute-force oracle for the exact planner.

This is deliberately written in a different style than
queue_planner.planner: it enumerates every per-job choice (reject, or accept
on an eligible server at an event-derived start) via itertools.product, with
no dynamic programming and no Pareto pruning.  Candidate starts come from a
bounded event closure, so no time tick is enumerated.  It is only suitable
for the small fixtures used in the planner tests.
"""
from itertools import product


def _event_closure(sid, job, all_jobs, pos_boundary, running):
    events = {pos_boundary[sid]}
    events.update(j['arrival'] for j in all_jobs if sid in j['eligible'])
    actual_start, service = running.get(sid, (None, 0))
    if actual_start is not None:
        events.add(actual_start)
        events.add(actual_start + service)
    frontier = set(events)
    depth = 0
    while frontier and depth < len(all_jobs) + 2:
        new_frontier = set()
        for event in frontier:
            for other in all_jobs:
                if sid not in other['eligible'] or other['service'] == 0:
                    continue
                tick = max(event, other['arrival']) + other['service']
                if tick <= job['deadline'] and tick not in events:
                    events.add(tick)
                    new_frontier.add(tick)
        frontier = new_frontier
        depth += 1
    return sorted({max(event, job['arrival']) for event in events
                   if max(event, job['arrival']) <= job['deadline']})


def brute_force(now, servers, pending, running):
    pos_boundary = [now] * servers
    for sid, (actual_start, service) in running.items():
        pos_boundary[sid] = max(now + 1, actual_start + service)
    running_intervals = {
        sid: [(actual_start, actual_start + service)]
        for sid, (actual_start, service) in running.items()}

    choices = []
    for job in pending:
        options = [None]
        for sid in job['eligible']:
            for start in _event_closure(sid, job, pending, pos_boundary,
                                        running):
                if start < max(now, job['arrival']):
                    continue
                if job['service'] > 0 and start < pos_boundary[sid]:
                    continue
                options.append((sid, start))
        choices.append(options)

    best = None
    by_id = {j['id']: j for j in pending}
    for combo in product(*choices):
        used = {}
        feasible = True
        tuples = []
        value = 0
        wait = 0
        for job, choice in zip(pending, combo):
            if choice is None:
                continue
            sid, start = choice
            finish = start + job['service']
            if finish > job['deadline']:
                feasible = False
                break
            if job['service'] == 0:
                used.setdefault(sid, []).append((start, start, True))
            else:
                for left, right, is_zero in used.get(sid, ()):
                    if not is_zero and start < right and left < finish:
                        feasible = False
                        break
                if not feasible:
                    break
                used.setdefault(sid, []).append((start, finish, False))
            tuples.append((job['id'], sid, start))
            value += job['value']
            wait += start - job['arrival']
        if not feasible:
            continue
        for sid, items in used.items():
            positive = [(l, r) for l, r, z in items if not z]
            positive += running_intervals.get(sid, ())
            for point, _r, is_zero in items:
                if is_zero and any(l < point < r for l, r in positive):
                    feasible = False
                    break
            if not feasible:
                break
        if not feasible:
            continue
        tuples.sort()
        key = (-value, wait, tuple(tuples))
        if best is None or key < best[0]:
            best = (key, tuples, value, wait)
    if best is None:
        return {'suggestions': [], 'value_total': 0, 'wait_total': 0,
                'unselected': sorted(j['id'] for j in pending),
                'tuples': ()}
    _key, tuples, value, wait = best
    suggestions = [{'id': jid, 'server': sid, 'start': start,
                    'finish': start + by_id[jid]['service']}
                   for jid, sid, start in tuples]
    accepted = {t[0] for t in tuples}
    return {'suggestions': suggestions, 'value_total': value,
            'wait_total': wait,
            'unselected': sorted(j['id'] for j in pending
                                 if j['id'] not in accepted),
            'tuples': tuple(tuples)}
