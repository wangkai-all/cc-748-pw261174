"""Structural validation: cycles, resource capacity, unreachable deadlines."""
from __future__ import annotations

import unittest

from app.models import ValidationError
from app.schedule import (check_deadline, check_resource_capacity,
                          validate_runnable)
from tests.helpers import load


class ValidationTests(unittest.TestCase):
    def test_cyclic_dependency_reported(self):
        network = load("bad_cycle.json")
        with self.assertRaises(ValidationError) as ctx:
            network.topological_order()
        codes = [i["code"] for i in ctx.exception.issues]
        self.assertIn("cyclic_dependency", codes)
        self.assertEqual(
            set(ctx.exception.issues[0]["activity_ids"]), {"X", "Y", "Z"}
        )

    def test_cycle_blocks_full_validation(self):
        network = load("bad_cycle.json")
        issues = validate_runnable(network)
        self.assertEqual([i["code"] for i in issues], ["cyclic_dependency"])

    def test_resource_capacity_conflict_reported(self):
        network = load("bad_resource.json")
        violations = check_resource_capacity(network)
        self.assertTrue(violations)
        issues = validate_runnable(network)
        self.assertIn("resource_capacity_conflict", [i["code"] for i in issues])
        v = next(i for i in issues if i["code"] == "resource_capacity_conflict")
        self.assertEqual(v["resource_id"], "crew")
        self.assertGreater(v["demand"], v["capacity"])
        self.assertEqual(set(v["activities"]), {"P", "Q"})

    def test_valid_network_has_no_resource_issue(self):
        network = load("confluence.json")
        self.assertEqual(check_resource_capacity(network), [])
        self.assertEqual(validate_runnable(network), [])

    def test_unreachable_deadline(self):
        network = load("bad_deadline.json")
        info = check_deadline(network, 7.0)
        self.assertFalse(info["reachable"])
        self.assertEqual(info["minimum_achievable_duration"], 9.0)
        issues = validate_runnable(network, deadline=7.0)
        self.assertIn("unreachable_deadline", [i["code"] for i in issues])

    def test_reachable_deadline_passes(self):
        network = load("bad_deadline.json")
        self.assertTrue(check_deadline(network, 10.0)["reachable"])
        self.assertEqual(validate_runnable(network, deadline=10.0), [])

    def test_missing_predecessor_and_factor_are_rejected(self):
        from app.models import Network
        bad = {
            "id": "x", "name": "x",
            "risk_factors": [], "resources": [],
            "activities": [
                {"id": "A", "base_duration": 3, "min_duration": 2,
                 "predecessors": ["GHOST"], "risk_loadings": {"NOPE": 1.0}},
            ],
        }
        with self.assertRaises(ValidationError) as ctx:
            Network.from_dict(bad)
        codes = {i["code"] for i in ctx.exception.issues}
        self.assertIn("predecessor_missing", codes)
        self.assertIn("loading_unknown_factor", codes)

    def test_bad_correlation_matrix_rejected(self):
        from app.models import Network
        bad = {
            "id": "x", "name": "x",
            "risk_factors": [{"id": "f1", "kind": "normal", "mean": 0, "sd": 1},
                             {"id": "f2", "kind": "normal", "mean": 0, "sd": 1}],
            "factor_correlation": [[1.0, 0.99], [0.99, -1.0]],
            "resources": [], "activities": [],
        }
        with self.assertRaises(ValidationError) as ctx:
            Network.from_dict(bad)
        codes = {i["code"] for i in ctx.exception.issues}
        self.assertTrue({"correlation_bad_diagonal", "correlation_not_psd"} & codes)


if __name__ == "__main__":
    unittest.main()
