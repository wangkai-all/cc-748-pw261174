"""Common-risk-factor scenario generation.

Risk enters every simulation through *shared* latent sources: one draw per
risk factor per scenario, with an optional Gaussian copula correlating the
factor latents. Activity durations are deterministic functions of those
shared draws plus an activity-specific residual — there is deliberately no
way to attach independent noise "to activities" in place of the shared
factors, so independent sampling cannot silently hide schedule-wide delays.
"""
from __future__ import annotations

import math

from .models import Network, RiskFactor
from .rng import cholesky, std_normal_cdf


class FactorSampler:
    """Precomputes the copula factor and draws full scenarios on demand."""

    def __init__(self, network: Network, rng):
        self.network = network
        self.rng = rng
        self.factor_ids = list(network.factor_order)
        n = len(self.factor_ids)
        if network.factor_correlation:
            self.L = cholesky(network.factor_correlation)
        else:
            self.L = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    # -- fixed draw order, per scenario: factor latents, then idio normals --
    def draw_latents(self) -> list[float]:
        n = len(self.factor_ids)
        z = [self.rng.gauss(0.0, 1.0) for _ in range(n)]
        return [sum(self.L[i][k] * z[k] for k in range(n)) for i in range(n)]

    def realize_factors(self, latents: list[float]) -> dict[str, float]:
        """Map each latent normal to the factor's realized value."""
        out: dict[str, float] = {}
        for fid, z in zip(self.factor_ids, latents):
            rf = self.network.factors[fid]
            out[fid] = self._realize_one(rf, z)
        return out

    @staticmethod
    def _realize_one(rf: RiskFactor, z: float) -> float:
        if rf.kind == "normal":
            return rf.mean + rf.sd * z
        if rf.kind == "lognormal":
            # mean/sd describe the raw (positive) shock; solve log-space params.
            var = rf.sd * rf.sd
            m2 = rf.mean * rf.mean
            if rf.mean <= 0:
                raise ValueError(f"lognormal factor {rf.id!r} needs positive mean")
            sigma2 = math.log1p(var / m2)
            mu = math.log(rf.mean) - 0.5 * sigma2
            return math.exp(mu + math.sqrt(sigma2) * z)
        if rf.kind == "bernoulli":
            # Gaussian copula: shock occurs when Phi(z) <= p.
            return 1.0 if std_normal_cdf(z) <= (rf.p if rf.p is not None else 0.2) else 0.0
        raise ValueError(f"unsupported factor kind {rf.kind!r}")

    def draw_idiosyncratic(self) -> dict[str, float]:
        return {
            aid: (ac.idio_sd * self.rng.gauss(0.0, 1.0) if ac.idio_sd > 0 else 0.0)
            for aid, ac in self.network.activities.items()
        }


def base_plus_shock_duration(network: Network, aid: str,
                             factor_values: dict[str, float],
                             idio: float) -> float:
    """Unhastened duration: base + shared-factor effects + residual, floored > 0."""
    ac = network.activities[aid]
    total = ac.base_duration + idio
    for fid, loading in ac.risk_loadings.items():
        total += loading * factor_values[fid]
    return max(total, 1e-6)


def apply_crashing(network: Network, base_durations: dict[str, float],
                   crash_days: dict[str, float]) -> dict[str, float]:
    """Subtract crash days but never below each activity's data-supported floor."""
    out: dict[str, float] = {}
    for aid, ac in network.activities.items():
        allowed = max(0.0, min(float(crash_days.get(aid, 0.0)),
                               ac.base_duration - ac.min_duration))
        out[aid] = max(base_durations[aid] - allowed, ac.min_duration)
    return out


def crash_cost(network: Network, crash_days: dict[str, float]) -> float:
    total = 0.0
    for aid, ac in network.activities.items():
        used = max(0.0, min(float(crash_days.get(aid, 0.0)),
                            ac.base_duration - ac.min_duration))
        total += used * ac.crash_cost_per_day
    return total
