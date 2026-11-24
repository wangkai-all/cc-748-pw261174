"""Scenario-level data export (CSV and JSON)."""
from __future__ import annotations

import csv
import io
import json

from .models import Network


def scenarios_to_csv(network: Network, result: dict) -> str:
    """One row per scenario: factor draws, per-activity durations, project KPIs."""
    scenarios = result["scenarios"]
    order = network.topological_order()
    factor_ids = network.factor_order
    buf = io.StringIO()
    writer = csv.writer(buf)
    header = ["scenario"]
    header += [f"factor:{fid}" for fid in factor_ids]
    header += [f"duration:{aid}" for aid in order]
    header += ["project_duration", "on_time", "critical_path"]
    writer.writerow(header)

    on_time = result["summary"].get("deadline") is not None
    deadline = result["summary"].get("deadline")
    for i, sc in enumerate(scenarios):
        row = [i]
        row += [sc["factor_values"].get(fid, "") for fid in factor_ids]
        row += [sc["base_durations"][aid] for aid in order]
        pdur = result["project_durations"][i]
        row.append(pdur)
        row.append("" if not on_time else (1 if pdur <= deadline + 1e-9 else 0))
        row.append("->".join(result["critical_paths"][i]))
        writer.writerow(row)
    return buf.getvalue()


def result_to_json(network: Network, result: dict) -> str:
    payload = {
        "network_id": network.id,
        "network_name": network.name,
        "plan": result.get("plan", {}),
        "summary": result["summary"],
        "activities": result["activities"],
        "delay_attribution": result["delay_attribution"],
        "histogram": result["histogram"],
        "scenarios": [
            {
                "scenario": i,
                "factor_values": sc["factor_values"],
                "base_durations": sc["base_durations"],
                "project_duration": result["project_durations"][i],
                "critical_path": result["critical_paths"][i],
            }
            for i, sc in enumerate(result["scenarios"])
        ],
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def frontier_to_csv(frontier: dict) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["step", "cost", "expected_duration", "duration_sd",
                     "days_saved", "p5", "p50", "p95", "crash_days_json"])
    for pt in frontier["points"]:
        writer.writerow([
            pt["step"], pt["cost"], pt["expected_duration"], pt["duration_sd"],
            pt["days_saved"], pt["p5"], pt["p50"], pt["p95"],
            json.dumps(pt["crash_days"], sort_keys=True),
        ])
    return buf.getvalue()
