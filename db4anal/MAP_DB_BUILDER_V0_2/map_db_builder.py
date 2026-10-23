#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import math
import statistics
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

try:
    import orjson
except ImportError as exc:  # pragma: no cover
    raise SystemExit("orjson is required: py -m pip install orjson") from exc

import replay_protocol_builder as rp
import world_state_builder as ws

VERSION = "0.2.0"
CACHE_SCHEMA = "PYHOTS_MAP_FACTS_V0_1"
CACHE_CONFIG_VERSION = "0.2.0"


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS))


def load_json(path: Path) -> Any:
    return orjson.loads(path.read_bytes())


def _idx(data: Any, n: int) -> Any:
    return rp._data_index(data, n)


def _to_int(v: Any) -> Optional[int]:
    return rp.safe_int(v)


def _event_type(e: dict) -> str:
    return rp._event_type(e)


def _event_time(e: dict) -> Optional[float]:
    return rp._event_time(e)


def _parse_data(e: dict) -> Any:
    return rp.parse_embedded_json(e.get("Data")) if "Data" in e else e


def _round_coord(v: float, precision: float = 0.25) -> float:
    if precision <= 0:
        return float(v)
    return round(round(float(v) / precision) * precision, 4)


def _median(values: list[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def _quantile(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    xs = sorted(float(x) for x in values)
    if len(xs) == 1:
        return xs[0]
    pos = max(0.0, min(1.0, q)) * (len(xs) - 1)
    lo = int(math.floor(pos)); hi = int(math.ceil(pos))
    if lo == hi:
        return xs[lo]
    frac = pos - lo
    return xs[lo] * (1.0 - frac) + xs[hi] * frac


def _bounds(points: list[tuple[float, float]]) -> dict:
    if not points:
        return {"point_count": 0, "raw": None, "robust_p01_p99": None}
    xs = [p[0] for p in points]; ys = [p[1] for p in points]
    return {
        "point_count": len(points),
        "raw": {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)},
        "robust_p01_p99": {
            "min_x": _quantile(xs, 0.01), "max_x": _quantile(xs, 0.99),
            "min_y": _quantile(ys, 0.01), "max_y": _quantile(ys, 0.99),
        },
    }


def _tag(data: Any) -> Optional[tuple[int, int]]:
    a = _to_int(_idx(data, 0)); b = _to_int(_idx(data, 1))
    return (a, b) if a is not None and b is not None else None


def _tag_text(t: tuple[int, int]) -> str:
    return f"{t[0]}:{t[1]}"


def _unit_owner_class(control: Optional[int], tracker_player_map: dict[int, str]) -> str:
    if control is None:
        return "NO_CONTROL_ID"
    if control in tracker_player_map:
        return "ROSTER_PLAYER_CONTROLLED"
    return "NON_ROSTER_CONTROL_ID"


def _replay_scope(state: dict) -> tuple[str, list[str]]:
    players = state.get("players", {}) or {}
    meta = state.get("metadata", {}) or {}
    integ = state.get("integrity", {}) or {}
    reasons: list[str] = []
    if len(players) != 10:
        reasons.append("NONSTANDARD_PLAYER_COUNT")
    if not meta.get("map_id"):
        reasons.append("UNKNOWN_MAP_ID")
    if meta.get("build") is None:
        reasons.append("UNKNOWN_BUILD")
    ident = integ.get("player_identity", {}) or {}
    if int(ident.get("players_with_hero_unit", 0)) != len(players):
        reasons.append("INCOMPLETE_HERO_OWNERSHIP")
    if int(ident.get("players_with_position_anchors", 0)) != len(players):
        reasons.append("INCOMPLETE_POSITION_COVERAGE")
    if (integ.get("position_scale", {}) or {}).get("status") == "FAIL_SCALE_MISMATCH":
        reasons.append("POSITION_SCALE_MISMATCH")
    return ("CORE_10_PLAYER" if not reasons else "QUARANTINED", reasons)


def _hero_spawn_points_from_state(state: dict) -> list[dict]:
    rows: list[dict] = []
    for pid, p in (state.get("players", {}) or {}).items():
        ident = p.get("identity", {}) or {}
        for a in p.get("position_anchors", []) or []:
            if a.get("source") not in {"TRACKER_UNIT_BORN", "TRACKER_UNIT_REVIVED", "STAT_PLAYERSPAWNED"}:
                continue
            if a.get("x") is None or a.get("y") is None:
                continue
            rows.append({
                "player_id": pid,
                "team": ident.get("team"),
                "hero_unit_id": ident.get("hero_unit_id"),
                "time": a.get("time"),
                "x": float(a["x"]), "y": float(a["y"]),
                "source": a.get("source"),
            })
    return rows


def _movement_cells_from_state(state: dict, cell_size: float) -> list[dict]:
    cells: dict[tuple[int, int], dict] = {}
    if cell_size <= 0:
        return []
    for p in (state.get("players", {}) or {}).values():
        for a in p.get("position_anchors", []) or []:
            if a.get("source") != "TRACKER_PERIODIC_POSITION":
                continue
            try:
                x = float(a["x"]); y = float(a["y"])
            except (KeyError, TypeError, ValueError):
                continue
            ix = math.floor(x / cell_size); iy = math.floor(y / cell_size)
            key = (ix, iy)
            row = cells.setdefault(key, {
                "cell_x": ix, "cell_y": iy,
                "center_x": (ix + 0.5) * cell_size,
                "center_y": (iy + 0.5) * cell_size,
                "hits": 0,
            })
            row["hits"] += 1
    return sorted(cells.values(), key=lambda r: (-r["hits"], r["cell_x"], r["cell_y"]))


def extract_map_facts(replay: dict, source_name: str, schema: Optional[rp.AbilitySchemaIndex], *, coord_precision: float = 0.25, movement_cell_size: float = 4.0) -> dict:
    # Build verified ownership/position metadata using exactly the frozen World State semantics.
    state = ws.build_world_state(replay, source_name, schema, movement_detail="none")
    scope, quarantine_reasons = _replay_scope(state)
    players = rp.replay_players(replay)
    tracker_player_map, _ = ws.build_tracker_player_map(replay, players)

    units: dict[tuple[int, int], dict] = {}
    spawn_groups: dict[tuple, dict] = {}
    stat_groups: dict[tuple, dict] = {}
    type_change_counts: Counter[tuple[str, str]] = Counter()
    owner_change_counts: Counter[tuple[Optional[int], Optional[int]]] = Counter()
    all_points: list[tuple[float, float]] = []

    def spawn_group(unit_type: str, x: float, y: float, owner_class: str) -> dict:
        qx = _round_coord(x, coord_precision); qy = _round_coord(y, coord_precision)
        key = (unit_type, qx, qy, owner_class)
        return spawn_groups.setdefault(key, {
            "unit_type": unit_type,
            "x": qx, "y": qy,
            "owner_class": owner_class,
            "spawn_count": 0,
            "born_event_count": 0,
            "init_event_count": 0,
            "first_time": None, "last_time": None,
            "control_player_ids": set(), "upkeep_player_ids": set(),
            "died_count": 0,
            "lifetimes": [], "death_displacements": [],
        })

    events = [e for e in (replay.get("TrackerEvents", []) or []) if isinstance(e, dict)]
    events.sort(key=lambda e: (_event_time(e) if _event_time(e) is not None else float("inf")))
    for e in events:
        typ = _event_type(e); t = _event_time(e); data = _parse_data(e)
        if t is None:
            continue
        if typ in {"unitBornEvent", "unitInitEvent"}:
            tg = _tag(data); unit_type = _idx(data, 2)
            control = _to_int(_idx(data, 3)); upkeep = _to_int(_idx(data, 4))
            x = _to_int(_idx(data, 5)); y = _to_int(_idx(data, 6))
            if not tg or unit_type is None:
                continue
            rec = {
                "tag": _tag_text(tg), "unit_type": str(unit_type), "born_time": float(t),
                "control_player_id": control, "upkeep_player_id": upkeep,
                "x": float(x) if x is not None else None, "y": float(y) if y is not None else None,
                "owner_class": _unit_owner_class(control, tracker_player_map),
                "died_time": None,
            }
            units[tg] = rec
            if x is not None and y is not None:
                all_points.append((float(x), float(y)))
                g = spawn_group(str(unit_type), float(x), float(y), rec["owner_class"])
                g["spawn_count"] += 1
                g["born_event_count" if typ == "unitBornEvent" else "init_event_count"] += 1
                g["first_time"] = float(t) if g["first_time"] is None else min(g["first_time"], float(t))
                g["last_time"] = float(t) if g["last_time"] is None else max(g["last_time"], float(t))
                if control is not None: g["control_player_ids"].add(control)
                if upkeep is not None: g["upkeep_player_ids"].add(upkeep)
                rec["spawn_key"] = (str(unit_type), g["x"], g["y"], rec["owner_class"])

        elif typ == "unitDiedEvent":
            tg = _tag(data); x = _to_int(_idx(data, 3)); y = _to_int(_idx(data, 4))
            if x is not None and y is not None:
                all_points.append((float(x), float(y)))
            if not tg or tg not in units:
                continue
            u = units[tg]; u["died_time"] = float(t)
            sk = u.get("spawn_key")
            if sk in spawn_groups:
                g = spawn_groups[sk]; g["died_count"] += 1
                life = float(t) - float(u["born_time"])
                if life >= 0: g["lifetimes"].append(life)
                if x is not None and y is not None and u.get("x") is not None and u.get("y") is not None:
                    g["death_displacements"].append(math.hypot(float(x)-float(u["x"]), float(y)-float(u["y"])))

        elif typ == "unitTypeChangeEvent":
            tg = _tag(data); new_type = _idx(data, 2)
            if tg and tg in units and new_type is not None:
                old = str(units[tg].get("unit_type")); new = str(new_type)
                type_change_counts[(old, new)] += 1
                units[tg]["unit_type"] = new

        elif typ == "unitOwnerChangeEvent":
            control = _to_int(_idx(data, 2)); upkeep = _to_int(_idx(data, 3))
            owner_change_counts[(control, upkeep)] += 1

        elif typ == "statGameEvent":
            st = ws.parse_stat_game_event(e)
            if not st:
                continue
            x = ws._fixed_value((st.get("fixed") or {}).get("PositionX"))
            y = ws._fixed_value((st.get("fixed") or {}).get("PositionY"))
            if x is None or y is None:
                continue
            all_points.append((float(x), float(y)))
            qx = _round_coord(x, coord_precision); qy = _round_coord(y, coord_precision)
            key = (str(st.get("name")), qx, qy)
            g = stat_groups.setdefault(key, {
                "event_name": str(st.get("name")), "x": qx, "y": qy,
                "count": 0, "first_time": None, "last_time": None,
            })
            g["count"] += 1
            g["first_time"] = float(t) if g["first_time"] is None else min(g["first_time"], float(t))
            g["last_time"] = float(t) if g["last_time"] is None else max(g["last_time"], float(t))

    finalized_spawns: list[dict] = []
    for g in spawn_groups.values():
        disps = list(g.pop("death_displacements"))
        lifes = list(g.pop("lifetimes"))
        died = int(g["died_count"])
        stationary = sum(d <= 1.5 for d in disps)
        finalized_spawns.append({
            **g,
            "control_player_ids": sorted(g["control_player_ids"]),
            "upkeep_player_ids": sorted(g["upkeep_player_ids"]),
            "median_lifetime": _median(lifes),
            "median_death_displacement": _median(disps),
            "stationary_death_ratio": (stationary / len(disps)) if disps else None,
            "lifecycle_samples": len(lifes),
        })
    finalized_spawns.sort(key=lambda r: (r["unit_type"], r["x"], r["y"], r["owner_class"]))

    type_counts: dict[str, dict] = {}
    for g in finalized_spawns:
        u = type_counts.setdefault(g["unit_type"], {"unit_type": g["unit_type"], "spawn_count": 0, "distinct_spawn_points": 0, "owner_classes": Counter()})
        u["spawn_count"] += int(g["spawn_count"]); u["distinct_spawn_points"] += 1; u["owner_classes"][g["owner_class"]] += int(g["spawn_count"])
    for u in type_counts.values():
        u["owner_classes"] = dict(u["owner_classes"])

    meta = state.get("metadata", {}) or {}
    return {
        "schema": {"name": "PYHOTS_MAP_REPLAY_FACTS", "version": VERSION},
        "metadata": {
            "source": source_name,
            "source_id": hashlib.sha256(source_name.encode("utf-8", "replace")).hexdigest()[:16],
            "map_id": meta.get("map_id"), "map_name": meta.get("map_name"),
            "build": meta.get("build"), "player_count": len(state.get("players", {}) or {}),
            "replay_scope": scope, "quarantine_reasons": quarantine_reasons,
            "position_scale_status": ((state.get("integrity", {}) or {}).get("position_scale", {}) or {}).get("status"),
            "position_scale_best": ((state.get("integrity", {}) or {}).get("position_scale", {}) or {}).get("best_scale"),
        },
        "observed_event_bounds": _bounds(all_points),
        "unit_spawn_points": finalized_spawns,
        "unit_type_summary": sorted(type_counts.values(), key=lambda r: (-r["spawn_count"], r["unit_type"])),
        "stat_spatial_points": sorted(stat_groups.values(), key=lambda r: (r["event_name"], r["x"], r["y"])),
        "hero_spawn_points": _hero_spawn_points_from_state(state),
        "hero_movement_cells": _movement_cells_from_state(state, movement_cell_size),
        "unit_type_change_edges": [
            {"from_unit_type": a, "to_unit_type": b, "count": n}
            for (a, b), n in sorted(type_change_counts.items())
        ],
        "unit_owner_change_summary": [
            {"control_player_id": a, "upkeep_player_id": b, "count": n}
            for (a, b), n in sorted(owner_change_counts.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1])))
        ],
    }


