"""Crashing: CRN consistency, supported intervals, budget frontier behavior."""
from __future__ import annotations

import unittest

from app.crashing import (cost_duration_frontier, plan_under_budget,
                          validate_crash_request)
from app.risk import crash_cost
from app.simulation import generate_scenarios, run_simulation
from tests.helpers import load

SEED = 777
N = 4000


class CrashingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.network = load("confluence.json")
        cls.scenarios = generate_scenarios(cls.network, N, SEED)
        cls.order = cls.network.topological_order()

    def test_crash_cannot_go_below_supported_floor(self):
        issues = validate_crash_request(self.network, {"A": 999.0})
        self.assertEqual(issues[0]["code"], "crash_beyond_limit")
        self.assertAlmostEqual(issues[0]["max_crash_days"], 1.5)
        self.assertEqual(validate_crash_request(self.network, {"A": 1.0}), [])
        self.assertEqual(validate_crash_request(self.network, {"UNKNOWN": 1.0})[0]["code"],
                         "unknown_activity")

    def test_crn_same_scenarios_used_by_every_plan(self):
        baseline = run_simulation(self.network, self.scenarios, {}, 35.0, None)
        crashed = run_simulation(self.network, self.scenarios,
                                 {"C": 2.0, "D": 1.0}, 35.0, None)
        # The underlying draws are identical: same n, same base durations.
        self.assertEqual(len(baseline["scenarios"]), len(crashed["scenarios"]))
        for s1, s2 in zip(baseline["scenarios"], crashed["scenarios"]):
            self.assertEqual(s1["factor_values"], s2["factor_values"])
            self.assertEqual(s1["base_durations"], s2["base_durations"])
        # Crashing cannot increase any scenario's project duration.
        for a, b in zip(baseline["project_durations"], crashed["project_durations"]):
            self.assertLessEqual(b, a + 1e-9)
        self.assertLess(
            crashed["summary"]["mean_project_duration"],
            baseline["summary"]["mean_project_duration"],
        )
        # Cost accounting.
        self.assertAlmostEqual(
            crashed["summary"]["total_crash_cost"], 2.0 * 2500 + 1.0 * 3000
        )

    def test_frontier_monotone_cost_and_non_increasing_duration(self):
        frontier = cost_duration_frontier(
            self.network, self.scenarios, budget=40000, step_days=1.0,
            deadline=35.0,
        )
        points = frontier["points"]
        self.assertGreaterEqual(len(points), 3)
        for prev, nxt in zip(points, points[1:]):
            self.assertGreaterEqual(nxt["cost"], prev["cost"] - 1e-9)
            self.assertLessEqual(
                nxt["expected_duration"], prev["expected_duration"] + 1e-9
            )
            self.assertGreaterEqual(nxt["on_time_probability"],
                                    prev["on_time_probability"] - 1e-12)
        # Budget cap is respected.
        self.assertLessEqual(points[-1]["cost"], 40000 + 1e-9)
        # Every allocation in the frontier stays inside supported intervals.
        for pt in points:
            self.assertEqual(
                validate_crash_request(self.network, pt["crash_days"]), []
            )
            self.assertTrue(plan_under_budget(
                self.network, pt["crash_days"], 40000)["within_budget"])

    def test_zero_budget_frontier_is_single_baseline_point(self):
        frontier = cost_duration_frontier(self.network, self.scenarios, budget=0.0)
        self.assertEqual(len(frontier["points"]), 1)
        self.assertEqual(frontier["points"][0]["cost"], 0.0)

    def test_crash_cost_linear_and_capped(self):
        self.assertAlmostEqual(crash_cost(self.network, {"A": 1.5}), 1.5 * 1200)
        # Asking beyond the cap is silently clamped inside cost accounting,
        # but rejected up-front by validate_crash_request.
        self.assertAlmostEqual(
            crash_cost(self.network, {"A": 50.0}), 1.5 * 1200
        )


if __name__ == "__main__":
    unittest.main()
