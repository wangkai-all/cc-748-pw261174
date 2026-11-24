"""Monte Carlo simulation over shared-risk scenarios (CRN).

Scenarios are generated once for a (network, seed, n_scenarios) triple and
reused by every crashing plan, so the difference between two plans is
observed on the *same* random realizations — common random numbers. A plan
never re-draws anything; it only maps the stored base durations through its
crash limits and re-runs the deterministic CPM.
"""
from __future__ import annotations

import math

from .models import Network
from .risk import FactorSampler, apply_crashing, crash_cost
from .rng import make_rng
from .schedule import cpm

HIST_BINS = 30


def generate_scenarios(network: Network, n_scenarios: int, seed: int) -> list[dict]:
    """Draw n scenarios of shared factor values and unhastened durations."""
    rng = make_rng(seed)
    sampler = FactorSampler(network, rng)
    order = network.topological_order()
    scenarios: list[dict] = []
    for _ in range(n_scenarios):
        latents = sampler.draw_latents()
        factor_values = sampler.realize_factors(latents)
        idio = sampler.draw_idiosyncratic()
        base_durations = {
            aid: _round(
                max(
                    network.activities[aid].base_duration
                    + idio[aid]
                    + sum(load * factor_values[fid]
                          for fid, load in network.activities[aid].risk_loadings.items()),
                    1e-6,
                )
            )
            for aid in order
        }
        scenarios.append({"factor_values": factor_values, "base_durations": base_durations})
    return scenarios


def evaluate_plan(network: Network, scenarios: list[dict],
                  crash_days: dict[str, float], order: list[str],
                  deadline: float | None) -> dict:
    """Run CPM on every stored scenario under one crashing plan."""
    n = len(scenarios)
    aids = order
    project = [0.0] * n
    on_time: list[bool] = []
    crit_count = {aid: 0 for aid in aids}
    dur_sum = {aid: 0.0 for aid in aids}
    dur_sq = {aid: 0.0 for aid in aids}
    es_sum = {aid: 0.0 for aid in aids}
    # Running sums for cruciality = corr(activity duration, project duration).
    p_sum = 0.0
    p_sq = 0.0
    cross = {aid: 0.0 for aid in aids}
    paths: list[list[str]] = []
    per_scenario_durations = {aid: [0.0] * n for aid in aids}

    for i, sc in enumerate(scenarios):
        durations = apply_crashing(network, sc["base_durations"], crash_days)
        plan = cpm(network, durations, order)
        pdur = plan["project_duration"]
        project[i] = pdur
        if deadline is not None:
            on_time.append(pdur <= deadline + 1e-9)
        paths.append(plan["critical_path"])
        for aid in aids:
            d = durations[aid]
            per_scenario_durations[aid][i] = d
            dur_sum[aid] += d
            dur_sq[aid] += d * d
            es_sum[aid] += plan["es"][aid]
            cross[aid] += d * pdur
            if aid in plan["critical"]:
                crit_count[aid] += 1
        p_sum += pdur
        p_sq += pdur * pdur

    mean_p = p_sum / n
    var_p = max(p_sq / n - mean_p * mean_p, 0.0)
    sd_p = math.sqrt(var_p)

    activities_stats: dict[str, dict] = {}
    for aid in aids:
        mean_d = dur_sum[aid] / n
        var_d = max(dur_sq[aid] / n - mean_d * mean_d, 0.0)
        cov = cross[aid] / n - mean_d * mean_p
        denom = math.sqrt(var_d) * sd_p
        cruciality = cov / denom if denom > 1e-12 else 0.0
        activities_stats[aid] = {
            "activity_id": aid,
            "criticality_index": crit_count[aid] / n,
            "cruciality_index": _round(cruciality),
            "mean_duration": _round(mean_d),
            "duration_sd": _round(math.sqrt(var_d)),
            "mean_start": _round(es_sum[aid] / n),
            "base_duration": network.activities[aid].base_duration,
            "min_duration": network.activities[aid].min_duration,
            "max_crash_days": _round(
                network.activities[aid].base_duration - network.activities[aid].min_duration
            ),
            "crash_days_applied": _round(
                max(0.0, min(float(crash_days.get(aid, 0.0)),
                             network.activities[aid].base_duration
                             - network.activities[aid].min_duration))
            ),
            "crash_cost_per_day": network.activities[aid].crash_cost_per_day,
        }

    sorted_p = sorted(project)
    total_cost = crash_cost(network, crash_days)
    summary = {
        "n_scenarios": n,
        "mean_project_duration": _round(mean_p),
        "sd_project_duration": _round(sd_p),
        "p5": _round(percentile(sorted_p, 5)),
        "p50": _round(percentile(sorted_p, 50)),
        "p95": _round(percentile(sorted_p, 95)),
        "min_project_duration": _round(sorted_p[0]),
        "max_project_duration": _round(sorted_p[-1]),
        "on_time_probability": (sum(on_time) / n) if deadline is not None else None,
        "deadline": deadline,
        "total_crash_cost": _round(total_cost),
        "total_crash_days": _round(
            sum(activities_stats[a]["crash_days_applied"] for a in aids)
        ),
    }

    return {
        "summary": summary,
        "activities": activities_stats,
        "project_durations": [_round(v) for v in project],
        "critical_paths": paths,
        "per_scenario_durations": per_scenario_durations,
        "histogram": histogram(project, sorted_p),
    }