def _support_tier(replay_support: int) -> str:
    if replay_support >= 5: return "STRONG_5_PLUS_REPLAYS"
    if replay_support >= 3: return "MODERATE_3_4_REPLAYS"
    if replay_support == 2: return "LIMITED_2_REPLAYS"
    return "SINGLE_REPLAY_ONLY"


def _cluster_points(points: list[dict], radius: float, *, identity_fields: tuple[str, ...]) -> list[dict]:
    """Deterministic grid-assisted clustering with strict identity partitioning.

    Clustering is spatial evidence only. It does not infer Fort/Camp/Objective semantics.
    """
    if not points:
        return []
    radius = max(0.01, float(radius)); cell = radius
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for p in points:
        ident = tuple(str(p.get(k)) for k in identity_fields)
        groups[ident].append(p)

    out: list[dict] = []
    for ident, rows in sorted(groups.items(), key=lambda kv: kv[0]):
        clusters: list[dict] = []
        grid: dict[tuple[int, int], set[int]] = defaultdict(set)
        for p in sorted(rows, key=lambda r: (float(r["x"]), float(r["y"]), str(r.get("replay_id")))):
            x = float(p["x"]); y = float(p["y"]); w = max(1, int(p.get("event_count", 1)))
            gx = math.floor(x / cell); gy = math.floor(y / cell)
            candidate_ids: set[int] = set()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    candidate_ids.update(grid.get((gx+dx, gy+dy), set()))
            best = None; best_d = None
            for ci in candidate_ids:
                c = clusters[ci]
                d = math.hypot(x - c["cx"], y - c["cy"])
                if d <= radius and (best_d is None or d < best_d):
                    best = ci; best_d = d
            if best is None:
                best = len(clusters)
                clusters.append({
                    "cx": x, "cy": y, "weight": 0, "points": [],
                    "replay_ids": set(), "builds": set(), "sources": [],
                    "point_sources": set(), "hero_unit_ids": set(),
                    "event_count": 0, "died_count": 0, "lifecycle_samples": 0,
                    "lifetime_samples": [], "displacement_samples": [], "stationary_samples": [],
                    "control_player_ids": set(), "upkeep_player_ids": set(),
                })
            c = clusters[best]
            old_cell = (math.floor(c["cx"] / cell), math.floor(c["cy"] / cell))
            new_weight = c["weight"] + w
            c["cx"] = (c["cx"] * c["weight"] + x * w) / new_weight
            c["cy"] = (c["cy"] * c["weight"] + y * w) / new_weight
            c["weight"] = new_weight; c["event_count"] += w
            c["points"].append((x, y))
            if p.get("replay_id") is not None: c["replay_ids"].add(str(p["replay_id"]))
            if p.get("build") is not None: c["builds"].add(int(p["build"]))
            example_source = p.get("replay_source") or p.get("source")
            if example_source and str(example_source) not in c["sources"] and len(c["sources"]) < 5: c["sources"].append(str(example_source))
            if p.get("point_source") is not None: c["point_sources"].add(str(p.get("point_source")))
            if p.get("hero_unit_id") is not None: c["hero_unit_ids"].add(str(p.get("hero_unit_id")))
            c["died_count"] += int(p.get("died_count", 0) or 0)
            c["lifecycle_samples"] += int(p.get("lifecycle_samples", 0) or 0)
            if p.get("median_lifetime") is not None: c["lifetime_samples"].append(float(p["median_lifetime"]))
            if p.get("median_death_displacement") is not None: c["displacement_samples"].append(float(p["median_death_displacement"]))
            if p.get("stationary_death_ratio") is not None: c["stationary_samples"].append(float(p["stationary_death_ratio"]))
            c["control_player_ids"].update(p.get("control_player_ids", []) or [])
            c["upkeep_player_ids"].update(p.get("upkeep_player_ids", []) or [])
            new_cell = (math.floor(c["cx"] / cell), math.floor(c["cy"] / cell))
            if old_cell != new_cell:
                grid[old_cell].discard(best)
            grid[new_cell].add(best)

        for c in clusters:
            spread = max((math.hypot(x-c["cx"], y-c["cy"]) for x, y in c["points"]), default=0.0)
            rec = {identity_fields[i]: ident[i] for i in range(len(identity_fields))}
            rec.update({
                "x": round(c["cx"], 4), "y": round(c["cy"], 4),
                "cluster_radius_max": round(spread, 4),
                "event_count": c["event_count"],
                "replay_support": len(c["replay_ids"]),
                "support_tier": _support_tier(len(c["replay_ids"])),
                "builds": sorted(c["builds"]),
                "source_examples": c["sources"],
                "died_count": c["died_count"],
                "lifecycle_samples": c["lifecycle_samples"],
                "median_lifetime_across_replay_groups": _median(c["lifetime_samples"]),
                "median_death_displacement_across_replay_groups": _median(c["displacement_samples"]),
                "median_stationary_death_ratio": _median(c["stationary_samples"]),
                "control_player_ids": sorted(c["control_player_ids"]),
                "upkeep_player_ids": sorted(c["upkeep_player_ids"]),
                "point_sources": sorted(c["point_sources"]),
                "hero_unit_ids": sorted(c["hero_unit_ids"]),
                "semantic_class": "UNCLASSIFIED",
            })
            out.append(rec)
    return sorted(out, key=lambda r: (-int(r["replay_support"]), -int(r["event_count"]), tuple(str(r.get(k)) for k in identity_fields), r["x"], r["y"]))



