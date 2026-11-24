"""Pure-stdlib HTTP server: REST API plus the static single-page frontend."""
from __future__ import annotations

import json
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .crashing import validate_crash_request
from .exports import frontier_to_csv, result_to_json, scenarios_to_csv
from .jobs import JobManager
from .layout import compute_layout
from .models import Network, ValidationError
from .schedule import cpm, deterministic_horizon, validate_runnable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
WEB_DIR = os.path.join(ROOT, "web")

MAX_SCENARIOS = 100_000
DEFAULT_SCENARIOS = 5000


def load_dataset_catalog() -> list[dict]:
    catalog = []
    for fname in sorted(os.listdir(DATA_DIR)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(DATA_DIR, fname), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        catalog.append({
            "file": fname,
            "id": data.get("id", fname[:-5]),
            "name": data.get("name", fname),
            "description": data.get("description", ""),
            "expect_invalid": bool(data.get("expect_invalid", False)),
            "tags": data.get("tags", []),
        })
    return catalog


def load_network(file_name: str) -> Network:
    safe = os.path.basename(file_name)
    path = os.path.join(DATA_DIR, safe)
    if not os.path.isfile(path):
        raise FileNotFoundError(safe)
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return Network.from_dict(data)


def network_payload(network: Network) -> dict:
    return {
        "id": network.id,
        "name": network.name,
        "description": network.description,
        "activities": [
            {
                "id": ac.id,
                "name": ac.name,
                "base_duration": ac.base_duration,
                "min_duration": ac.min_duration,
                "predecessors": ac.predecessors,
                "risk_loadings": ac.risk_loadings,
                "idio_sd": ac.idio_sd,
                "crash_cost_per_day": ac.crash_cost_per_day,
                "resources": ac.resources,
            }
            for ac in (network.activities[a] for a in network.topological_order())
        ],
        "risk_factors": [
            {"id": f.id, "name": f.name, "kind": f.kind, "mean": f.mean,
             "sd": f.sd, "p": f.p, "description": f.description}
            for f in network.factors.values()
        ],
        "resources": [
            {"id": r.id, "name": r.name, "capacity": r.capacity}
            for r in network.resources.values()
        ],
        "factor_correlation": network.factor_correlation,
        "factor_order": network.factor_order,
        "layout": compute_layout(network),
        "deterministic": _deterministic_payload(network),
    }


def _deterministic_payload(network: Network) -> dict:
    info = deterministic_horizon(network)
    plan = info["plan"]
    return {
        "nominal_duration": plan["project_duration"],
        "critical_path": plan["critical_path"],
        "minimum_achievable_duration": info["minimum_achievable"],
        "total_float": {a: round(tf, 6) for a, tf in plan["total_float"].items()},
    }


def normalize_spec(body: dict) -> dict:
    spec = {
        "seed": int(body.get("seed", 42)),
        "n_scenarios": min(int(body.get("n_scenarios", DEFAULT_SCENARIOS)), MAX_SCENARIOS),
        "deadline": body.get("deadline"),
        "budget": float(body.get("budget", 0.0)),
        "crash_days": body.get("crash_days", {}) or {},
        "step_days": float(body.get("step_days", 1.0)),
    }
    if spec["n_scenarios"] <= 0:
        raise ValidationError([{"code": "bad_n_scenarios",
                                "message": "n_scenarios must be positive"}])
    if spec["seed"] < 0:
        raise ValidationError([{"code": "bad_seed",
                                "message": "seed must be a non-negative integer"}])
    return spec


class Handler(BaseHTTPRequestHandler):
    manager = JobManager()
    server_version = "ScheduleRisk/1.0"

    # ------------------------------------------------------------- helpers
    def _send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, content_type: str, filename: str | None = None):
        body = text.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if filename:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValidationError([{"code": "bad_json", "message": str(exc)}])

    def _network_or_error(self, file_name: str) -> Network | None:
        try:
            return load_network(file_name)
        except FileNotFoundError:
            self._send_json({"issues": [{"code": "dataset_not_found",
                                         "message": f"unknown dataset {file_name!r}"}]}, 404)
        except ValidationError as exc:
            self._send_json({"issues": exc.issues}, 422)
        return None

    def log_message(self, fmt, *args):  # quieter logs
        pass

    # --------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/datasets":
                return self._send_json({"datasets": load_dataset_catalog()})

            if path.startswith("/api/networks/"):
                file_name = path.rsplit("/", 1)[-1] + ".json"
                network = self._network_or_error(file_name)
                if network is None:
                    return
                return self._send_json(network_payload(network))

            if path == "/api/validate":
                qs = self._qs(parsed.query)
                file_name = qs.get("dataset", "")
                network = self._network_or_error(file_name)
                if network is None:
                    return
                deadline = qs.get("deadline")
                issues = validate_runnable(
                    network, float(deadline) if deadline not in (None, "") else None)
                return self._send_json({
                    "valid": not issues,
                    "issues": issues,
                    "deterministic": _deterministic_payload(network),
                })

            if path.startswith("/api/jobs/"):
                job_id = path.rsplit("/", 1)[-1]
                job = self.manager.get(job_id)
                if not job:
                    return self._send_json(
                        {"issues": [{"code": "job_not_found",
                                     "message": f"unknown job {job_id!r}"}]}, 404)
                return self._send_json(self._job_payload(job))

            if path == "/api/jobs":
                return self._send_json({"jobs": self.manager.list_jobs()})

            if path.startswith("/api/export/"):
                return self._handle_export(path, parsed.query)

            return self._serve_static(path)
        except ValidationError as exc:
            return self._send_json({"issues": exc.issues}, 422)
        except Exception as exc:  # pragma: no cover - defensive
            return self._send_json(
                {"issues": [{"code": "internal_error", "message": str(exc)}]}, 500)

    @staticmethod
    def _qs(query: str) -> dict[str, str]:
        from urllib.parse import parse_qs
        return {k: v[-1] for k, v in parse_qs(query).items()}

    def _job_payload(self, job) -> dict:
        payload = {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
            "progress": job.progress,
            "signature": job.signature,
            "network_id": job.network_id,
            "spec": job.spec,
            "error": job.error,
        }
        if job.status == "done":
            payload["result"] = job.result
        return payload

    def _handle_export(self, path: str, query: str):
        qs = self._qs(query)
        job_id = path.split("/")[-1]
        fmt = qs.get("format", "csv")
        job = self.manager.get(job_id)
        if not job:
            return self._send_json(
                {"issues": [{"code": "job_not_found", "message": "unknown job"}]}, 404)
        if job.status != "done":
            return self._send_json(
                {"issues": [{"code": "job_not_ready",
                             "message": f"job is {job.status}"}]}, 409)
        if job.kind == "frontier":
            if fmt == "json":
                return self._send_text(
                    json.dumps(job.result["frontier"], indent=2, ensure_ascii=False),
                    "application/json; charset=utf-8", "frontier.json")
            return self._send_text(frontier_to_csv(job.result["frontier"]),
                                   "text/csv; charset=utf-8", "frontier.csv")

        network = load_network(qs.get("dataset", ""))
        result = job.result
        if fmt == "json":
            return self._send_text(result_to_json(network, result),
                                   "application/json; charset=utf-8", "scenarios.json")
        return self._send_text(scenarios_to_csv(network, result),
                               "text/csv; charset=utf-8", "scenarios.csv")

    # --------------------------------------------------------------- POST
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/api/simulate", "/api/frontier"):
                body = self._read_json()
                network = self._network_or_error(body.get("dataset", ""))
                if network is None:
                    return
                spec = normalize_spec(body)
                # Pre-check request validity before spawning a thread.
                pre_issues = validate_runnable(network, spec["deadline"])
                if path == "/api/simulate":
                    pre_issues += validate_crash_request(
                        network, {k: float(v) for k, v in spec["crash_days"].items()})
                if pre_issues:
                    return self._send_json({"issues": pre_issues}, 422)
                if path == "/api/simulate":
                    job = self.manager.start_simulation(network, spec)
                else:
                    job = self.manager.start_frontier(network, spec)
                return self._send_json(self._job_payload(job), 202)

            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[-2]
                ok = self.manager.cancel(job_id)
                if not ok:
                    return self._send_json(
                        {"issues": [{"code": "job_not_cancelable",
                                     "message": "job missing or already finished"}]}, 409)
                return self._send_json({"canceled": job_id})

            return self._send_json(
                {"issues": [{"code": "not_found", "message": path}]}, 404)
        except ValidationError as exc:
            return self._send_json({"issues": exc.issues}, 422)

    # -------------------------------------------------------------- static
    def _serve_static(self, path: str):
        if path in ("/", ""):
            path = "/index.html"
        rel = os.path.normpath(path.lstrip("/"))
        full = os.path.join(WEB_DIR, rel)
        if not full.startswith(WEB_DIR + os.sep) or not os.path.isfile(full):
            return self._send_json(
                {"issues": [{"code": "not_found", "message": path}]}, 404)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8"
                         if ctype.startswith("text/") or ctype.endswith("javascript") else ""))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), Handler)
