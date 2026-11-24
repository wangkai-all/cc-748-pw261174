"""Background jobs: plan signatures guard against stale overwrites."""
from __future__ import annotations

import time
import unittest

from app.jobs import JobManager, plan_signature
from app.models import ValidationError
from tests.helpers import load


def wait_done(manager, job_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = manager.get(job_id)
        if job.status in ("done", "error", "canceled"):
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish in time")


class JobTests(unittest.TestCase):
    def setUp(self):
        self.manager = JobManager()
        self.network = load("confluence.json")

    def test_signature_changes_with_plan(self):
        spec1 = {"seed": 1, "n_scenarios": 100, "deadline": 35.0,
                 "budget": 0.0, "crash_days": {}}
        spec2 = dict(spec1, crash_days={"A": 1.0})
        self.assertNotEqual(
            plan_signature(self.network.id, spec1),
            plan_signature(self.network.id, spec2),
        )
        self.assertEqual(
            plan_signature(self.network.id, spec1),
            plan_signature(self.network.id, dict(spec1)),
        )

    def test_simulation_job_runs_and_carries_signature(self):
        spec = {"seed": 9, "n_scenarios": 500, "deadline": 35.0,
                "budget": 0.0, "crash_days": {}}
        job = self.manager.start_simulation(self.network, spec)
        done = wait_done(self.manager, job.job_id)
        self.assertEqual(done.status, "done")
        self.assertEqual(done.result["signature"], job.signature)
        self.assertEqual(done.result["plan"]["seed"], 9)
        self.assertEqual(done.result["summary"]["n_scenarios"], 500)
        self.assertIsNotNone(done.result["summary"]["on_time_probability"])
        self.assertIn("scenarios", done.result)

    def test_stale_job_result_cannot_overwrite_newer_page(self):
        """Simulate the UI's staleness guard using server-side signatures."""
        spec_old = {"seed": 1, "n_scenarios": 300, "deadline": 35.0,
                    "budget": 0.0, "crash_days": {}}
        spec_new = dict(spec_old, crash_days={"C": 2.0})
        old_job = self.manager.start_simulation(self.network, spec_old)
        # User changes the plan before the old job is consumed.
        new_job = self.manager.start_simulation(self.network, spec_new)
        old_done = wait_done(self.manager, old_job.job_id)
        new_done = wait_done(self.manager, new_job.job_id)
        # The page only accepts a payload matching the latest signature.
        self.assertNotEqual(old_done.signature, new_done.signature)
        self.assertFalse(old_done.result["signature"] == new_job.signature)
        self.assertTrue(new_done.result["signature"] == new_job.signature)
        self.assertEqual(new_done.result["summary"]["total_crash_days"], 2.0)

    def test_cancel_prevents_result_delivery(self):
        spec = {"seed": 3, "n_scenarios": 20000, "deadline": 35.0,
                "budget": 0.0, "crash_days": {}}
        job = self.manager.start_simulation(self.network, spec)
        self.assertTrue(self.manager.cancel(job.job_id))
        done = wait_done(self.manager, job.job_id)
        self.assertIn(done.status, ("canceled", "done"))
        if done.status == "canceled":
            self.assertIsNone(done.result)
            self.assertEqual(done.error["code"], "canceled")

    def test_invalid_plan_rejected_before_thread_starts(self):
        bad_spec = {"seed": 1, "n_scenarios": 100, "deadline": 1.0,
                    "budget": 0.0, "crash_days": {}}
        with self.assertRaises(ValidationError) as ctx:
            self.manager.start_simulation(self.network, bad_spec)
        codes = [i["code"] for i in ctx.exception.issues]
        self.assertIn("unreachable_deadline", codes)

    def test_rapid_plan_changes_keep_only_latest_signature(self):
        """Two plans posted back-to-back: both jobs complete, but only the
        latest signature is the one a correctly-behaving page would accept."""
        specs = [
            {"seed": 1, "n_scenarios": 300, "deadline": 35.0,
             "budget": 0.0, "crash_days": {}},
            {"seed": 1, "n_scenarios": 300, "deadline": 35.0,
             "budget": 0.0, "crash_days": {"D": 1.0}},
        ]
        jobs = [self.manager.start_simulation(self.network, s) for s in specs]
        signatures = [j.signature for j in jobs]
        self.assertEqual(len(set(signatures)), 2)
        for j in jobs:
            wait_done(self.manager, j.job_id)
        latest_signature = signatures[-1]
        for j in jobs:
            # The page applies a payload only if it matches the latest sig.
            accepted = j.result["signature"] == latest_signature
            if j is jobs[-1]:
                self.assertTrue(accepted)
            else:
                self.assertFalse(accepted)
                self.assertNotIn(j.result, [jobs[-1].result])

    def test_frontier_job_and_budget_cap(self):
        spec = {"seed": 5, "n_scenarios": 400, "deadline": 35.0,
                "budget": 15000.0, "step_days": 1.0, "crash_days": {}}
        job = self.manager.start_frontier(self.network, spec)
        done = wait_done(self.manager, job.job_id)
        self.assertEqual(done.status, "done")
        points = done.result["frontier"]["points"]
        self.assertTrue(points)
        self.assertLessEqual(points[-1]["cost"], 15000.0 + 1e-9)
        for pt in points:
            if pt["on_time_probability"] is not None:
                self.assertGreaterEqual(pt["on_time_probability"], 0.0)
                self.assertLessEqual(pt["on_time_probability"], 1.0)


if __name__ == "__main__":
    unittest.main()
