"""Deterministic layered layout for the activity network (longest-path ranks)."""
from __future__ import annotations

from .models import Network


def compute_layout(network: Network, width: float = 1080.0,
                   height: float = 560.0) -> dict:
    order = network.topological_order()
    rank: dict[str, int] = {}
    for aid in order:
        preds = network.activities[aid].predecessors
        rank[aid] = 0 if not preds else 1 + max(rank[p] for p in preds)

    layers: dict[int, list[str]] = {}
    for aid, r in rank.items():
        layers.setdefault(r, []).append(aid)
    for r in layers:
        layers[r].sort()

    max_rank = max(layers, default=0)
    margin_x, margin_y = 110.0, 80.0
    dx = (width - 2 * margin_x) / max(1, max_rank)
    nodes = {}
    for r, members in layers.items():
        dy = (height - 2 * margin_y) / max(1, len(members))
        for k, aid in enumerate(members):
            nodes[aid] = {
                "x": round(margin_x + r * dx, 2),
                "y": round(margin_y + (k + 0.5) * dy, 2),
                "rank": r,
            }

    edges = []
    for ac in network.activities.values():
        for p in ac.predecessors:
            edges.append({"source": p, "target": ac.id})
    return {"width": width, "height": height, "nodes": nodes, "edges": edges}