def _observed_motion_class(cluster: dict) -> str:
    """Describe only replay-observed displacement evidence; this is not gameplay semantics."""
    samples = int(cluster.get("lifecycle_samples", 0) or 0)
    disp = cluster.get("median_death_displacement_across_replay_groups")
    stationary = cluster.get("median_stationary_death_ratio")
    if samples <= 0 and disp is None and stationary is None:
        return "NO_LIFECYCLE_EVIDENCE"
    try:
        d = float(disp) if disp is not None else None
    except (TypeError, ValueError):
        d = None
    try:
        sr = float(stationary) if stationary is not None else None
    except (TypeError, ValueError):
        sr = None
    if (sr is not None and sr >= 0.80) or (d is not None and d <= 1.5):
        return "STATIONARY_OR_LOCAL"
    if d is not None and d >= 5.0 and (sr is None or sr <= 0.20):
        return "MOBILE"
    return "MIXED_OR_UNRESOLVED"


def _map_object_candidates(spawn_clusters: list[dict], replay_count: int) -> list[dict]:
    """Separate map-controlled/non-roster spatial facts from roster-player generated units.

    This is an evidence partition, not a Fort/Camp/Objective classifier.
    """
    min_support = 1 if replay_count <= 1 else 2
    out: list[dict] = []
    for c in spawn_clusters:
        if c.get("owner_class") == "ROSTER_PLAYER_CONTROLLED":
            continue
        support = int(c.get("replay_support", 0) or 0)
        if support < min_support:
            continue
        r = dict(c)
        r["support_fraction"] = round(support / replay_count, 4) if replay_count else 0.0
        r["observed_motion_class"] = _observed_motion_class(c)
        r["candidate_basis"] = "NON_ROSTER_CONTROL_AND_REPLAY_SUPPORT"
        out.append(r)
    return out


