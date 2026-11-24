"""Closed-form checks against the analytic tiny parallel network.

Network: A = 10 + 2 Z_a, B = 12 + 1.5 Z_b (independent standard normals),
M is a near-zero merge milestone. Analytic results:

* P(A is the critical activity)
    = P(A > B) = Phi((mu_A - mu_B) / sqrt(sd_A^2 + sd_B^2))
    = Phi(-0.8) ~= 0.2119  (B has the larger mean, so B is usually critical)
* P(max(A, B) <= 22) = Phi((22-10)/2) * Phi((22-12)/1.5)
    = Phi(6) Phi(20/3) ~= 0.9999999990 (deadline effectively only binds A)
* E[max(A, B)] ~= 12.3005 (derived in the test below)
"""
from __future__ import annotations

import math
import unittest

from app.rng import std_normal_cdf, std_normal_pdf
from app.simulation import generate_scenarios, run_simulation
from tests.helpers import load

N = 20000
SEED = 20261124


class TinyAnalyticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = load("tiny_analytic.json")
        cls.scenarios = generate_scenarios(cls.network, N, SEED)
        cls.result = run_simulation(cls.network, cls.scenarios, {}, 22.0, None)

    def test_closed_form_criticality(self):
        p_a_critical = std_normal_cdf((10.0 - 12.0) / math.sqrt(4.0 + 2.25))
        self.assertAlmostEqual(p_a_critical, 0.2119, places=3)
        observed_a = self.result["activities"]["A"]["criticality_index"]
        observed_b = self.result["activities"]["B"]["criticality_index"]
        # Monte Carlo standard error at p~0.21 is ~0.0029; allow 4 sigma.
        self.assertAlmostEqual(observed_a, p_a_critical, delta=0.012)
        self.assertAlmostEqual(observed_b, 1.0 - p_a_critical, delta=0.012)
        # The merge milestone is always critical.
        self.assertAlmostEqual(self.result["activities"]["M"]["criticality_index"], 1.0)

    def test_closed_form_on_time(self):
        expected = std_normal_cdf((22.0 - 10.0) / 2.0) * std_normal_cdf((22.0 - 12.0) / 1.5)
        self.assertAlmostEqual(expected, 0.999999999, delta=5e-4)
        self.assertAlmostEqual(
            self.result["summary"]["on_time_probability"], expected, delta=0.012
        )

    def test_closed_form_expected_max(self):
        # E[max(A,B)] with delta = mu_B - mu_A = 2, sigma = 2.5:
        # E[max] = mu_A + delta * Phi(delta/sigma) + sigma * phi(delta/sigma)
        delta, sigma = 2.0, 2.5
        z = delta / sigma
        expected = 10.0 + delta * std_normal_cdf(z) + sigma * std_normal_pdf(z)
        self.assertAlmostEqual(expected, 12.3005, delta=0.01)
        self.assertAlmostEqual(
            self.result["summary"]["mean_project_duration"], expected, delta=0.05
        )

    def test_milestone_does_not_drive_duration(self):
        for pd in self.result["project_durations"][:200]:
            self.assertGreater(pd, 9.0)  # real activities dominate the 0.01 merge


if __name__ == "__main__":
    unittest.main()
