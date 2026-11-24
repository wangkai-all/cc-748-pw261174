"""Crashing optimization on common random numbers.

Crashing only moves activity durations toward their data-supported
``min_duration``; no allocation beyond that interval is accepted. The
expected project duration of every candidate plan is measured on the *same*
stored scenarios, so a frontier point reflects a real plan difference rather
than fresh sampling luck.
"""
from __future__ import annotations

from .models import Network
from .risk import apply_crashing, crash_cost
from .schedule import cpm
from .simulation import percentile


def validate_crash_request(network: Network, crash_days: dict[str, float]) -> list[dict]:
    """Reject allocations outside each activity's supported crash interval."""
    issues: list[dict] = []
    for aid, days in crash_days.items():
        if aid not in network.activities:
            issues.append({"code": "unknown_activity",
                           "message": f"crash request references unknown activity {aid!r}",
                           "activity_id": aid})
            continue
        ac = network.activities[aid]
        cap = ac.base_duration - ac.min_duration
        if days < -1e-9:
            issues.append({"code": "negative_crash",
                           "message": f"crash days for {aid!r} must be non-negative",
                           "activity_id": aid})
        if days > cap + 1e-9:
            issues.append({"code": "crash_beyond_limit",
                           "message": (
                               f"activity {aid!r} can be crashed at most {cap:g} days "
                               f"(down to min_duration {ac.min_duration:g}); {days:g} requested"
                           ),
                           "activity_id": aid, "max_crash_days": cap,
                           "requested_days": days})
    return issues


def _expected_duration(network: Network, scenarios: list[dict],
                       crash_days: dict[str, float], order: list[str]) -> tuple[float, float]:
    total = 0.0
    sq = 0.0
    for sc in scenarios:
        durations = apply_crashing(network, sc["base_durations"], crash_days)
        pdur = cpm(network, durations, order)["project_duration"]
        total += pdur
        sq += pdur * pdur
    n = len(scenarios)
    mean = total / n
    var = max(sq / n - mean * mean, 0.0)
    return mean, var ** 0.5


def _candidate_activities(network: Network, crash_days: dict[str, float]) -> list[str]:
    out = []
    for aid, ac in network.activities.items():
        used = crash_days.get(aid, 0.0)
        if used + 1e-9 < ac.base_duration - ac.min_duration:
            out.append(aid)
    return out


def cost_duration_frontier(network: Network, scenarios: list[dict],
                           budget: float, step_days: float = 1.0,
                           deadline: float | None = None,
                           progress_callback=None) -> dict:
    """Greedy cost-vs-expected-duration frontier up to ``budget``.

    At each step every still-eligible activity is crashed one ``step_days``
    increment in turn and evaluated on the identical scenarios; the cheapest
    effective reduction (cost per expected day saved, ties broken on
    absolute reduction) is committed. Dominated points are dropped.
    """
    order = network.topological_order()
    crash_days: dict[str, float] = {aid: 0.0 for aid in network.activities}
    budget = max(0.0, float(budget))

    mean0, sd0 = _expected_duration(network, scenarios, crash_days, order)
    frontier = [{
        "crash_days": dict(crash_days),
        "cost": 0.0,
        "expected_duration": mean0,
        "duration_sd": sd0,
        "days_saved": 0.0,
    }]
    steps = 0
    max_steps = int(sum(ac.base_duration - ac.min_duration
                        for ac in network.activities.values()) / step_days) + 2

    while True:
        steps += 1
        if progress_callback:
            progress_callback(min(0.95, steps / max(1, max_steps)))
        eligible = _candidate_activities(network, crash_days)
        if not eligible:
            break

        best = None  # (cost_per_day_saved, -reduction, aid, mean, sd, extra_cost)
        base_mean = frontier[-1]["expected_duration"]
        for aid in eligible:
            ac = network.activities[aid]
            room = ac.base_duration - ac.min_duration - crash_days[aid]
            increment = min(step_days, room)
            extra_cost = increment * ac.crash_cost_per_day
            if crash_cost(network, crash_days) + extra_cost > budget + 1e-9:
                continue
            trial = dict(crash_days)
            trial[aid] = crash_days[aid] + increment
            mean, sd = _expected_duration(network, scenarios, trial, order)
            reduction = base_mean - mean
            if reduction <= 1e-9:
                # No expected benefit on the current scenarios: skip this
                # activity rather than spending budget for zero improvement.
                continue
            score = (extra_cost / reduction, -reduction)
            if best is None or score < best[0]:
                best = (score, aid, mean, sd, extra_cost, increment)

        if best is None:
            break  # nothing affordable or nothing effective remains

        _, aid, mean, sd, extra_cost, increment = best
        crash_days[aid] += increment
        cost = crash_cost(network, crash_days)
        point = {
            "crash_days": dict(crash_days),
            "cost": round(cost, 6),
            "expected_duration": mean,
            "duration_sd": sd,
            "days_saved": round(mean0 - mean, 6),
        }
        # Drop the previous point if the new one dominates it (lower or
        # equal expected duration — cost is necessarily higher here).
        if frontier and mean <= frontier[-1]["expected_duration"] + 1e-9:
            frontier.append(point)
        else:
            frontier[-1] = point

        if cost >= budget - 1e-9:
            break

    # Full distribution metrics for each frontier point.
    points = []
    for idx, pt in enumerate(frontier):
        project = []
        for sc in scenarios:
            durations = apply_crashing(network, sc["base_durations"], pt["crash_days"])
            project.append(cpm(network, durations, order)["project_duration"])
        project_sorted = sorted(project)
        on_time = None
        if deadline is not None:
            on_time = sum(1 for v in project if v <= deadline + 1e-9) / len(project)
        points.append({
            "step": idx,
            "cost": round(pt["cost"], 4),
            "expected_duration": round(pt["expected_duration"], 4),
            "duration_sd": round(pt["duration_sd"], 4),
            "days_saved": round(pt["days_saved"], 4),
            "p5": round(percentile(project_sorted, 5), 4),
            "p50": round(percentile(project_sorted, 50), 4),
            "p95": round(percentile(project_sorted, 95), 4),
            "on_time_probability": round(on_time, 6) if on_time is not None else None,
            "crash_days": {a: round(d, 6) for a, d in pt["crash_days"].items() if d > 0},
        })
    return {"budget": budget, "step_days": step_days,
            "deadline": deadline, "points": points}


def plan_under_budget(network: Network, crash_days: dict[str, float],
                      budget: float) -> dict:
    """Check a requested allocation against the budget cap."""
    cost = crash_cost(network, crash_days)
    return {
        "cost": round(cost, 6),
        "budget": float(budget),
        "within_budget": cost <= float(budget) + 1e-9,
    }