def _stable_hero_spawn_regions(hero_regions_raw: list[dict], replay_count: int, min_fraction: float = 0.50) -> list[dict]:
    """Expose only cross-replay stable team spawn regions.

    Raw team-level birth/revive clusters remain available separately as evidence.
    This avoids treating one-off hero births caused by forms/special states as base spawn regions.
    """
    if replay_count <= 1:
        min_support = 1
    else:
        min_support = max(2, int(math.ceil(replay_count * float(min_fraction))))
    out = []
    for c in hero_regions_raw:
        support = int(c.get("replay_support", 0) or 0)
        if support < min_support:
            continue
        r = dict(c)
        r["support_fraction"] = round(support / replay_count, 4) if replay_count else 0.0
        r["region_basis"] = "TEAM_BIRTH_REVIVE_CROSS_REPLAY_STABILITY"
        r["min_support_required"] = min_support
        out.append(r)
    return out


def _replay_native_landmark_links(init_clusters: list[dict], spawn_clusters: list[dict], join_radius: float = 1.5) -> list[dict]:
    """Spatially join explicit replay *Init landmarks to nearby unit spawn clusters.

    The event_name remains the semantic evidence source; nearby unit types are only
    spatial provenance and are not promoted to Fort/Camp/etc by this function.
    """
    out = []
    radius = float(join_radius)
    for init in init_clusters:
        ix, iy = float(init.get("x", 0.0)), float(init.get("y", 0.0))
        nearby = []
        for c in spawn_clusters:
            dx = float(c.get("x", 0.0)) - ix
            dy = float(c.get("y", 0.0)) - iy
            d = math.hypot(dx, dy)
            if d > radius:
                continue
            nearby.append({
                "unit_type": c.get("unit_type"),
                "owner_class": c.get("owner_class"),
                "x": c.get("x"),
                "y": c.get("y"),
                "distance": round(d, 4),
                "replay_support": int(c.get("replay_support", 0) or 0),
                "support_tier": c.get("support_tier"),
                "observed_motion_class": _observed_motion_class(c),
                "semantic_class": "UNCLASSIFIED",
            })
        init_support = max(1, int(init.get("replay_support", 0) or 0))
        for r in nearby:
            r["support_fraction_of_landmark"] = round(min(int(r["replay_support"]), init_support) / init_support, 4)
            r["support_gap_from_landmark"] = abs(init_support - int(r["replay_support"]))
        # Evidence rank: avoid a one-off player object winning merely because it is 0.2 units closer.
        # Prefer non-roster/system facts whose cross-replay support matches the explicit *Init landmark,
        # then use geometric distance as a tie-breaker.
        nearby.sort(key=lambda r: (
            1 if r.get("owner_class") == "ROSTER_PLAYER_CONTROLLED" else 0,
            r["support_gap_from_landmark"],
            -r["replay_support"],
            r["distance"],
            str(r["unit_type"]),
        ))
        for i, r in enumerate(nearby, 1):
            r["evidence_rank"] = i
        out.append({
            "event_name": init.get("event_name"),
            "x": init.get("x"),
            "y": init.get("y"),
            "replay_support": int(init.get("replay_support", 0) or 0),
            "support_tier": init.get("support_tier"),
            "builds": init.get("builds", []),
            "source_examples": init.get("source_examples", []),
            "join_radius": radius,
            "nearby_spawn_clusters": nearby,
            "semantic_class": "UNCLASSIFIED",
            "semantic_evidence_source": "REPLAY_STAT_EVENT_NAME",
        })
    return out


