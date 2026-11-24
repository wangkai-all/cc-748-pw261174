"""Background calculation jobs with plan-version guarding.

Every job is tied to an immutable plan signature (network id, seed, scenario
count, deadline, budget, crash allocation). When the user changes the plan
while a job is still running, the client starts a new job and simply stops
polling the old one; the old job, when it finishes, stores its result under
its own signature and can never overwrite the newer page state. Job results
also carry the signature back, so the client can discard stale payloads.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from .crashing import cost_duration_frontier, validate_crash_request
from .models import Network, ValidationError
from .schedule import validate_runnable
from .simulation import generate_scenarios, run_simulation


def plan_signature(network_id: str, spec: dict) -> str:
    payload = json.dumps(
        {"network_id": network_id, "spec": spec},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class Job:
    job_id: str
    signature: str
    kind: str
    status: str = "queued"
    progress: float = 0.0
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    result: dict | None = None
    error: dict | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    canceled: bool = False
    spec: dict = field(default_factory=dict)
    network_id: str = ""


class JobManager:
    def __init__(self, max_jobs: int = 20):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self) -> list[dict]:
        with self._lock:
            return [self._public(j) for j in sorted(
                self._jobs.values(), key=lambda x: x.created_at, reverse=True)]

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status in ("done", "error", "canceled"):
            return False
        job.cancel_event.set()
        return True

    def start_simulation(self, network: Network, spec: dict) -> Job:
        """Validate, then launch a simulation job on a worker thread."""
        crash_days = {k: float(v) for k, v in (spec.get("crash_days") or {}).items()}
        deadline = spec.get("deadline")
        budget = float(spec.get("budget", 0.0))
        n_scenarios = int(spec.get("n_scenarios", 5000))
        seed = int(spec.get("seed", 42))

        issues = validate_runnable(network, deadline)
        issues += validate_crash_request(network, crash_days)
        if issues:
            raise ValidationError(issues)

        job = self._new_job(network, "simulation", spec)

        def worker():
            try:
                job.started_at = time.time()
                job.status = "running"

                def progress(p):
                    job.progress = round(0.2 + 0.7 * p, 4)
                    if job.cancel_event.is_set():
                        raise _Canceled()

                job.progress = 0.05
                scenarios = generate_scenarios(network, n_scenarios, seed)
                job.progress = 0.2
                if job.cancel_event.is_set():
                    raise _Canceled()
                result = run_simulation(network, scenarios, crash_days, deadline, progress)
                if job.cancel_event.is_set():
                    raise _Canceled()
                result["plan"] = {
                    "seed": seed,
                    "n_scenarios": n_scenarios,
                    "deadline": deadline,
                    "budget": budget,
                    "crash_days": crash_days,
                }
                result["job_id"] = job.job_id
                result["signature"] = job.signature
                job.result = self._serialize(result)
                job.progress = 1.0
                job.status = "done"
            except _Canceled:
                job.status = "canceled"
                job.error = {"code": "canceled", "message": "job canceled by client"}
            except ValidationError as exc:
                job.status = "error"
                job.error = {"code": "validation_error", "issues": exc.issues}
            except Exception as exc:  # pragma: no cover - defensive
                job.status = "error"
                job.error = {"code": "internal_error", "message": str(exc)}
            finally:
                job.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return job

    def start_frontier(self, network: Network, spec: dict) -> Job:
        budget = float(spec.get("budget", 0.0))
        n_scenarios = int(spec.get("n_scenarios", 5000))
        seed = int(spec.get("seed", 42))
        step_days = float(spec.get("step_days", 1.0))

        issues = validate_runnable(network)
        if budget < 0:
            issues.append({"code": "bad_budget", "message": "budget must be non-negative"})
        if step_days <= 0:
            issues.append({"code": "bad_step", "message": "step_days must be positive"})
        if issues:
            raise ValidationError(issues)

        job = self._new_job(network, "frontier", spec)

        def worker():
            try:
                job.started_at = time.time()
                job.status = "running"
                job.progress = 0.05
                scenarios = generate_scenarios(network, n_scenarios, seed)
                job.progress = 0.15
                if job.cancel_event.is_set():
                    raise _Canceled()
                frontier = cost_duration_frontier(
                    network, scenarios, budget, step_days,
                    deadline=spec.get("deadline"),
                    progress_callback=lambda p: setattr(job, "progress", round(0.15 + 0.8 * p, 4)),
                )
                if job.cancel_event.is_set():
                    raise _Canceled()
                job.result = self._serialize({
                    "frontier": frontier,
                    "plan": {"seed": seed, "n_scenarios": n_scenarios,
                             "budget": budget, "step_days": step_days},
                    "job_id": job.job_id,
                    "signature": job.signature,
                })
                job.progress = 1.0
                job.status = "done"
            except _Canceled:
                job.status = "canceled"
                job.error = {"code": "canceled", "message": "job canceled by client"}
            except ValidationError as exc:
                job.status = "error"
                job.error = {"code": "validation_error", "issues": exc.issues}
            except Exception as exc:  # pragma: no cover - defensive
                job.status = "error"
                job.error = {"code": "internal_error", "message": str(exc)}
            finally:
                job.finished_at = time.time()

        threading.Thread(target=worker, daemon=True).start()
        return job

    def _new_job(self, network: Network, kind: str, spec: dict) -> Job:
        with self._lock:
            sig = plan_signature(network.id, spec)
            job_id = uuid.uuid4().hex[:12]
            job = Job(job_id=job_id, signature=sig, kind=kind,
                      spec=spec, network_id=network.id)
            self._jobs[job_id] = job
            if len(self._jobs) > self._max_jobs:
                for old in sorted(self._jobs.values(), key=lambda x: x.created_at):
                    if old.status in ("done", "error", "canceled"):
                        del self._jobs[old.job_id]
                        break
        return job

    def _public(self, job: Job) -> dict:
        return {
            "job_id": job.job_id,
            "kind": job.kind,
            "status": job.status,
            "progress": job.progress,
            "signature": job.signature,
            "network_id": job.network_id,
            "spec": job.spec,
            "error": job.error,
        }

    @staticmethod
    def _serialize(obj):
        """Make everything JSON-safe (numpy-free, but tuples/sets may appear)."""
        if isinstance(obj, dict):
            return {str(k): JobManager._serialize(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [JobManager._serialize(v) for v in obj]
        if isinstance(obj, set):
            return sorted(JobManager._serialize(v) for v in obj)
        return obj


class _Canceled(Exception):
    pass
