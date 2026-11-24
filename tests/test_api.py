"""End-to-end HTTP API tests against the real stdlib server."""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.request
import urllib.error

from app.server import make_server


def http(method: str, url: str, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            ctype = resp.headers["Content-Type"]
            body = json.loads(raw) if "application/json" in ctype else raw
            return resp.status, body, dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8")), dict(exc.headers)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server("127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def wait_job(self, job_id):
        for _ in range(200):
            status, body, _ = http("GET", f"{self.base}/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            if body["status"] in ("done", "error", "canceled"):
                return body
            time.sleep(0.1)
        self.fail("job timed out")

    def test_catalog_and_network_payload(self):
        status, body, _ = http("GET", f"{self.base}/api/datasets")
        self.assertEqual(status, 200)
        files = {d["file"] for d in body["datasets"]}
        self.assertIn("tiny_analytic.json", files)
        self.assertIn("confluence.json", files)

        status, body, _ = http("GET", f"{self.base}/api/networks/confluence")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["activities"]), 8)
        self.assertIn("nodes", body["layout"])
        self.assertGreater(body["deterministic"]["nominal_duration"], 0.0)

    def test_invalid_networks_return_structured_422(self):
        status, body, _ = http("GET", f"{self.base}/api/networks/bad_cycle")
        self.assertEqual(status, 422)
        self.assertIn("cyclic_dependency", [i["code"] for i in body["issues"]])

        status, body, _ = http(
            "GET", f"{self.base}/api/validate?dataset=bad_deadline.json&deadline=7"
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["valid"])
        self.assertIn("unreachable_deadline", [i["code"] for i in body["issues"]])

    def test_simulate_lifecycle_and_export(self):
        spec = {"dataset": "tiny_analytic.json", "seed": 42,
                "n_scenarios": 1000, "deadline": 22, "budget": 0,
                "crash_days": {}}
        status, body, _ = http("POST", f"{self.base}/api/simulate", spec)
        self.assertEqual(status, 202)
        job_id = body["job_id"]
        final = self.wait_job(job_id)
        self.assertEqual(final["status"], "done")
        self.assertEqual(final["signature"], body["signature"])
        result = final["result"]
        self.assertAlmostEqual(
            result["summary"]["on_time_probability"], 0.999999999, delta=0.03
        )

        status, csv_body, headers = http(
            "GET", f"{self.base}/api/export/{job_id}?format=csv&dataset=tiny_analytic.json"
        )
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["Content-Type"])
        lines = csv_body if isinstance(csv_body, str) else csv_body.decode()
        self.assertIn("factor:fa", lines)
        # Header row plus one row per scenario.
        self.assertEqual(len(lines.strip().splitlines()), 1000 + 1)

        status, json_body, _ = http(
            "GET", f"{self.base}/api/export/{job_id}?format=json&dataset=tiny_analytic.json"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(json_body["scenarios"]), 1000)

    def test_crash_beyond_limit_rejected_with_422(self):
        spec = {"dataset": "tiny_analytic.json", "seed": 42,
                "n_scenarios": 500, "deadline": 22, "budget": 0,
                "crash_days": {"A": 99}}
        status, body, _ = http("POST", f"{self.base}/api/simulate", spec)
        self.assertEqual(status, 422)
        self.assertIn("crash_beyond_limit", [i["code"] for i in body["issues"]])

    def test_frontier_lifecycle_and_export(self):
        spec = {"dataset": "confluence.json", "seed": 7,
                "n_scenarios": 600, "deadline": 35, "budget": 20000,
                "crash_days": {}, "step_days": 1.0}
        status, body, _ = http("POST", f"{self.base}/api/frontier", spec)
        self.assertEqual(status, 202)
        final = self.wait_job(body["job_id"])
        self.assertEqual(final["status"], "done")
        points = final["result"]["frontier"]["points"]
        self.assertGreaterEqual(len(points), 2)
        status, csv_body, headers = http(
            "GET", f"{self.base}/api/export/{body['job_id']}?format=csv"
        )
        self.assertEqual(status, 200)
        self.assertIn("expected_duration", csv_body)

    def test_static_index_served(self):
        with urllib.request.urlopen(f"{self.base}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("工期风险分析台", html)

    def test_unknown_dataset_404(self):
        status, body, _ = http("GET", f"{self.base}/api/networks/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["issues"][0]["code"], "dataset_not_found")


if __name__ == "__main__":
    unittest.main()