def _movement_observed_bounds(movement: list[dict], cell_size: float, replay_count: int) -> dict:
    """Bounds of cells actually visited by verified hero position anchors.

    Explicitly not a navmesh or map-boundary claim.
    """
    if not movement:
        return {"cell_size": cell_size, "all_observed": None, "cross_replay_observed": None, "cross_replay_min_support": None}
    half = float(cell_size) / 2.0
    def bounds(rows: list[dict]):
        if not rows:
            return None
        return {
            "min_x": min(float(r["center_x"]) - half for r in rows),
            "max_x": max(float(r["center_x"]) + half for r in rows),
            "min_y": min(float(r["center_y"]) - half for r in rows),
            "max_y": max(float(r["center_y"]) + half for r in rows),
            "cell_count": len(rows),
        }
    min_support = 1 if replay_count <= 1 else 2
    stable = [r for r in movement if int(r.get("replay_support", 0) or 0) >= min_support]
    return {
        "cell_size": cell_size,
        "all_observed": bounds(movement),
        "cross_replay_observed": bounds(stable),
        "cross_replay_min_support": min_support,
    }



def _semantic_landmarks(hero_spawn_regions: list[dict], landmark_links: list[dict]) -> list[dict]:
    """Promote only semantics directly supported by replay-native evidence.

    v0.2 intentionally does NOT infer Fort/Keep, camp type, boss, objective subtype,
    or lane identity. Those remain UNRESOLVED until independently proven.
    """
    out: list[dict] = []
    for r in hero_spawn_regions:
        row = dict(r)
        row.update({
            "semantic_class": "TEAM_SPAWN",
            "semantic_subtype": "UNRESOLVED",
            "semantic_evidence_source": "STABLE_HERO_SPAWN_REGION",
            "semantic_confidence": "SINGLE_REPLAY_EVIDENCE" if int(r.get("replay_support", 0) or 0) <= 1 else "REPLAY_PROVEN",
        })
        out.append(row)
    mapping = {
        "TownStructureInit": "TOWN_STRUCTURE",
        "JungleCampInit": "JUNGLE_CAMP",
    }
    for l in landmark_links:
        cls = mapping.get(str(l.get("event_name") or ""))
        if not cls:
            continue
        row = dict(l)
        row["semantic_class"] = cls
        row["semantic_subtype"] = "UNRESOLVED"
        row["semantic_confidence"] = "SINGLE_REPLAY_EVIDENCE" if int(l.get("replay_support", 0) or 0) <= 1 else "REPLAY_PROVEN"
        out.append(row)
    return sorted(out, key=lambda r: (str(r.get("semantic_class")), str(r.get("team") or ""), float(r.get("x", 0.0)), float(r.get("y", 0.0))))

