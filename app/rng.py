"""Seeded random streams and distribution helpers.

A single ``random.Random`` instance is used per simulation. The draw order
(one latent normal per risk factor, then one idiosyncratic normal per
activity, repeated for every scenario) is fixed and documented in
``simulation.py``. Because crashing only transforms durations *after* the
draws, every plan evaluated on the same (seed, n_scenarios) pair consumes
exactly the same random numbers — common random numbers (CRN), so plan
comparisons are not confounded by sampling noise.
"""
from __future__ import annotations

import math
import random


def make_rng(seed: int) -> random.Random:
    return random.Random(seed)


def std_normal_cdf(z: float) -> float:
    """Phi(z) for a standard normal."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def std_normal_pdf(z: float) -> float:
    return math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)


def cholesky(A: list[list[float]]) -> list[list[float]]:
    """Lower-triangular Cholesky factor of a symmetric (semi-)positive matrix."""
    n = len(A)
    L = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            s = sum(L[i][k] * L[j][k] for k in range(j))
            if i == j:
                v = A[i][i] - s
                if v < -1e-9:
                    raise ValueError("matrix is not positive semidefinite")
                L[i][j] = math.sqrt(max(v, 0.0))
            else:
                L[i][j] = (A[i][j] - s) / L[j][j]
    return L
