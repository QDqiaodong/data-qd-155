"""Exact three-level on-time acceptance planner.

The planner never reuses the FIFO earliest-idle-server rule from
queue_replay.  It solves the *joint* decision (acceptance subset, server and
integer start time) exactly for at most 12 pending jobs and 1..3 servers.

Strategy and completeness
-------------------------
Pending jobs are split into positive-service and zero-service jobs.

1. Per server, a subset DP enumerates every feasible schedule of any subset
   of its eligible positive jobs.  Positive jobs are left-packed
   (``start = max(finish, arrival)``); additional start candidates at
   zero-job arrival ticks are added only when an eligible zero job exists,
   so a positive job can share its start edge with a zero job.
   Left-packing loses no optimum: waiting only decreases with earlier
   starts and every feasible start is a release/arrival/previous-finish
   event, so no time tick is ever enumerated.  Shifting every time by a
   constant (including near 10**12) preserves the relative optimum.
2. Per-server options are combined across servers with a global mask DP
   over disjoint positive subsets.  The number of server assignments of n
   jobs across s servers is s**n (at most 3**12 = 531441), which bounds the
   search; the acceptance subset is chosen jointly with its schedule,
   never greedily.
3. Zero-service jobs occupy no positive-length interval.  They are
   evaluated once per surviving global state, each taking the
   lexicographically smallest feasible ``(start, server)`` pair under the
   strict-interior rule.  States are then compared with the exact contract
   objective: maximize total accepted value, then minimize total waiting,
   then minimize the id-sorted ``(id, server, start)`` tuple list, a short
   prefix sorting first.

Pruning safety
--------------
For one accepted mask the value (first objective) is fixed.  Per-server
states with identical (finish, wait) describe the same positive interval
set and merge to their smallest tuple list; when zero jobs exist every
distinct geometry survives because each exposes different zero-placement
edges.  Globally, only the minimum positive waiting per mask is kept:
waiting is the second objective and is compared before the tuple list, so a
strictly larger wait can never win.  States with equal waiting keep all
their finish vectors and tuple lists, because server assignment is itself
part of the third-level tuple and finish dominance must not prune it.  With
zero jobs the prune key additionally includes the zero-pair vector.
"""
from heapq import merge


def zero_tick(boundary, intervals, job):
    """Earliest feasible integer start for a zero-service job on a server.

    A start on either edge of a positive interval is legal; landing
    strictly inside one pushes the start to that interval's finish tick.
    """
    tick = max(boundary, job['arrival'])
    for left, right in intervals:
        if tick <= left:
            break
        if tick < right:  # equivalent to left < tick < right here
            tick = right
    return tick if tick <= job['deadline'] else None


def _assemble_tuples(split, servers):
    """Merge per-server (id, start) lists into id-sorted 3-tuples."""
    streams = []
    for sid in range(servers):
        def gen(server=sid):
            return ((jid, server, start) for jid, start in split[server])
        streams.append(gen())
    return tuple(merge(*streams)) if streams else ()