def aggregate_map(map_id: str, facts: list[dict], cluster_radius: float, movement_cell_size: float, hero_spawn_region_radius: float = 12.0) -> dict:
    replay_count = len(facts)
    builds = Counter(str(f.get("metadata", {}).get("build")) for f in facts)
    map_names = Counter(str(f.get("metadata", {}).get("map_name") or "UNKNOWN") for f in facts)

    spawn_points: list[dict] = []
    stat_points: list[dict] = []
    hero_spawn_points: list[dict] = []
    movement_cells: dict[tuple[int, int], dict] = {}
    type_summary: dict[str, dict] = {}
    type_change_edges: dict[tuple[str, str], dict] = {}
    bounds_points: list[tuple[float, float]] = []

    for f in facts:
        m = f.get("metadata", {}) or {}; rid = m.get("source_id"); source = m.get("source"); build = m.get("build")
        b = (f.get("observed_event_bounds", {}) or {}).get("raw")
        if b:
            bounds_points.extend([(float(b["min_x"]), float(b["min_y"])), (float(b["max_x"]), float(b["max_y"]))])
        for p in f.get("unit_spawn_points", []) or []:
            spawn_points.append({**p, "replay_id": rid, "source": source, "build": build, "event_count": p.get("spawn_count", 1)})
            u = type_summary.setdefault(str(p["unit_type"]), {
                "unit_type": str(p["unit_type"]), "spawn_count": 0, "replay_ids": set(), "owner_classes": Counter(),
            })
            u["spawn_count"] += int(p.get("spawn_count", 0)); u["replay_ids"].add(str(rid)); u["owner_classes"][str(p.get("owner_class"))] += int(p.get("spawn_count", 0))
        for p in f.get("stat_spatial_points", []) or []:
            stat_points.append({**p, "replay_id": rid, "source": source, "build": build, "event_count": p.get("count", 1)})
        for p in f.get("hero_spawn_points", []) or []:
            hero_spawn_points.append({**p, "point_source": p.get("source"), "replay_id": rid, "replay_source": source, "build": build, "event_count": 1})
        seen_cells = set()
        for c in f.get("hero_movement_cells", []) or []:
            key = (int(c["cell_x"]), int(c["cell_y"]))
            row = movement_cells.setdefault(key, {
                "cell_x": key[0], "cell_y": key[1], "center_x": c["center_x"], "center_y": c["center_y"],
                "hits": 0, "replay_ids": set(),
            })
            row["hits"] += int(c.get("hits", 0)); seen_cells.add(key)
        for key in seen_cells:
            movement_cells[key]["replay_ids"].add(str(rid))
        for e in f.get("unit_type_change_edges", []) or []:
            key = (str(e["from_unit_type"]), str(e["to_unit_type"]))
            row = type_change_edges.setdefault(key, {"from_unit_type": key[0], "to_unit_type": key[1], "count": 0, "replay_ids": set()})
            row["count"] += int(e.get("count", 0)); row["replay_ids"].add(str(rid))

    spawn_clusters = _cluster_points(spawn_points, cluster_radius, identity_fields=("unit_type", "owner_class"))
    stat_clusters = _cluster_points(stat_points, cluster_radius, identity_fields=("event_name",))
    # Preserve detailed per-hero/per-source evidence, but expose coarse team spawn regions separately.
    hero_evidence_clusters = _cluster_points(hero_spawn_points, cluster_radius, identity_fields=("team", "hero_unit_id", "point_source"))
    hero_regions_raw = _cluster_points(hero_spawn_points, hero_spawn_region_radius, identity_fields=("team",))
    hero_regions = _stable_hero_spawn_regions(hero_regions_raw, replay_count, min_fraction=0.50)
    map_candidates = _map_object_candidates(spawn_clusters, replay_count)
    init_stat_clusters = [c for c in stat_clusters if str(c.get("event_name") or "").endswith("Init") and c.get("event_name") != "PlayerInit"]
    landmark_links = _replay_native_landmark_links(init_stat_clusters, spawn_clusters, join_radius=1.5)
    semantic_landmarks = _semantic_landmarks(hero_regions, landmark_links)

    unit_types = []
    for u in type_summary.values():
        unit_types.append({
            "unit_type": u["unit_type"], "spawn_count": u["spawn_count"],
            "replay_support": len(u["replay_ids"]), "support_tier": _support_tier(len(u["replay_ids"])),
            "owner_classes": dict(u["owner_classes"]),
        })
    unit_types.sort(key=lambda r: (-r["replay_support"], -r["spawn_count"], r["unit_type"]))

    movement = []
    for c in movement_cells.values():
        movement.append({
            "cell_x": c["cell_x"], "cell_y": c["cell_y"], "center_x": c["center_x"], "center_y": c["center_y"],
            "hits": c["hits"], "replay_support": len(c["replay_ids"]),
            "support_fraction": round(len(c["replay_ids"]) / replay_count, 4) if replay_count else 0.0,
        })
    movement.sort(key=lambda r: (-r["replay_support"], -r["hits"], r["cell_x"], r["cell_y"]))

    edges = []
    for e in type_change_edges.values():
        edges.append({**{k:v for k,v in e.items() if k != "replay_ids"}, "replay_support": len(e["replay_ids"])})
    edges.sort(key=lambda r: (-r["replay_support"], -r["count"], r["from_unit_type"], r["to_unit_type"]))

    recurring = sum(1 for c in spawn_clusters if c["replay_support"] >= 2)
    high_support = sum(1 for c in spawn_clusters if c["replay_support"] >= 5)
    return {
        "schema": {"name": "PYHOTS_CANONICAL_MAP_FACTS", "version": VERSION},
        "map_id": map_id,
        "map_name_observations": dict(map_names),
        "replay_support": replay_count,
        "support_tier": _support_tier(replay_count),
        "builds": dict(builds),
        "observed_event_bounds": _bounds(bounds_points),
        "hero_movement_observed_bounds": _movement_observed_bounds(movement, movement_cell_size, replay_count),
        "unit_types": unit_types,
        "unit_spawn_clusters": spawn_clusters,
        "map_object_candidates": map_candidates,
        "stat_spatial_clusters": stat_clusters,
        "map_init_stat_clusters": init_stat_clusters,
        "replay_native_landmark_links": landmark_links,
        "semantic_landmarks": semantic_landmarks,
        "hero_spawn_evidence_clusters": hero_evidence_clusters,
        "hero_spawn_region_evidence": hero_regions_raw,
        "hero_spawn_regions": hero_regions,
        # Backward-compatible alias; consumers should prefer hero_spawn_regions.
        "hero_spawn_clusters": hero_regions,
        "hero_movement_grid": {"cell_size": movement_cell_size, "cells": movement},
        "unit_type_change_edges": edges,
        "semantics": {"status": "PARTIALLY_CLASSIFIED_REPLAY_PROVEN", "classes": ["TEAM_SPAWN", "TOWN_STRUCTURE", "JUNGLE_CAMP"], "subtypes": "UNRESOLVED", "note": "v0.2 promotes only replay-proven top-level classes. Fort/Keep, camp type/boss, objectives and lanes are not inferred."},
        "integrity": {
            "spawn_clusters_total": len(spawn_clusters),
            "spawn_clusters_replay_support_2_plus": recurring,
            "spawn_clusters_replay_support_5_plus": high_support,
            "map_object_candidates": len(map_candidates),
            "roster_player_generated_clusters": sum(1 for c in spawn_clusters if c.get("owner_class") == "ROSTER_PLAYER_CONTROLLED"),
            "hero_spawn_regions": len(hero_regions),
            "hero_spawn_region_evidence": len(hero_regions_raw),
            "hero_spawn_evidence_clusters": len(hero_evidence_clusters),
            "map_init_stat_clusters": len(init_stat_clusters),
            "replay_native_landmark_links": len(landmark_links),
            "semantic_landmarks": len(semantic_landmarks),
            "single_replay_warning": replay_count == 1,
        },
    }


def _fingerprint(path: Path, config_sig: str) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{config_sig}|{CACHE_SCHEMA}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


_MD_SCHEMA: Optional[rp.AbilitySchemaIndex] = None
_MD_HD_CMD: list[str] = []
_MD_TIMEOUT: float = 180.0
_MD_KEEP_DIR: Optional[Path] = None
_MD_COORD_PRECISION: float = 0.25
_MD_MOVEMENT_CELL: float = 4.0


def _init_worker(schema_path_s: str, hd_cmd: list[str], timeout: float, keep_dir_s: Optional[str], coord_precision: float, movement_cell: float) -> None:
    global _MD_SCHEMA, _MD_HD_CMD, _MD_TIMEOUT, _MD_KEEP_DIR, _MD_COORD_PRECISION, _MD_MOVEMENT_CELL
    _MD_SCHEMA = rp.AbilitySchemaIndex(load_json(Path(schema_path_s)))
    _MD_HD_CMD = list(hd_cmd or []); _MD_TIMEOUT = float(timeout)
    _MD_KEEP_DIR = Path(keep_dir_s) if keep_dir_s else None
    _MD_COORD_PRECISION = float(coord_precision); _MD_MOVEMENT_CELL = float(movement_cell)


def _worker(path_s: str) -> tuple[str, dict]:
    path = Path(path_s)
    replay = ws._load_replay(path, _MD_HD_CMD, _MD_TIMEOUT, _MD_KEEP_DIR)
    facts = extract_map_facts(replay, path.name, _MD_SCHEMA, coord_precision=_MD_COORD_PRECISION, movement_cell_size=_MD_MOVEMENT_CELL)
    return path_s, facts


def discover_inputs(explicit: list[str], dirs: list[str], recursive: bool, output_dir: Path) -> list[Path]:
    return ws.discover_inputs(explicit, dirs, recursive, output_dir)


