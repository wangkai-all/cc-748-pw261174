"""Network, activity, risk factor and resource data models plus validation."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .rng import cholesky


class ValidationError(Exception):
    """Raised with structured ``issues`` when a network cannot be analysed."""

    def __init__(self, issues: list[dict]):
        super().__init__("; ".join(i["code"] + ": " + i["message"] for i in issues))
        self.issues = issues


@dataclass
class Activity:
    id: str
    name: str
    base_duration: float          # nominal (unshocked) duration, days
    predecessors: list[str]
    risk_loadings: dict[str, float]   # factor_id -> coefficient (days of effect)
    idio_sd: float                # activity-specific residual std (days)
    min_duration: float           # crash limit (data-supported lower bound)
    crash_cost_per_day: float     # linear crash cost, cost units/day
    resources: dict[str, float]   # resource_id -> units occupied while active


@dataclass
class RiskFactor:
    id: str
    name: str
    kind: str                     # normal | lognormal | bernoulli
    mean: float
    sd: float
    p: float | None = None        # bernoulli probability of the shock
    description: str = ""


@dataclass
class Resource:
    id: str
    name: str
    capacity: float


@dataclass
class Network:
    id: str
    name: str
    description: str
    activities: dict[str, Activity]
    factors: dict[str, RiskFactor]
    resources: dict[str, Resource]
    factor_correlation: list[list[float]] = field(default_factory=list)
    factor_order: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ parse
    @classmethod
    def from_dict(cls, data: dict) -> "Network":
        issues: list[dict] = []

        def err(code: str, message: str, **extra):
            entry = {"code": code, "message": message}
            entry.update(extra)
            issues.append(entry)

        net_id = str(data.get("id", "network"))
        name = str(data.get("name", net_id))
        description = str(data.get("description", ""))

        raw_factors = data.get("risk_factors", []) or []
        factors: dict[str, RiskFactor] = {}
        factor_order: list[str] = []
        for rf in raw_factors:
            fid = str(rf.get("id", ""))
            if not fid:
                err("risk_factor_missing_id", "a risk factor has no id")
                continue
            if fid in factors:
                err("risk_factor_duplicate_id", f"duplicate risk factor id {fid!r}", factor_id=fid)
                continue
            kind = str(rf.get("kind", "normal"))
            if kind not in ("normal", "lognormal", "bernoulli"):
                err(
                    "risk_factor_unknown_kind",
                    f"factor {fid!r} has unsupported kind {kind!r}",
                    factor_id=fid,
                )
                continue
            mean = float(rf.get("mean", 0.0))
            sd = float(rf.get("sd", 1.0))
            if sd < 0:
                err("risk_factor_bad_sd", f"factor {fid!r} has negative sd", factor_id=fid)
            p = rf.get("p")
            if kind == "bernoulli":
                p = float(p if p is not None else 0.2)
                if not 0.0 <= p <= 1.0:
                    err("risk_factor_bad_p", f"factor {fid!r} p must be in [0,1]", factor_id=fid)
            factors[fid] = RiskFactor(
                id=fid,
                name=str(rf.get("name", fid)),
                kind=kind,
                mean=mean,
                sd=sd,
                p=p,
                description=str(rf.get("description", "")),
            )
            factor_order.append(fid)

        raw_resources = data.get("resources", []) or []
        resources: dict[str, Resource] = {}
        for rr in raw_resources:
            rid = str(rr.get("id", ""))
            if not rid:
                err("resource_missing_id", "a resource has no id")
                continue
            if rid in resources:
                err("resource_duplicate_id", f"duplicate resource id {rid!r}", resource_id=rid)
                continue
            cap = float(rr.get("capacity", 0.0))
            if cap < 0:
                err("resource_bad_capacity", f"resource {rid!r} capacity is negative", resource_id=rid)
            resources[rid] = Resource(rid, str(rr.get("name", rid)), cap)

        raw_activities = data.get("activities", []) or []
        activities: dict[str, Activity] = {}
        for ac in raw_activities:
            aid = str(ac.get("id", ""))
            if not aid:
                err("activity_missing_id", "an activity has no id")
                continue
            if aid in activities:
                err("activity_duplicate_id", f"duplicate activity id {aid!r}", activity_id=aid)
                continue
            base = float(ac.get("base_duration", 0.0))
            if base <= 0:
                err("activity_bad_duration",
                    f"activity {aid!r} base_duration must be positive", activity_id=aid)
            idio_sd = float(ac.get("idio_sd", 0.0))
            if idio_sd < 0:
                err("activity_bad_idio_sd",
                    f"activity {aid!r} idio_sd must be non-negative", activity_id=aid)
            min_dur = float(ac.get("min_duration", base))
            if min_dur <= 0:
                err("activity_bad_min_duration",
                    f"activity {aid!r} min_duration must be positive", activity_id=aid)
            if min_dur > base + 1e-9:
                err("activity_min_above_base",
                    f"activity {aid!r} min_duration exceeds base_duration", activity_id=aid)
            cost = float(ac.get("crash_cost_per_day", 0.0))
            if cost < 0:
                err("activity_bad_crash_cost",
                    f"activity {aid!r} crash_cost_per_day is negative", activity_id=aid)
            loadings = {str(k): float(v) for k, v in (ac.get("risk_loadings") or {}).items()}
            for lf in loadings:
                if lf not in factors:
                    err("loading_unknown_factor",
                        f"activity {aid!r} loads unknown risk factor {lf!r}",
                        activity_id=aid, factor_id=lf)
            res_need = {str(k): float(v) for k, v in (ac.get("resources") or {}).items()}
            for rk, rv in res_need.items():
                if rk not in resources:
                    err("activity_unknown_resource",
                        f"activity {aid!r} uses unknown resource {rk!r}",
                        activity_id=aid, resource_id=rk)
                if rv < 0:
                    err("activity_bad_resource_need",
                        f"activity {aid!r} needs negative amount of {rk!r}", activity_id=aid)
            preds = [str(p) for p in ac.get("predecessors", [])]
            activities[aid] = Activity(
                id=aid,
                name=str(ac.get("name", aid)),
                base_duration=base,
                predecessors=preds,
                risk_loadings=loadings,
                idio_sd=idio_sd,
                min_duration=min_dur,
                crash_cost_per_day=cost,
                resources=res_need,
            )

        for ac in activities.values():
            for p in ac.predecessors:
                if p not in activities:
                    err("predecessor_missing",
                        f"activity {ac.id!r} lists missing predecessor {p!r}",
                        activity_id=ac.id, predecessor_id=p)

        corr = [list(map(float, row)) for row in (data.get("factor_correlation") or [])]
        if corr:
            n = len(factor_order)
            if len(corr) != n or any(len(row) != n for row in corr):
                err("correlation_bad_shape",
                    "factor_correlation must be a square matrix over risk_factors")
            else:
                for i in range(n):
                    if not math.isclose(corr[i][i], 1.0, abs_tol=1e-9):
                        err("correlation_bad_diagonal",
                            f"correlation diagonal entry [{i}] is not 1")
                    for j in range(i):
                        if not math.isclose(corr[i][j], corr[j][i], abs_tol=1e-9):
                            err("correlation_asymmetric", "correlation matrix is not symmetric")
                if not any(e["code"] == "correlation_bad_shape" for e in issues):
                    try:
                        cholesky(corr)
                    except ValueError:
                        err("correlation_not_psd",
                            "factor_correlation is not positive semidefinite")

        if issues:
            raise ValidationError(issues)

        return cls(
            id=net_id,
            name=name,
            description=description,
            activities=activities,
            factors=factors,
            resources=resources,
            factor_correlation=corr,
            factor_order=factor_order,
        )

    # ------------------------------------------------------------- structural
    def topological_order(self) -> list[str]:
        """Kahn's algorithm. Raises ValidationError on a dependency cycle."""
        indeg = {aid: 0 for aid in self.activities}
        children: dict[str, list[str]] = {aid: [] for aid in self.activities}
        for ac in self.activities.values():
            for p in ac.predecessors:
                indeg[ac.id] += 1
                children[p].append(ac.id)
        ready = sorted(aid for aid, d in indeg.items() if d == 0)
        order: list[str] = []
        while ready:
            cur = ready.pop(0)
            order.append(cur)
            for nxt in sorted(children[cur]):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    ready.append(nxt)
            ready.sort()
        if len(order) != len(self.activities):
            pending = [aid for aid, d in indeg.items() if d > 0]
            raise ValidationError([{
                "code": "cyclic_dependency",
                "message": "activity predecessors contain a cycle",
                "activity_ids": sorted(pending),
            }])
        return order

    def successors(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {aid: [] for aid in self.activities}
        for ac in self.activities.values():
            for p in ac.predecessors:
                out[p].append(ac.id)
        return out