def _server_options(server, release, pos, zeros):
    """Subset DP for one server: mask -> list of (finish, wait, tuples).

    Every distinct (finish, wait, tuple) state is retained: different
    orderings of one mask accumulate different waiting and expose different
    interval edges that zero jobs can use, so no local dominance pruning is
    safe; only byte-identical states are removed.
    """
    n = len(pos)
    eligible = 0
    for i, job in enumerate(pos):
        if server in job['eligible']:
            eligible |= 1 << i
    pos_by_id = {j['id']: j for j in pos}
    zero_arrivals = sorted({z['arrival'] for z in zeros
                            if server in z['eligible']})
    # Without an eligible zero job, left-packing alone is optimal: delaying
    # a positive start to another job's arrival only adds waiting and cannot
    # open any zero-placement edge, so the subset DP (which already enumerates
    # every accepted subset independently) needs no extra start events.  With
    # zero jobs, zero arrivals become additional start candidates so a
    # positive job can share its start edge with a zero job.
    states = {0: [(release, 0, ())]}
    for mask in range(1, 1 << n):
        if mask & ~eligible:
            continue
        candidates = []
        bits = mask
        while bits:
            bit = bits & -bits
            i = bit.bit_length() - 1
            job = pos[i]
            for finish, wait, tuples in states.get(mask ^ bit, ()):
                packed = max(finish, job['arrival'])
                starts = {packed}
                for candidate in zero_arrivals:
                    if candidate >= packed and \
                            candidate + job['service'] \
                            <= job['deadline']:
                        starts.add(candidate)
                for start in sorted(starts):
                    end = start + job['service']
                    if end > job['deadline']:
                        continue
                    new_tuples = tuple(sorted(
                        tuples + ((job['id'], start),)))
                    candidates.append(
                        (end, wait + start - job['arrival'], new_tuples))
            bits ^= bit
        if not candidates:
            continue
        if zero_arrivals:
            # Zero jobs can use interval edges, so every distinct geometry
            # (including tuple variants on equal finish/wait) must survive.
            states[mask] = sorted(set(candidates))
        else:
            # No zero job: identical (finish, wait) is the same positive
            # interval set; keep the smallest tuple list for obj3.
            buckets = {}
            for finish, wait, tuples in candidates:
                key = (finish, wait)
                if key not in buckets or tuples < buckets[key]:
                    buckets[key] = tuples
            states[mask] = sorted(
                (f, w, t) for (f, w), t in buckets.items())
    return states, pos_by_id


def _zero_vector_for(split, servers, zeros, now, running_intervals,
                     pos_by_id):
    """Best feasible (tick, server) pair per zero job for a full state."""
    planned = {}
    for sid in range(servers):
        planned[sid] = sorted(
            (start, start + pos_by_id[jid]['service'])
            for jid, start in split[sid])
    pairs = []
    for job in zeros:
        pair = _best_zero_pair(job, now, running_intervals, planned)
        pairs.append(pair)
    return tuple(pairs)


def _prune_global(states, has_zeros, servers, zeros, now,
                  running_intervals, pos_by_id):
    """Prune global states.

    Without zero jobs, a strict (wait, finish-vector) Pareto front is kept.
    With zero jobs, states are additionally distinguished by their best
    zero-job pair vector: two states with the same wait, finish vector and
    zero-pair vector differ only in tuple list, so the smallest tuple wins;
    distinct zero vectors all survive because they admit different value.
    """
    if has_zeros:
        # Key on the full (finish vector, zero-pair vector): both determine
        # zero placement and the objective.  Finish-vector dominance is not
        # used because server assignment is part of the obj3 tuple list.
        # States sharing a key differ only in positive wait and tuple list,
        # so the smallest (wait, tuple) wins.
        best = {}
        for split, value, wait, finishes in states:
            zero_pairs = _zero_vector_for(
                split, servers, zeros, now, running_intervals, pos_by_id)
            key = (tuple(finishes), zero_pairs)
            tuples = _assemble_tuples(split, servers)
            current = best.get(key)
            if current is None or (wait, tuples) < (current[0], current[2]):
                best[key] = (wait, (split, value), tuples)
        return [(split, value, wait, list(finishes))
                for (finishes, _zp), (wait, (split, value), _t)
                in best.items()]

    # Within one accepted mask the value (obj1) is fixed.  A state whose
    # positive waiting is strictly larger can never win: obj2 compares
    # waiting before the tuple list, and zero placement is evaluated at the
    # leaf independently.  Among states with equal waiting every distinct
    # finish vector and tuple list survives, because server assignment is
    # part of obj3 and finishes shape zero placement.
    min_wait = min(wait for _s, _v, wait, _f in states)
    geometry = {}
    for split, value, wait, finishes in states:
        if wait > min_wait:
            continue
        key = (wait, finishes)
        geometry.setdefault(key, []).append((split, value))
    kept = []
    for (wait, finishes), candidates in geometry.items():
        best_split, best_value = min(
            candidates,
            key=lambda sv: _assemble_tuples(sv[0], servers))
        kept.append((best_split, best_value, wait, finishes))
    return kept


def _best_zero_pair(job, now, running_intervals, planned_intervals):
    best = None
    for sid in job['eligible']:
        intervals = sorted(list(running_intervals.get(sid, ()))
                           + planned_intervals.get(sid, ()))
        tick = zero_tick(now, intervals, job)
        if tick is not None:
            pair = (tick, sid)
            if best is None or pair < best:
                best = pair
    return best