def main() -> int:
    ap = argparse.ArgumentParser(description="Pyhots Map DB Builder v0.2 — replay-proven semantic landmarks")
    ap.add_argument("--replay", action="append", default=[])
    ap.add_argument("--replay-dir", action="append", default=[])
    ap.add_argument("--no-recursive", action="store_true")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--abilities-complete", required=True, help="Pyhots Ability DB used only for the frozen World State hero-identity bridge")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--heroesdecode-path")
    ap.add_argument("--heroesdecode-timeout", type=float, default=180.0)
    ap.add_argument("--keep-decoded-json", action="store_true")
    ap.add_argument("--coord-precision", type=float, default=0.25, help="Within-replay coordinate quantization for identical spawn facts")
    ap.add_argument("--cluster-radius", type=float, default=1.25, help="Cross-replay radius for the same unit/event spatial cluster")
    ap.add_argument("--movement-cell-size", type=float, default=4.0, help="Grid size for replay-derived hero movement density")
    ap.add_argument("--hero-spawn-region-radius", type=float, default=12.0, help="Coarse team spawn-region clustering radius; spatial aggregation only")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.output_dir).expanduser().resolve(); outdir.mkdir(parents=True, exist_ok=True)
    cache_dir = outdir / ".map_facts_cache"; cache_dir.mkdir(exist_ok=True)
    replay_out = outdir / "replay_facts"; replay_out.mkdir(exist_ok=True)
    maps_out = outdir / "maps"; maps_out.mkdir(exist_ok=True)
    keep_dir = outdir / "decoded_replays" if args.keep_decoded_json else None
    if keep_dir: keep_dir.mkdir(exist_ok=True)

    full = Path(args.abilities_complete).expanduser().resolve()
    if not full.exists(): raise SystemExit(f"Ability DB not found: {full}")
    sha = rp.file_sha256(full)
    schema_path = cache_dir / f"ability_identity_{sha[:16]}.json"
    if not schema_path.exists():
        dump_json(schema_path, rp.compact_ability_schema(load_json(full), source_sha256=sha))

    inputs = discover_inputs(args.replay, args.replay_dir, not args.no_recursive, outdir)
    if not inputs: raise SystemExit("No .StormReplay or decoded replay JSON inputs found")
    need_hd = any(p.suffix.lower() == ".stormreplay" for p in inputs)
    hd_cmd: list[str] = []; hd_version = None
    if need_hd:
        hd_cmd = rp.resolve_heroesdecode_command(args.heroesdecode_path)
        ok, hd_version = rp.check_heroesdecode_command(hd_cmd)
        if not ok: raise SystemExit(f"HeroesDecode unavailable: {hd_version}")

    config_sig = hashlib.sha256(orjson.dumps({
        "v": CACHE_CONFIG_VERSION, "ability_sha": sha, "heroesdecode": hd_version,
        "coord_precision": args.coord_precision, "movement_cell": args.movement_cell_size,
    })).hexdigest()

    cached = 0; todo = []; facts_all: list[dict] = []; failures = []
    for p in inputs:
        key = _fingerprint(p, config_sig); cp = cache_dir / f"{key}.json"
        if cp.exists() and not args.force:
            try:
                facts_all.append(load_json(cp)); cached += 1; continue
            except Exception:
                pass
        todo.append((p, cp))

    print(f"Pyhots Map DB Builder v{VERSION}")
    print(f"Discovered: {len(inputs)} | cached: {cached} | to process: {len(todo)} | workers: {max(1,args.workers)}")
    if hd_version: print(f"HeroesDecode: {hd_version}")
    t0 = time.time()
    tasks = [str(p) for p, _ in todo]; path_to_cache = {str(p): cp for p, cp in todo}
    if tasks:
        with cf.ProcessPoolExecutor(
            max_workers=max(1, args.workers), initializer=_init_worker,
            initargs=(str(schema_path), hd_cmd, args.heroesdecode_timeout, str(keep_dir) if keep_dir else None, args.coord_precision, args.movement_cell_size),
        ) as ex:
            futs = {ex.submit(_worker, ps): ps for ps in tasks}
            for fut in cf.as_completed(futs):
                ps = futs[fut]
                try:
                    _, facts = fut.result(); dump_json(path_to_cache[ps], facts); facts_all.append(facts)
                except Exception as exc:
                    failures.append({"file": ps, "error": f"{type(exc).__name__}: {exc}"})

    # Materialize deterministic per-replay fact shards.
    for f in facts_all:
        rid = (f.get("metadata", {}) or {}).get("source_id") or hashlib.sha256(str(f.get("metadata", {})).encode()).hexdigest()[:16]
        dump_json(replay_out / f"{rid}.map_facts.json", f)

    core = [f for f in facts_all if (f.get("metadata", {}) or {}).get("replay_scope") == "CORE_10_PLAYER"]
    quarantine = [f for f in facts_all if f not in core]
    by_map: dict[str, list[dict]] = defaultdict(list)
    for f in core:
        mid = str((f.get("metadata", {}) or {}).get("map_id") or "UNKNOWN")
        by_map[mid].append(f)

    maps = {}
    for mid, rows in sorted(by_map.items()):
        m = aggregate_map(mid, rows, args.cluster_radius, args.movement_cell_size, args.hero_spawn_region_radius)
        maps[mid] = m; dump_json(maps_out / f"{mid}.json", m)

    q_reasons = Counter()
    for f in quarantine:
        for r in (f.get("metadata", {}) or {}).get("quarantine_reasons", []) or ["UNKNOWN"]:
            q_reasons[str(r)] += 1

    gates = {
        "PROCESSING": "PASS" if not failures and len(facts_all) == len(inputs) else "FAIL",
        "QUARANTINE_ACCOUNTING": "PASS" if len(core) + len(quarantine) == len(facts_all) else "FAIL",
        "CORE_WORLD_STATE": "PASS" if core and all(not (f.get("metadata", {}) or {}).get("quarantine_reasons") for f in core) else "FAIL",
        "MAP_ID": "PASS" if core and all((f.get("metadata", {}) or {}).get("map_id") for f in core) else "FAIL",
        "BUILD_PROVENANCE": "PASS" if core and all((f.get("metadata", {}) or {}).get("build") is not None for f in core) else "FAIL",
        "MAP_OUTPUT": "PASS" if len(maps) == len(by_map) and all(m.get("replay_support") == len(by_map[mid]) for mid, m in maps.items()) else "FAIL",
        "CLUSTER_PROVENANCE": "PASS" if all(all(int(c.get("replay_support",0)) >= 1 and int(c.get("event_count",0)) >= 1 for c in m.get("unit_spawn_clusters", [])) for m in maps.values()) else "FAIL",
        "MAP_CANDIDATE_SEPARATION": "PASS" if all(all(c.get("owner_class") != "ROSTER_PLAYER_CONTROLLED" and int(c.get("replay_support",0)) >= (1 if int(m.get("replay_support",0)) <= 1 else 2) for c in m.get("map_object_candidates", [])) for m in maps.values()) else "FAIL",
        "HERO_SPAWN_REGION_PROVENANCE": "PASS" if all(
            m.get("hero_spawn_regions") and
            {str(c.get("team")) for c in m.get("hero_spawn_regions", [])} >= {"blue", "red"} and
            all(int(c.get("replay_support",0)) >= (1 if int(m.get("replay_support",0)) <= 1 else max(2, int(math.ceil(int(m.get("replay_support",0)) * 0.50)))) for c in m.get("hero_spawn_regions", []))
            for m in maps.values()
        ) else "FAIL",
        "REPLAY_NATIVE_LANDMARK_JOIN": "PASS" if all(
            len(m.get("replay_native_landmark_links", [])) == len(m.get("map_init_stat_clusters", [])) and
            all(c.get("semantic_evidence_source") == "REPLAY_STAT_EVENT_NAME" for c in m.get("replay_native_landmark_links", []))
            for m in maps.values()
        ) else "FAIL",
        "RAW_CANDIDATES_FAIL_CLOSED": "PASS" if all(all(c.get("semantic_class") == "UNCLASSIFIED" for c in m.get("map_object_candidates", [])) for m in maps.values()) else "FAIL",
        "REPLAY_PROVEN_SEMANTICS": "PASS" if all(
            (m.get("semantics", {}) or {}).get("status") == "PARTIALLY_CLASSIFIED_REPLAY_PROVEN" and
            all(c.get("semantic_class") in {"TEAM_SPAWN", "TOWN_STRUCTURE", "JUNGLE_CAMP"} for c in m.get("semantic_landmarks", [])) and
            sum(1 for c in m.get("semantic_landmarks", []) if c.get("semantic_class") == "TEAM_SPAWN") == len(m.get("hero_spawn_regions", [])) and
            sum(1 for c in m.get("semantic_landmarks", []) if c.get("semantic_class") in {"TOWN_STRUCTURE", "JUNGLE_CAMP"}) == len(m.get("replay_native_landmark_links", []))
            for m in maps.values()
        ) else "FAIL",
        "SEMANTIC_SUBTYPES_FAIL_CLOSED": "PASS" if all(all(c.get("semantic_subtype") == "UNRESOLVED" for c in m.get("semantic_landmarks", [])) for m in maps.values()) else "FAIL",
    }
    integrity = {"gates": gates, "map_db_integrity": "PASS" if all(v == "PASS" for v in gates.values()) else "FAIL"}

    summary = {
        "schema": {"name": "PYHOTS_MAP_DB_SUMMARY", "version": VERSION},
        "replays_discovered": len(inputs), "replays_processed": len(facts_all), "failed": len(failures), "cached": cached,
        "core_replays": len(core), "quarantined_replays": len(quarantine), "quarantine_reason_counts": dict(q_reasons),
        "maps": {mid: len(rows) for mid, rows in sorted(by_map.items())},
        "map_count": len(maps),
        "total_unit_spawn_clusters": sum(len(m.get("unit_spawn_clusters", [])) for m in maps.values()),
        "total_recurring_spawn_clusters": sum(sum(1 for c in m.get("unit_spawn_clusters", []) if c.get("replay_support",0) >= 2) for m in maps.values()),
        "total_map_object_candidates": sum(len(m.get("map_object_candidates", [])) for m in maps.values()),
        "total_roster_player_generated_clusters": sum(sum(1 for c in m.get("unit_spawn_clusters", []) if c.get("owner_class") == "ROSTER_PLAYER_CONTROLLED") for m in maps.values()),
        "total_hero_spawn_regions": sum(len(m.get("hero_spawn_regions", [])) for m in maps.values()),
        "total_hero_spawn_region_evidence": sum(len(m.get("hero_spawn_region_evidence", [])) for m in maps.values()),
        "total_map_init_stat_clusters": sum(len(m.get("map_init_stat_clusters", [])) for m in maps.values()),
        "total_replay_native_landmark_links": sum(len(m.get("replay_native_landmark_links", [])) for m in maps.values()),
        "total_semantic_landmarks": sum(len(m.get("semantic_landmarks", [])) for m in maps.values()),
        "semantic_class_counts": dict(Counter(c.get("semantic_class") for m in maps.values() for c in m.get("semantic_landmarks", []))),
        "elapsed_seconds": round(time.time()-t0, 3), "heroesdecode_version": hd_version,
        "integrity": integrity,
    }
    db = {
        "schema": {"name": "PYHOTS_MAP_DB", "version": VERSION},
        "contract": {
            "fact_layer": "REPLAY_DERIVED",
            "semantic_labels": "REPLAY_PROVEN_TOP_LEVEL_ONLY",
            "semantic_subtypes": "UNRESOLVED",
            "cluster_radius": args.cluster_radius,
            "coord_precision": args.coord_precision,
            "movement_cell_size": args.movement_cell_size,
            "hero_spawn_region_radius": args.hero_spawn_region_radius,
        },
        "summary": summary,
        "maps": maps,
    }
    dump_json(outdir / "map_db.json", db)
    dump_json(outdir / "summary.json", summary)
    dump_json(outdir / "integrity_report.json", integrity)
    dump_json(outdir / "quarantined_replays.json", [{"metadata": f.get("metadata", {})} for f in quarantine])
    dump_json(outdir / "failed_replays.json", failures)

    print(orjson.dumps(summary, option=orjson.OPT_INDENT_2).decode())
    return 0 if not failures and integrity["map_db_integrity"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