def factor_delay_attribution(network: Network, scenarios: list[dict],
                             project_durations: list[float]) -> dict:
    """Where the project-level delay comes from.

    * Global view: correlation of each shared factor with project duration,
      and its mean realized value.
    * Per-activity view: mean duration decomposed into base, each factor's
      mean loading effect, and the residual.
    """
    n = len(scenarios)
    mean_p = sum(project_durations) / n
    var_p = sum((v - mean_p) ** 2 for v in project_durations) / n

    factor_stats = {}
    for fid in network.factor_order:
        vals = [sc["factor_values"][fid] for sc in scenarios]
        mean_f = sum(vals) / n
        var_f = sum((v - mean_f) ** 2 for v in vals) / n
        cov = sum((vals[i] - mean_f) * (project_durations[i] - mean_p)
                  for i in range(n)) / n
        denom = math.sqrt(var_f * var_p)
        factor_stats[fid] = {
            "factor_id": fid,
            "name": network.factors[fid].name,
            "kind": network.factors[fid].kind,
            "mean_value": _round(mean_f),
            "correlation_with_project_duration": _round(cov / denom) if denom > 1e-12 else 0.0,
        }

    per_activity: dict[str, dict] = {}
    for aid, ac in network.activities.items():
        contributions: dict[str, float] = {}
        for fid, loading in ac.risk_loadings.items():
            mean_value = factor_stats[fid]["mean_value"]
            contributions[fid] = _round(loading * mean_value)
        mean_idio = sum(
            sc["base_durations"][aid]
            - ac.base_duration
            - sum(load * sc["factor_values"][fid] for fid, load in ac.risk_loadings.items())
            for sc in scenarios
        ) / n
        per_activity[aid] = {
            "base": ac.base_duration,
            "factor_contributions": contributions,
            "idiosyncratic_mean": _round(mean_idio),
            "mean_unhastened": _round(
                ac.base_duration
                + sum(contributions.values())
                + mean_idio
            ),
        }
    return {"factors": factor_stats, "activities": per_activity}


def percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (q / 100.0) * (len(sorted_values) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def histogram(values: list[float], sorted_values: list[float] | None = None,
              bins: int = HIST_BINS) -> dict:
    if sorted_values is None:
        sorted_values = sorted(values)
    lo, hi = sorted_values[0], sorted_values[-1]
    if hi - lo <= 1e-12:
        return {"bin_edges": [_round(lo - 0.5), _round(hi + 0.5)],
                "counts": [len(values)]}
    width = (hi - lo) / bins
    edges = [lo + i * width for i in range(bins + 1)]
    counts = [0] * bins
    for v in values:
        idx = min(int((v - lo) / width), bins - 1)
        counts[idx] += 1
    return {"bin_edges": [_round(e) for e in edges], "counts": counts}


def run_simulation(network: Network, scenarios: list[dict],
                   crash_days: dict[str, float], deadline: float | None,
                   progress_callback=None) -> dict:
    """Full simulation bundle including delay attribution for the UI/export."""
    order = network.topological_order()
    result = evaluate_plan(network, scenarios, crash_days, order, deadline)
    if progress_callback:
        progress_callback(0.7)
    attribution = factor_delay_attribution(
        network, scenarios, result["project_durations"]
    )
    result["delay_attribution"] = attribution
    result["scenarios"] = scenarios
    if progress_callback:
        progress_callback(1.0)
    return result


def _round(v: float, ndigits: int = 6) -> float:
    return round(float(v), ndigits)