def optimize(now, servers, pending, running):
    """Return the unique optimal plan for the current snapshot.

    pending: list of dicts id, arrival, service, deadline, value, eligible.
    running: mapping server -> (actual_start, service).
    """
    positive_boundary = [now] * servers
    running_intervals = {}
    for sid, (actual_start, service) in running.items():
        positive_boundary[sid] = max(now + 1, actual_start + service)
        running_intervals[sid] = [(actual_start, actual_start + service)]

    pos = sorted((j for j in pending if j['service'] > 0),
                 key=lambda j: j['id'])
    zeros = [j for j in pending if j['service'] == 0]
    per_server = [_server_options(sid, positive_boundary[sid], pos, zeros)
                  for sid in range(servers)]
    pos_by_id = {j['id']: j for j in pos}
    full = (1 << len(pos)) - 1
    has_zeros = bool(zeros)

    # State: (split, value, wait, finish vector).  Tuple lists are assembled
    # only at leaves, so dominated intermediate states never pay for a merge.
    dp = {0: [(((),) * servers, 0, 0, tuple(positive_boundary))]}
    for current in range(servers):
        options, _ = per_server[current]
        weighted = {
            sub: [(fin, lw, lt,
                   sum(pos_by_id[jid]['value']
                       for jid, _start in lt))
                  for fin, lw, lt in opts]
            for sub, opts in options.items()}
        next_dp = {}
        seen_split = {}
        for target_mask in range(full + 1):
            previous = target_mask
            bucket = []
            dedup = seen_split.setdefault(target_mask, set())
            while True:
                sub = target_mask ^ previous
                if sub in weighted and previous in dp:
                    for finish, local_wait, local_tuples, added_value \
                            in weighted[sub]:
                        for split, value, wait, finishes in dp[previous]:
                            new_split = list(split)
                            new_split[current] = local_tuples
                            new_finishes = list(finishes)
                            new_finishes[current] = finish
                            split_key = tuple(new_split)
                            if split_key in dedup:
                                continue
                            dedup.add(split_key)
                            bucket.append((
                                split_key, value + added_value,
                                wait + local_wait, tuple(new_finishes)))
                if previous == 0:
                    break
                previous = (previous - 1) & target_mask
            if bucket:
                next_dp[target_mask] = bucket
        dp = {
            target_mask: _prune_global(
                bucket, has_zeros, servers, zeros, now,
                running_intervals, pos_by_id)
            for target_mask, bucket in next_dp.items()}

    best_key = None
    best = None
    for states in dp.values():
        for split, value, wait, _finishes in states:
            tuples = _assemble_tuples(split, servers)
            planned_intervals = {}
            for sid in range(servers):
                planned_intervals[sid] = sorted(
                    (start, start + pos_by_id[jid]['service'])
                    for jid, server, start in tuples if server == sid)
            all_tuples = list(tuples)
            total_value = value
            total_wait = wait
            for job in zeros:
                pair = _best_zero_pair(job, now, running_intervals,
                                       planned_intervals)
                if pair is None:
                    continue
                start, sid = pair
                total_value += job['value']
                total_wait += start - job['arrival']
                all_tuples.append((job['id'], sid, start))
            all_tuples.sort()
            key = (-total_value, total_wait, tuple(all_tuples))
            if best_key is None or key < best_key:
                best_key = key
                best = (all_tuples, total_value, total_wait)

    all_tuples, value_total, wait_total = best
    accepted = {jid for jid, _s, _t in all_tuples}
    by_id = {j['id']: j for j in pending}
    suggestions = []
    for jid, sid, start in all_tuples:
        job = by_id[jid]
        suggestions.append({'id': jid, 'server': sid, 'start': start,
                            'finish': start + job['service']})
    suggestions.sort(key=lambda item: item['id'])
    unselected = sorted(j['id'] for j in pending if j['id'] not in accepted)
    return {'suggestions': suggestions, 'value_total': value_total,
            'wait_total': wait_total, 'unselected': unselected,
            'tuples': tuple(all_tuples)}
