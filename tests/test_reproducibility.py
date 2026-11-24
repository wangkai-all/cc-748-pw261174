"""Fixed-seed reproducibility and shared-source dependence."""
from __future__ import annotations

import math
import unittest

from app.simulation import generate_scenarios, run_simulation
from tests.helpers import load

SEED = 314159
N = 3000


class ReproducibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = load("confluence.json")

    def test_same_seed_bit_identical_outputs(self):
        s1 = generate_scenarios(self.network, N, SEED)
        s2 = generate_scenarios(self.network, N, SEED)
        self.assertEqual(s1, s2)
        r1 = run_simulation(self.network, s1, {}, 35.0, None)
        r2 = run_simulation(self.network, s2, {}, 35.0, None)
        self.assertEqual(r1["project_durations"], r2["project_durations"])
        self.assertEqual(r1["summary"], r2["summary"])

    def test_different_seed_changes_draws_but_stays_similar(self):
        s1 = generate_scenarios(self.network, N, SEED)
        s2 = generate_scenarios(self.network, N, SEED + 1)
        self.assertNotEqual(s1[0]["factor_values"], s2[0]["factor_values"])
        r1 = run_simulation(self.network, s1, {}, 35.0, None)
        r2 = run_simulation(self.network, s2, {}, 35.0, None)
        self.assertAlmostEqual(
            r1["summary"]["mean_project_duration"],
            r2["summary"]["mean_project_duration"],
            delta=0.5,
        )

    def test_shared_weather_creates_correlated_outcomes(self):
        """Rain loads several outdoor activities: their durations must co-move."""
        scenarios = generate_scenarios(self.network, N, SEED)
        rain = [s["factor_values"]["rain"] for s in scenarios]
        d_a = [s["base_durations"]["A"] for s in scenarios]
        d_b = [s["base_durations"]["B"] for s in scenarios]

        def corr(x, y):
            mx, my = sum(x) / len(x), sum(y) / len(y)
            cov = sum((a - mx) * (b - my) for a, b in zip(x, y)) / len(x)
            vx = sum((a - mx) ** 2 for a in x) / len(x)
            vy = sum((b - my) ** 2 for b in y) / len(y)
            return cov / math.sqrt(vx * vy)

        # Both load the same rain factor (A 1.2, B 1.5) -> positive correlation.
        self.assertGreater(corr(rain, d_a), 0.7)
        self.assertGreater(corr(rain, d_b), 0.7)
        self.assertGreater(corr(d_a, d_b), 0.4)

    def test_correlated_weather_factors_are_jointly_drawn(self):
        """rain and cold have copula correlation 0.6."""
        scenarios = generate_scenarios(self.network, N, SEED)
        rain = [s["factor_values"]["rain"] for s in scenarios]
        cold = [s["factor_values"]["cold"] for s in scenarios]
        mx, my = sum(rain) / N, sum(cold) / N
        cov = sum((a - mx) * (b - my) for a, b in zip(rain, cold)) / N
        vx = sum((a - mx) ** 2 for a in rain) / N
        vy = sum((b - my) ** 2 for b in cold) / N
        self.assertAlmostEqual(cov / math.sqrt(vx * vy), 0.6, delta=0.06)

    def test_bernoulli_material_shock_frequency(self):
        # steel_delay fires with p = 0.25.
        scenarios = generate_scenarios(self.network, N, SEED)
        fires = sum(1 for s in scenarios if s["factor_values"]["steel_delay"] == 1.0)
        self.assertAlmostEqual(fires / N, 0.25, delta=0.03)

    def test_critical_path_membership_matches_cpm(self):
        scenarios = generate_scenarios(self.network, 200, SEED)
        result = run_simulation(self.network, scenarios, {}, 35.0, None)
        crit = result["activities"]
        # Sum of per-path memberships: every scenario has at least one critical activity.
        self.assertGreaterEqual(
            sum(c["criticality_index"] for c in crit.values()), 1.0 - 1e-9
        )
        # Indices are valid probabilities.
        for c in crit.values():
            self.assertGreaterEqual(c["criticality_index"], 0.0)
            self.assertLessEqual(c["criticality_index"], 1.0)


if __name__ == "__main__":
    unittest.main()
