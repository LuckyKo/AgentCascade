#!/usr/bin/env python3
"""Generate an Obsidian .canvas graph of the AgentCascade memory files.

Wraps obsidian-second-brain's deterministic link_graph.py (pure stdlib) and
renders the result as a native Obsidian JSON Canvas file so the user can open
the memory knowledge graph visually in Obsidian.

Scope: top-level memory files only (archive/ is excluded - those are obsolete).

Usage:
    python scripts/generate_memory_canvas.py \
        --vault "N:/work/WD/AgentCascade/.agent_lessons" \
        --obsidian-skill-root "N:/work/WD/obsidian-second-brain" \
        [--out atlas.canvas]

The output .canvas is written to the vault root by default. It is a non-.md
file, so it does not pollute future link_graph runs.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path


def _load_link_graph(obsidian_skill_root: Path):
    """Import build_graph from the obsidian-second-brain scripts dir."""
    scripts_dir = obsidian_skill_root / "scripts"
    if not scripts_dir.is_dir():
        raise SystemExit(f"obsidian skill scripts dir not found: {scripts_dir}")
    sys.path.insert(0, str(scripts_dir))
    from link_graph import build_graph  # noqa: E402
    return build_graph


def _is_top_level(rel_path: str) -> bool:
    """Top-level memory files have no '/' in their vault-relative path."""
    return "/" not in rel_path


def _filter_to_top_level(graph: dict) -> dict:
    keep = {n["id"] for n in graph["nodes"] if _is_top_level(n["path"])}
    nodes = [n for n in graph["nodes"] if n["id"] in keep]
    edges = [e for e in graph["edges"] if e["from"] in keep and e["to"] in keep]
    typed_edges = [e for e in graph.get("typed_edges", [])
                   if e["from"] in keep and e["to"] in keep]
    node_ids = {n["id"] for n in nodes}
    orphans = [n["path"] for n in nodes if n["degree"] == 0]
    ranked = sorted(nodes, key=lambda n: n["degree"], reverse=True)
    stats = dict(graph.get("stats", {}))
    stats.update({
        "node_count": len(nodes),
        "edge_count": len(edges),
        "typed_edge_count": len(typed_edges),
        "orphan_count": len(orphans),
        "top_hubs": [{"path": n["path"], "title": n["title"], "degree": n["degree"]}
                     for n in ranked[:10]],
        "orphans": orphans[:50],
        "scope": "top-level",
    })
    return {
        "nodes": nodes,
        "edges": edges,
        "typed_edges": typed_edges,
        "stats": stats,
    }


# ── Color palette (Obsidian canvas node colors) ──────────────────────────────
# Hubs (degree >= hub_threshold): green. Linked: blue. Orphans: red.
COLOR_HUB = "#2ea043"      # green - load-bearing memories
COLOR_LINKED = "#1f6feb"   # blue  - connected memories
COLOR_ORPHAN = "#d1242f"   # red   - unlinked (cleanup candidates)


def _degree_role(degree: int, hub_threshold: int) -> str:
    if degree == 0:
        return "orphan"
    if degree >= hub_threshold:
        return "hub"
    return "linked"


def _layout(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[float, float]]:
    """Simple force-directed layout (deterministic via fixed seed).

    Good enough for a few hundred nodes. Hubs get pulled toward center by
    giving them lower initial radius; orphans are pushed outward afterward.
    """
    rng = random.Random(42)
    n = len(nodes)
    if n == 0:
        return {}

    ids = [nd["id"] for nd in nodes]
    pos = {i: (rng.uniform(-1, 1) * 300, rng.uniform(-1, 1) * 300) for i in ids}

    # adjacency for repulsion/attraction
    adj = {i: [] for i in ids}
    for e in edges:
        if e["from"] in pos and e["to"] in pos:
            adj[e["from"]].append(e["to"])
            adj[e["to"]].append(e["from"])

    degree = {nd["id"]: nd["degree"] for nd in nodes}

    iterations = 120
    area = max(600, n * 4)
    k = math.sqrt(area / max(n, 1))  # ideal spring length

    for it in range(iterations):
        temp = (1.0 - it / iterations) * k * 0.5 + 2.0
        disp = {i: [0.0, 0.0] for i in ids}
        # repulsion between all pairs (O(n^2), fine for n ~ 400)
        for a in range(n):
            ia = ids[a]
            xa, ya = pos[ia]
            for b in range(a + 1, n):
                ib = ids[b]
                xb, yb = pos[ib]
                dx = xa - xb
                dy = ya - yb
                dist2 = dx * dx + dy * dy
                if dist2 < 1e-4:
                    dx = rng.uniform(-0.1, 0.1)
                    dy = rng.uniform(-0.1, 0.1)
                    dist2 = dx * dx + dy * dy
                dist = math.sqrt(dist2)
                force = (k * k) / dist
                fx = dx / dist * force
                fy = dy / dist * force
                disp[ia][0] += fx
                disp[ia][1] += fy
                disp[ib][0] -= fx
                disp[ib][1] -= fy
        # attraction along edges (spring)
        for e in edges:
            ia, ib = e["from"], e["to"]
            if ia not in pos or ib not in pos:
                continue
            xa, ya = pos[ia]
            xb, yb = pos[ib]
            dx = xa - xb
            dy = ya - yb
            dist = math.sqrt(dx * dx + dy * dy) or 1e-4
            force = (dist * dist) / k
            fx = dx / dist * force
            fy = dy / dist * force
            disp[ia][0] -= fx
            disp[ia][1] -= fy
            disp[ib][0] += fx
            disp[ib][1] += fy
        # apply with temperature clamp; hubs damped more (stay central)
        for i in ids:
            dx, dy = disp[i]
            dist = math.sqrt(dx * dx + dy * dy) or 1e-4
            damp = 0.5 if degree.get(i, 0) >= 6 else 1.0
            move = min(dist, temp) / dist
            nx = pos[i][0] + dx * move * damp
            ny = pos[i][1] + dy * move * damp
            pos[i] = (nx, ny)

    # Center the layout and push orphans outward to the periphery.
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    cx, cy = sum(xs) / n, sum(ys) / n
    max_deg = max((degree.get(i, 0) for i in ids), default=1) or 1
    final = {}
    for idx, i in enumerate(ids):
        x, y = pos[i][0] - cx, pos[i][1] - cy
        d = degree.get(i, 0)
        if d == 0:
            # push orphans to a large ring
            ang = math.atan2(y, x)
            target_r = 2600 + (idx % 7) * 40
            x, y = math.cos(ang) * target_r, math.sin(ang) * target_r
        elif d >= max_deg:
            # top hub pinned near center
            x *= 0.3
            y *= 0.3
        final[i] = (x, y)
    return final


def _node_size(degree: int) -> tuple[int, int]:
    """Hubs are larger; linked medium; orphans small."""
    if degree >= 12:
        return 420, 90
    if degree >= 6:
        return 360, 80
    if degree >= 3:
        return 300, 70
    if degree == 0:
        return 220, 50
    return 260, 60


def _label(node: dict) -> str:
    """Short display label: title + degree badge."""
    title = node["title"]
    deg = node["degree"]
    if len(title) > 48:
        title = title[:45] + "..."
    if deg == 0:
        return f"{title}\n(orphan)"
    return f"{title}\n(deg {deg})"


def build_canvas(graph: dict, hub_threshold: int) -> tuple[dict, dict]:
    nodes = graph["nodes"]
    edges = graph["edges"]
    pos = _layout(nodes, edges)

    canvas_nodes = []
    for idx, nd in enumerate(nodes):
        x, y = pos.get(nd["id"], (0.0, 0.0))
        w, h = _node_size(nd["degree"])
        role = _degree_role(nd["degree"], hub_threshold)
        color = {"hub": COLOR_HUB, "linked": COLOR_LINKED, "orphan": COLOR_ORPHAN}[role]
        canvas_nodes.append({
            "id": f"n{idx}",
            "type": "text",
            "x": round(x - w / 2),
            "y": round(y - h / 2),
            "width": w,
            "height": h,
            "color": color,
            "text": _label(nd),
            # store the real vault-relative path so a reviewer/tool can map back
            "_meta_path": nd["path"],
        })

    id_of = {nd["id"]: f"n{i}" for i, nd in enumerate(nodes)}
    canvas_edges = []
    for eidx, e in enumerate(edges):
        a, b = id_of.get(e["from"]), id_of.get(e["to"])
        if not a or not b:
            continue
        canvas_edges.append({
            "id": f"e{eidx}",
            "fromNode": a,
            "toNode": b,
            "fromSide": "right",
            "toSide": "left",
        })

    # Obsidian JSON Canvas: nodes + edges. Extra keys are ignored by Obsidian.
    canvas = {"nodes": canvas_nodes, "edges": canvas_edges}
    meta = {
        "node_count": len(canvas_nodes),
        "edge_count": len(canvas_edges),
        "roles": {
            "hub": sum(1 for c in canvas_nodes if c["color"] == COLOR_HUB),
            "linked": sum(1 for c in canvas_nodes if c["color"] == COLOR_LINKED),
            "orphan": sum(1 for c in canvas_nodes if c["color"] == COLOR_ORPHAN),
        },
    }
    return canvas, meta


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Generate an Obsidian .canvas of AC memory files")
    ap.add_argument("--vault", required=True, help="Path to .agent_lessons vault root")
    ap.add_argument("--obsidian-skill-root", required=True,
                    help="Path to obsidian-second-brain repo (for link_graph.py)")
    ap.add_argument("--out", default=None,
                    help="Output canvas path (default: <vault>/atlas.canvas)")
    ap.add_argument("--hub-threshold", type=int, default=6,
                    help="Degree at/above which a node is colored as a hub")
    args = ap.parse_args(argv[1:])

    vault = Path(args.vault).expanduser().resolve()
    skill_root = Path(args.obsidian_skill_root).expanduser().resolve()
    if not vault.is_dir():
        print(f"vault path does not exist: {vault}", file=sys.stderr)
        return 2

    build_graph = _load_link_graph(skill_root)
    full = build_graph(vault, None)
    top = _filter_to_top_level(full)

    canvas, meta = build_canvas(top, args.hub_threshold)

    out_path = Path(args.out) if args.out else vault / "atlas.canvas"
    # Strip the internal _meta_path from node dicts before writing (keep file clean),
    # but emit a companion .json with path mapping for tooling.
    write_nodes = []
    path_map = {}
    for c in canvas["nodes"]:
        p = c.pop("_meta_path")
        path_map[c["id"]] = p
        write_nodes.append(c)
    canvas["nodes"] = write_nodes

    out_path.write_text(json.dumps(canvas, ensure_ascii=False, indent=1), encoding="utf-8")

    # Companion mapping file (node id -> vault-relative path) for tooling/review.
    map_path = out_path.with_suffix(".map.json")
    map_path.write_text(json.dumps(path_map, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"canvas written: {out_path}")
    print(f"path map written: {map_path}")
    print(f"nodes={meta['node_count']} edges={meta['edge_count']} "
          f"hubs={meta['roles']['hub']} linked={meta['roles']['linked']} "
          f"orphans={meta['roles']['orphan']}")
    st = top["stats"]
    print(f"top-level stats: nodes={st['node_count']} edges={st['edge_count']} "
          f"orphans={st['orphan_count']} dangling={st.get('dangling_link_count', 'n/a')}")
    print("top hubs:")
    for h in st["top_hubs"][:8]:
        print(f"  {h['degree']:>3}  {h['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
