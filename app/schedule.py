"""Deterministic CPM scheduling and resource capacity checks."""
from __future__ import annotations

from .models import Network, ValidationError


def cpm(network: Network, durations: dict[str, float], order: list[str] | None = None) -> dict:
    """Critical path method for one duration realization.

    Returns early/late starts and finishes, total floats, the set of
    critical activities and the project duration. Activities start at the
    early-start schedule (no resource leveling).
    """
    if order is None:
        order = network.topological_order()
    succ = network.successors()

    es: dict[str, float] = {}
    ef: dict[str, float] = {}
    for aid in order:
        ac = network.activities[aid]
        start = max((ef[p] for p in ac.predecessors), default=0.0)
        es[aid] = start
        ef[aid] = start + durations[aid]

    project_duration = max(ef.values(), default=0.0)

    # Backward pass from the project horizon.
    lf: dict[str, float] = {}
    ls: dict[str, float] = {}
    for aid in reversed(order):
        ac = network.activities[aid]
        if not succ[aid]:
            lf[aid] = project_duration
        else:
            lf[aid] = min(ls[c] for c in succ[aid])
        ls[aid] = lf[aid] - durations[aid]

    # Durations are stored rounded to 1e-6, so float comparisons need a
    # tolerance comfortably above that resolution.
    total_float = {aid: ls[aid] - es[aid] for aid in order}
    critical = {aid for aid, tf in total_float.items() if abs(tf) < 1e-5}

    # Reconstruct one critical path by walking critical predecessors.
    path: list[str] = []
    ends = [aid for aid in order if not succ[aid]]
    if ends:
        cur = max(ends, key=lambda a: ef[a])
        chain = [cur]
        while True:
            ac = network.activities[cur]
            crit_preds = [p for p in ac.predecessors if p in critical]
            if not crit_preds:
                break
            cur = max(crit_preds, key=lambda p: ef[p])
            chain.append(cur)
        path = list(reversed(chain))

    return {
        "project_duration": project_duration,
        "es": es,
        "ef": ef,
        "ls": ls,
        "lf": lf,
        "total_float": total_float,
        "critical": critical,
        "critical_path": path,
    }


def resource_profile(network: Network, es: dict[str, float],
                     durations: dict[str, float]) -> dict[str, list[dict]]:
    """Time-phased resource loading under the early-start schedule.

    Emits, per resource, the intervals during which each occupying
    activity runs and the overall peak demand.
    """
    profiles: dict[str, list[dict]] = {}
    for rid, res in network.resources.items():
        events: list[tuple[float, float, str]] = []
        for ac in network.activities.values():
            need = ac.resources.get(rid, 0.0)
            if need > 0:
                events.append((es[ac.id], es[ac.id] + durations[ac.id], ac.id))
        if not events:
            profiles[rid] = {"capacity": res.capacity, "peak": 0.0,
                             "violations": [], "intervals": []}
            continue
        events.sort()
        # Sweep adjacent breakpoints to compute constant-demand segments.
        points = sorted({t for iv in events for t in iv[:2]})
        violations: list[dict] = []
        segments: list[dict] = []
        peak = 0.0
        for t0, t1 in zip(points, points[1:]):
            if t1 - t0 <= 1e-9:
                continue
            active = [aid for (s, e, aid) in events if s < t1 - 1e-9 and e > t0 + 1e-9]
            demand = sum(network.activities[aid].resources[rid] for aid in active)
            peak = max(peak, demand)
            seg = {"start": round(t0, 6), "end": round(t1, 6),
                   "demand": demand, "activities": sorted(active)}
            segments.append(seg)
            if demand > res.capacity + 1e-9:
                violations.append({
                    "code": "resource_capacity_conflict",
                    "resource_id": rid,
                    "start": round(t0, 6),
                    "end": round(t1, 6),
                    "demand": demand,
                    "capacity": res.capacity,
                    "activities": sorted(active),
                })
        profiles[rid] = {"capacity": res.capacity, "peak": peak,
                         "violations": violations, "intervals": segments}
    return profiles


def check_resource_capacity(network: Network,
                            durations: dict[str, float] | None = None) -> list[dict]:
    """Capacity check on the deterministic (nominal or given) schedule."""
    order = network.topological_order()
    if durations is None:
        durations = {aid: ac.base_duration for aid, ac in network.activities.items()}
    plan = cpm(network, durations, order)
    profiles = resource_profile(network, plan["es"], durations)
    violations: list[dict] = []
    for rid, prof in profiles.items():
        violations.extend(prof["violations"])
    return violations


def deterministic_horizon(network: Network) -> dict:
    """Nominal CPM: shortest achievable project duration (all durations nominal)."""
    order = network.topological_order()
    durations = {aid: ac.base_duration for aid, ac in network.activities.items()}
    plan = cpm(network, durations, order)
    return {
        "order": order,
        "durations": durations,
        "plan": plan,
        "minimum_achievable": cpm(
            network,
            {aid: ac.min_duration for aid, ac in network.activities.items()},
            order,
        )["project_duration"],
    }


def check_deadline(network: Network, deadline: float) -> dict:
    """A deadline below the fully-crashed deterministic horizon is unreachable."""
    info = deterministic_horizon(network)
    min_horizon = info["minimum_achievable"]
    nominal = info["plan"]["project_duration"]
    reachable = deadline + 1e-9 >= min_horizon
    result = {
        "deadline": deadline,
        "nominal_duration": nominal,
        "minimum_achievable_duration": min_horizon,
        "reachable": reachable,
    }
    if not reachable:
        result["issue"] = {
            "code": "unreachable_deadline",
            "message": (
                f"deadline {deadline:g} days is shorter than the fully crashed "
                f"deterministic horizon of {min_horizon:g} days"
            ),
            "deadline": deadline,
            "minimum_achievable_duration": min_horizon,
        }
    return result


def validate_runnable(network: Network, deadline: float | None = None) -> list[dict]:
    """Full structural validation: cycle, resources, deadline. Returns issues."""
    issues: list[dict] = []
    try:
        network.topological_order()
    except ValidationError as exc:
        return exc.issues

    for v in check_resource_capacity(network):
        issues.append({
            "code": "resource_capacity_conflict",
            "message": (
                f"resource {v['resource_id']!r} demand {v['demand']:g} exceeds "
                f"capacity {v['capacity']:g} between day {v['start']:g} and {v['end']:g}"
            ),
            **v,
        })

    if deadline is not None:
        dl = check_deadline(network, float(deadline))
        if not dl["reachable"]:
            issues.append(dl["issue"])
    return issues
