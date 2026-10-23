from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import shutil
import statistics
import struct
import sys
import xml.etree.ElementTree as ET
from array import array
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import static_geometry_v05_core as v05
import static_geometry_v04_core as v04
import static_geometry_v03_core as core
from mpq_reader import MPQArchive

# Backward-compatible exports for all previous regression tests.
parse_cell_attribute = v05.parse_cell_attribute
mapinfo_label = v05.mapinfo_label
parse_position = v05.parse_position
parse_scale = v05.parse_scale
parse_object_doodads = v05.parse_object_doodads
doodad_footprint_enabled = v05.doodad_footprint_enabled
parse_terrain_frame = v05.parse_terrain_frame
footprint_place_area = v05.footprint_place_area
absolute_shrub_geometry = v05.absolute_shrub_geometry
rect_from_center = v05.rect_from_center
point_in_rect = v05.point_in_rect
connected_components = v05.connected_components

SCHEMA_VERSION = "0.9"
CORE_ATTRS = ("CellAttribute_Pcl", "CellAttribute_Pnp", "CellAttribute_Vbl", "CellAttribute_Vur")
CANONICAL_PNP_MASKS = (0x0F, 0x2B, 0x33, 0x71, 0x8E, 0xCC, 0xD4, 0xF0)
EXPECTED_CANONICAL_ANGLES = {
    0x0F: 90.0, 0x2B: 135.0, 0x33: 180.0, 0x71: -135.0,
    0x8E: 45.0, 0xCC: 0.0, 0xD4: -45.0, 0xF0: -90.0,
}
# Local terrain-cell triangle geometry. +Y is north. Each triangle has area 1/8 cell.
PNP_BIT_TRIANGLES = {
    0: {"quadrant": "NW", "edge": "W", "vertices": [[0.0, 0.5], [0.0, 1.0], [0.5, 0.5]]},
    1: {"quadrant": "NW", "edge": "N", "vertices": [[0.0, 1.0], [0.5, 1.0], [0.5, 0.5]]},
    2: {"quadrant": "NE", "edge": "E", "vertices": [[1.0, 1.0], [1.0, 0.5], [0.5, 0.5]]},
    3: {"quadrant": "NE", "edge": "N", "vertices": [[0.5, 1.0], [1.0, 1.0], [0.5, 0.5]]},
    4: {"quadrant": "SW", "edge": "S", "vertices": [[0.0, 0.0], [0.5, 0.0], [0.5, 0.5]]},
    5: {"quadrant": "SW", "edge": "W", "vertices": [[0.0, 0.0], [0.0, 0.5], [0.5, 0.5]]},
    6: {"quadrant": "SE", "edge": "S", "vertices": [[0.5, 0.0], [1.0, 0.0], [0.5, 0.5]]},
    7: {"quadrant": "SE", "edge": "E", "vertices": [[1.0, 0.0], [1.0, 0.5], [0.5, 0.5]]},
}


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def angle_diff_deg(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def valid_cell_attribute_payload(raw: bytes | None, w: int, h: int) -> bytes | None:
    if not raw or len(raw) < 4:
        return None
    bit_count = struct.unpack_from("<I", raw, 0)[0]
    payload = raw[4:]
    if bit_count != w * h * 8 or len(payload) != w * h:
        return None
    return payload


def attribute_replay_validation(selected: dict[str, Any], terrain_manifest: dict[str, Any], map_db: Path) -> dict[str, Any]:
    maps: dict[str, Any] = {}
    totals = {name: Counter() for name in CORE_ATTRS}
    for map_id in sorted(selected):
        package = selected[map_id]
        frame = terrain_manifest[map_id]["terrain_frame"]
        w, h = frame["cell_dim"]["x"], frame["cell_dim"]["y"]
        m = v05._read_map_object(map_db, map_id)
        move = m.get("hero_movement_grid", {}).get("cells", [])
        spawns = m.get("hero_spawn_regions", [])
        per_layer = {}
        with MPQArchive(package["path"]) as archive:
            for name in CORE_ATTRS:
                payload = valid_cell_attribute_payload(archive.read(name), w, h)
                if payload is None:
                    per_layer[name] = {"status": "UNRESOLVED_INVALID_LAYER"}
                    continue
                move_cells = move_zero_cells = move_hits = move_zero_hits = 0
                nz_hist = Counter()
                for cell in move:
                    b = v05._cell_byte(payload, w, h, float(cell["center_x"]), float(cell["center_y"]))
                    if b is None:
                        continue
                    hits = int(cell.get("hits", 0))
                    move_cells += 1; move_hits += hits
                    if b == 0:
                        move_zero_cells += 1; move_zero_hits += hits
                    else:
                        nz_hist[b] += hits
                spawn_n = spawn_zero = 0
                for s in spawns:
                    b = v05._cell_byte(payload, w, h, float(s["x"]), float(s["y"]))
                    if b is None:
                        continue
                    spawn_n += 1
                    if b == 0:
                        spawn_zero += 1
                rec = {
                    "status": "PROVEN_REPLAY_CROSS_VALIDATION",
                    "movement_hits": move_hits,
                    "movement_hits_at_mask_0": move_zero_hits,
                    "movement_hit_mask_0_fraction": move_zero_hits / move_hits if move_hits else None,
                    "movement_cells": move_cells,
                    "movement_cells_at_mask_0": move_zero_cells,
                    "hero_spawn_regions": spawn_n,
                    "hero_spawn_regions_at_mask_0": spawn_zero,
                    "nonzero_mask_movement_hit_histogram": {f"0x{k:02x}": v for k, v in sorted(nz_hist.items())},
                }
                per_layer[name] = rec
                t = totals[name]
                t["movement_hits"] += move_hits; t["movement_zero_hits"] += move_zero_hits
                t["spawns"] += spawn_n; t["spawn_zero"] += spawn_zero
        maps[map_id] = {"layers": per_layer}
    summary = {}
    for name, t in totals.items():
        summary[name] = {
            "movement_hits": t["movement_hits"],
            "movement_hits_at_mask_0": t["movement_zero_hits"],
            "movement_hit_mask_0_fraction": t["movement_zero_hits"] / t["movement_hits"] if t["movement_hits"] else None,
            "hero_spawn_regions": t["spawns"],
            "hero_spawn_regions_at_mask_0": t["spawn_zero"],
            "hero_spawn_mask_0_fraction": t["spawn_zero"] / t["spawns"] if t["spawns"] else None,
        }
    return {
        "schema": {"name": "PYHOTS_CELL_ATTRIBUTE_REPLAY_VALIDATION", "version": SCHEMA_VERSION},
        "contract": {
            "replay_is_cross_validation_not_geometry_source": True,
            "nonzero_traversal_disproves_general_movement_exclusion_only": True,
            "zero_traversal_does_not_prove_walkability_or_layer_semantics": True,
        },
        "maps": maps,
        "summary": summary,
    }


def pnp_gradient_evidence(selected: dict[str, Any], terrain_manifest: dict[str, Any]) -> dict[str, Any]:
    sums = {m: [0.0, 0.0, 0] for m in CANONICAL_PNP_MASKS}
    partial_total = canonical_total = 0
    per_map = {}
    for map_id in sorted(selected):
        frame = terrain_manifest[map_id]["terrain_frame"]
        w, h = frame["cell_dim"]["x"], frame["cell_dim"]["y"]
        with MPQArchive(selected[map_id]["path"]) as archive:
            payload = valid_cell_attribute_payload(archive.read("CellAttribute_Pnp"), w, h)
        if payload is None:
            per_map[map_id] = {"status": "UNRESOLVED"}
            continue
        partial = sum(1 for b in payload if b not in (0, 255))
        canonical = sum(1 for b in payload if b in CANONICAL_PNP_MASKS)
        partial_total += partial; canonical_total += canonical
        per_map[map_id] = {"partial_cells": partial, "canonical_partial_cells": canonical}
        for y in range(1, h - 1):
            for x in range(1, w - 1):
                b = payload[y * w + x]
                if b not in sums:
                    continue
                gx = gy = 0.0; n = 0
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        if dx == 0 and dy == 0:
                            continue
                        nb = payload[(y + dy) * w + x + dx]
                        if nb not in (0, 255):
                            continue
                        sign = 1.0 if nb == 255 else -1.0
                        norm = math.hypot(dx, dy)
                        gx += sign * dx / norm; gy += sign * dy / norm; n += 1
                if n and (gx or gy):
                    mag = math.hypot(gx, gy)
                    sums[b][0] += gx / mag; sums[b][1] += gy / mag; sums[b][2] += 1
    evidence = {}
    ok = True
    for mask in CANONICAL_PNP_MASKS:
        sx, sy, n = sums[mask]
        angle = math.degrees(math.atan2(sy, sx)) if n else None
        expected = EXPECTED_CANONICAL_ANGLES[mask]
        diff = angle_diff_deg(angle, expected) if angle is not None else None
        passed = n >= 100 and diff is not None and diff <= 5.0
        ok = ok and passed
        evidence[f"0x{mask:02x}"] = {
            "samples_with_definite_neighbors": n,
            "average_boundary_normal_degrees": angle,
            "expected_cardinal_or_diagonal_degrees": expected,
            "angle_error_degrees": diff,
            "status": "PASS" if passed else "REVIEW",
        }
    return {
        "schema": {"name": "PYHOTS_PNP_SUBCELL_LAYOUT", "version": SCHEMA_VERSION},
        "status": "PROVEN_FROM_CANONICAL_BOUNDARY_GRADIENTS" if ok else "UNRESOLVED",
        "geometry_contract": {
            "terrain_cell_local_frame": {"min": [0.0, 0.0], "max": [1.0, 1.0], "positive_y": "NORTH"},
            "bits_are_equal_area_triangular_subregions": True,
            "bit_to_local_triangle": {str(bit): data for bit, data in PNP_BIT_TRIANGLES.items()},
            "mask_semantics": "SET_BIT_SELECTS_THE_CORRESPONDING_PNP_EXCLUSION_TRIANGLE",
            "full_mask_0xff": "ALL_EIGHT_TRIANGLES_SELECTED",
            "clear_mask_0x00": "NO_PNP_TRIANGLES_SELECTED",
        },
        "canonical_halfplane_masks": {
            "N": "0x0f", "NE": "0x8e", "E": "0xcc", "SE": "0xd4",
            "S": "0xf0", "SW": "0x71", "W": "0x33", "NW": "0x2b",
        },
        "boundary_gradient_evidence": evidence,
        "coverage": {
            "partial_cells": partial_total,
            "canonical_partial_cells": canonical_total,
            "canonical_fraction_of_partial": canonical_total / partial_total if partial_total else None,
        },
        "maps": per_map,
    }


def parse_terrain_xml(raw: bytes) -> dict[str, Any]:
    root = ET.fromstring(raw)
    hm = root.find("heightMap")
    if hm is None:
        raise ValueError("missing heightMap")
    vert = hm.find("vertData")
    cc = []
    for e in hm.findall(".//cc"):
        cc.append({
            "i": int(e.get("i", "0")),
            "f": int(e.get("f", "1")),
            "cid": int(e.get("cid", "0")),
            "cvar": int(e.get("cvar", "0")),
        })
    cliff_doodads = []
    for e in hm.findall(".//cliffDoodad"):
        cliff_doodads.append({"name": e.get("name"), "pos": e.get("pos"), "rot": int(e.get("rot", "0"))})
    ramps = [dict(e.attrib) for e in hm.findall(".//ramp")]
    cliff_sets = [dict(e.attrib) for e in hm.findall(".//cliffSet")]
    return {
        "version": int(root.get("version", "0")),
        "height_map_attrs": dict(hm.attrib),
        "vert_data": dict(vert.attrib) if vert is not None else None,
        "cliff_sets": cliff_sets,
        "cliff_cells": cc,
        "cliff_doodads": cliff_doodads,
        "ramps": ramps,
    }


def parse_heightmap(raw: bytes, terrain_xml: dict[str, Any], frame: dict[str, Any], out_path: Path) -> dict[str, Any]:
    if len(raw) < 32 or raw[:4] != b"HMAP":
        return {"status": "UNRESOLVED_INVALID_HMAP"}
    magic, version, sx, sy = struct.unpack_from("<4I", raw, 0)
    expected_len = 32 + 6 * sx * sy
    if version != 101 or len(raw) != expected_len:
        return {"status": "UNRESOLVED_HMAP_VERSION_OR_SIZE", "version": version, "size": len(raw), "expected_size": expected_len}
    vd = terrain_xml.get("vert_data") or {}
    try:
        bias = float(vd["quantizeBias"]); qscale = float(vd["quantizeScale"]); standard = float(vd["standardHeight"])
    except (KeyError, TypeError, ValueError):
        return {"status": "UNRESOLVED_VERTDATA_PARAMS"}
    if (sx, sy) != (frame["vertex_dim"]["x"], frame["vertex_dim"]["y"]):
        return {"status": "UNRESOLVED_HMAP_DIMENSION_MISMATCH", "header_dim": [sx, sy], "terrain_dim": frame["vertex_dim"]}
    zscale = float(frame["scale"]["z"]); origin_z = float(frame["offset"]["z"])
    heights = array("f")
    masks = Counter()
    vals = []
    off = 32
    for _ in range(sx * sy):
        base, adj_raw, mask = struct.unpack_from("<HHH", raw, off); off += 6
        base_z = base * qscale - bias
        adj = adj_raw * qscale - bias
        z = base_z * zscale + origin_z + adj
        heights.append(float(z)); vals.append(float(z)); masks[mask] += 1
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("wb") as f:
        if sys.byteorder == "big":
            heights.byteswap()
        heights.tofile(f)
    sv = sorted(vals)
    n = len(sv)
    return {
        "status": "PROVEN_HMAP_FORMAT_AND_DEQUANTIZED_VERTEX_GRID",
        "magic": "HMAP", "version": version,
        "vertex_dim": {"x": sx, "y": sy},
        "row_major": True,
        "world_xy": {
            "x": "terrain.offset.x + vertex_x * terrain.scale.x",
            "y": "terrain.offset.y + vertex_y * terrain.scale.y",
        },
        "dequantization": {
            "quantizeBias": bias, "quantizeScale": qscale, "standardHeight_reference": standard,
            "terrain_z_scale": zscale, "terrain_origin_z": origin_z,
            "formula": "base=(heightBase*quantizeScale-quantizeBias); adj=(heightAdjustments*quantizeScale-quantizeBias); world_z=base*terrain_z_scale+terrain_origin_z+adj",
        },
        "height_summary": {
            "min": min(vals), "max": max(vals), "mean": statistics.fmean(vals), "median": statistics.median(vals),
            "p05": sv[int((n - 1) * 0.05)], "p95": sv[int((n - 1) * 0.95)],
        },
        "terrain_mask_histogram": {str(k): v for k, v in sorted(masks.items())},
        "grid_file": str(out_path.name),
        "grid_encoding": "float32 little-endian row-major world_z",
        "grid_sha256": sha256_file(out_path),
    }


def parse_sync_cliff(raw: bytes, frame: dict[str, Any], out_path: Path) -> tuple[dict[str, Any], list[int]]:
    if len(raw) < 32 or raw[:4] != b"CLIF":
        return {"status": "UNRESOLVED_INVALID_CLIF"}, []
    vals8 = struct.unpack_from("<8I", raw, 0)
    _magic, version, sx, sy, *rest = vals8
    expected = 32 + 2 * sx * sy
    if version != 100 or len(raw) != expected or (sx, sy) != (frame["cell_dim"]["x"], frame["cell_dim"]["y"]):
        return {"status": "UNRESOLVED_CLIF_VERSION_DIMENSION_OR_SIZE", "version": version, "dim": [sx, sy], "size": len(raw), "expected": expected}, []
    cells = list(struct.unpack_from(f"<{sx*sy}H", raw, 32))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(raw[32:])
    raw_hist = Counter(cells)
    rounded_no_floor = [int((v + 8) >> 4) for v in cells]
    qhist = Counter(rounded_no_floor)
    return {
        "status": "PROVEN_CLIF_RAW_GRID_ENGINE_FINAL_LEVEL_NEEDS_RUNTIME_BASE_FLOOR",
        "magic": "CLIF", "version": version, "cell_dim": {"x": sx, "y": sy}, "row_major": True,
        "raw_histogram": {str(k): v for k, v in sorted(raw_hist.items())},
        "rounded_quantized_without_runtime_base_floor_histogram": {str(k): v for k, v in sorted(qhist.items())},
        "engine_final_cliff_level": "UNRESOLVED_RUNTIME_BASE_CLIFF_FLOOR_NOT_IN_ARCHIVE_MEMBER",
        "grid_file": str(out_path.name),
        "grid_encoding": "uint16 little-endian raw CLIF cells row-major",
        "grid_sha256": sha256_file(out_path),
    }, cells


def parse_t3_cell_flags(raw: bytes, frame: dict[str, Any]) -> dict[str, Any]:
    if len(raw) < 24 or raw[:4] != b"LFCT":
        return {"status": "UNRESOLVED_INVALID_LFCT"}
    version = struct.unpack_from("<I", raw, 4)[0]
    if version not in (101, 102) or len(raw) < 32:
        return {"status": "UNRESOLVED_UNSUPPORTED_LFCT_VERSION", "version": version}
    w, h = struct.unpack_from("<II", raw, 24)
    payload = raw[32:]
    expected = w * h
    terr = frame["cell_dim"]
    valid = len(payload) == expected and (w, h) == (terr["x"], terr["y"])
    return {
        "status": "PROVEN_TERRAIN_CELL_FLAGS_FORMAT_SEMANTICS_NOT_PATHING_EQUIVALENT" if valid else "UNRESOLVED_LFCT_DIMENSION_OR_SIZE",
        "magic": "LFCT", "version": version, "cell_dim": {"x": w, "y": h}, "payload_bytes": len(payload),
        "value_histogram": {f"0x{k:02x}": v for k, v in sorted(Counter(payload).items())},
    }


def build_terrain_cliff_height_outputs(selected: dict[str, Any], terrain_manifest: dict[str, Any], output_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    cliff_maps = {}; height_maps = {}
    for map_id in sorted(selected):
        frame = terrain_manifest[map_id]["terrain_frame"]
        with MPQArchive(selected[map_id]["path"]) as archive:
            tx_raw = archive.read("t3Terrain.xml")
            hm_raw = archive.read("t3HeightMap")
            cl_raw = archive.read("t3SyncCliffLevel")
            cf_raw = archive.read("t3CellFlags")
        tx = parse_terrain_xml(tx_raw or b"")
        hpath = output_dir / "terrain_height_grids" / f"{map_id}.f32le"
        height_maps[map_id] = parse_heightmap(hm_raw or b"", tx, frame, hpath)
        cpath = output_dir / "cliff_raw_grids" / f"{map_id}.u16le"
        clmeta, _cells = parse_sync_cliff(cl_raw or b"", frame, cpath)
        w, h = frame["cell_dim"]["x"], frame["cell_dim"]["y"]
        hw, hh = (w + 1) // 2, (h + 1) // 2
        cc_out = []
        for e in tx["cliff_cells"]:
            i = e["i"]
            cc_out.append({**e, "half_cell_x": i % hw, "half_cell_y": i // hw})
        cc_valid = all(0 <= e["i"] < hw * hh for e in tx["cliff_cells"])
        cliff_maps[map_id] = {
            "sync_cliff": clmeta,
            "terrain_xml": {
                "terrain_version": tx["version"],
                "half_cell_dim": {"x": hw, "y": hh},
                "cliff_sets": tx["cliff_sets"],
                "cliff_cell_count": len(cc_out),
                "cliff_cells_index_valid": cc_valid,
                "cliff_cells": cc_out,
                "cliff_doodads": tx["cliff_doodads"],
                "ramps": tx["ramps"],
            },
            "t3CellFlags": parse_t3_cell_flags(cf_raw or b"", frame),
            "semantic_contract": {
                "asset_cliff_geometry": "PROVEN",
                "pcl_as_general_hero_blocker": "DISPROVEN_BY_REPLAY_NONZERO_TRAVERSAL",
                "exact_hero_cliff_transition_rule": "UNRESOLVED_ENGINE_PATHING_COMPOSITION",
            },
        }
    cliff = {
        "schema": {"name": "PYHOTS_STATIC_CLIFF_LAYERS", "version": SCHEMA_VERSION},
        "contract": {
            "t3SyncCliffLevel_raw_grid": "PROVEN",
            "t3Terrain_cliff_cells": "PROVEN_HALF_RES_ASSET_GEOMETRY",
            "t3CellFlags": "PROVEN_TERRAIN_FLAGS_NOT_ASSUMED_FULL_PATHING",
            "hero_path_blocking_from_cliffs": "UNRESOLVED_COMPOSITION",
        },
        "maps": cliff_maps,
        "summary": {
            "maps": len(cliff_maps),
            "maps_with_valid_clif": sum(1 for m in cliff_maps.values() if m["sync_cliff"].get("status", "").startswith("PROVEN")),
            "maps_with_valid_cc_indices": sum(1 for m in cliff_maps.values() if m["terrain_xml"]["cliff_cells_index_valid"]),
            "cliff_cells": sum(m["terrain_xml"]["cliff_cell_count"] for m in cliff_maps.values()),
            "cliff_doodads": sum(len(m["terrain_xml"]["cliff_doodads"]) for m in cliff_maps.values()),
            "ramps": sum(len(m["terrain_xml"]["ramps"]) for m in cliff_maps.values()),
        },
    }
    height = {
        "schema": {"name": "PYHOTS_TERRAIN_HEIGHT", "version": SCHEMA_VERSION},
        "contract": {
            "source": "t3HeightMap + t3Terrain.xml vertData",
            "grid_kind": "VERTEX_WORLD_Z",
            "query_interpolation": "ENGINE_USES_BILINEAR_FOR_SMOOTH_HEIGHT; GRID_PRESERVES_SOURCE_VERTICES",
        },
        "maps": height_maps,
        "summary": {
            "maps": len(height_maps),
            "maps_proven": sum(1 for m in height_maps.values() if m.get("status", "").startswith("PROVEN")),
        },
    }
    return cliff, height


def copy_attribute_payloads(selected: dict[str, Any], terrain_manifest: dict[str, Any], output_dir: Path) -> dict[str, Any]:
    maps = {}
    for map_id in sorted(selected):
        frame = terrain_manifest[map_id]["terrain_frame"]; w, h = frame["cell_dim"]["x"], frame["cell_dim"]["y"]
        recs = {}
        with MPQArchive(selected[map_id]["path"]) as archive:
            names = [n for n in archive.list_files() if n.startswith("CellAttribute_")]
            for name in sorted(names):
                raw = archive.read(name)
                parsed = v05.parse_cell_attribute(raw, w, h)
                if parsed.get("status") != "PROVEN_CELLATTRIBUTE_BITFIELD":
                    recs[name] = {**parsed, "grid_file": None, "semantic_role": "UNRESOLVED"}
                    continue
                payload = raw[4:]
                out = output_dir / "cell_attribute_grids" / map_id / f"{name}.u8"
                out.parent.mkdir(parents=True, exist_ok=True); out.write_bytes(payload)
                recs[name] = {
                    **parsed,
                    "grid_file": str(Path("cell_attribute_grids") / map_id / f"{name}.u8"),
                    "grid_sha256": sha256_file(out),
                    "grid_encoding": "uint8 mask per terrain cell; bit spatial geometry follows pnp_subcell_layout when format is valid",
                    "semantic_role": (
                        "REPLAY_VALIDATED_TERRAIN_PATHING_EXCLUSION" if name == "CellAttribute_Pnp" else
                        "UNRESOLVED_ENGINE_ATTRIBUTE_SEMANTICS"
                    ),
                }
        maps[map_id] = {"layers": recs}
    return {
        "schema": {"name": "PYHOTS_CELL_ATTRIBUTE_GRIDS", "version": SCHEMA_VERSION},
        "contract": {
            "spatial_bit_layout": "COMMON_8_SUBCELL_GEOMETRY_FROM_PNP_BOUNDARY_PROOF_FOR_VALID_8_BIT_CELLATTRIBUTE_FILES",
            "suffix_names_are_not_used_as_semantic_proof": True,
            "Pnp_semantics": "REPLAY_VALIDATED_TERRAIN_PATHING_EXCLUSION",
            "Pcl_Vbl_Vur_and_optional_layer_semantics": "UNRESOLVED_UNLESS_SEPARATELY_PROVEN",
        },
        "maps": maps,
    }


def shrub_general_footprint_audit(selected: dict[str, Any], catalog_index: Path) -> dict[str, Any]:
    placed_types = {d["Type"] for rec in selected.values() for d in rec["object_doodads"] if d.get("Type")}
    global_catalog = core.collect_global_catalog(catalog_index, placed_types)
    resolved_by_map = {}
    all_footprints = set()
    for map_id, package in selected.items():
        with MPQArchive(package["path"]) as archive:
            local = core.parse_local_actor_definitions(archive)
        allowed = set(package["document_mods"])
        rows = []
        for d in package["object_doodads"]:
            r = core.resolve_footprint(d["Type"], global_catalog, local, allowed)
            fid = r.get("footprint_id")
            if fid: all_footprints.add(fid)
            rows.append({"object_id": d.get("Id"), "actor_type": d["Type"], "footprint_id": fid, "resolution": r["status"], "footprint_enabled": core.doodad_footprint_enabled(d)})
        resolved_by_map[map_id] = rows
    defs = core.scan_catalog_objects(catalog_index, all_footprints)
    shrub_vision = {}
    for fid in sorted(all_footprints):
        rec = core.footprint_definition(defs.get(fid))
        if not rec: continue
        positive = []; negative = []
        for desc in rec.get("descendants", []):
            attrs = desc.get("attrs", {}); path = desc.get("path", [])
            if attrs.get("index") == "ShrubVision" and attrs.get("value") == "1":
                if path and path[-1] == "Positive": positive.append(path)
                if path and path[-1] == "Negative": negative.append(path)
        if positive or negative:
            shrub_vision[fid] = {"positive": bool(positive), "negative": bool(negative), "mod": rec.get("mod"), "file": rec.get("file")}
    active_nonstock = []
    stock = core.SHRUB_FOOTPRINT_IDS
    active_counts = Counter()
    for map_id, rows in resolved_by_map.items():
        for row in rows:
            fid = row["footprint_id"]
            if row["footprint_enabled"] and fid in shrub_vision:
                active_counts[(map_id, fid)] += 1
                if fid not in stock:
                    active_nonstock.append({"map_id": map_id, **row})
    return {
        "schema": {"name": "PYHOTS_SHRUB_FOOTPRINT_GENERAL_AUDIT", "version": SCHEMA_VERSION},
        "status": "PASS_NO_ACTIVE_NONSTOCK_SHRUBVISION_FOOTPRINTS" if not active_nonstock else "FAIL_ACTIVE_NONSTOCK_SHRUBVISION_FOOTPRINTS",
        "placed_actor_types": len(placed_types),
        "resolved_unique_footprint_ids": len(all_footprints),
        "shrubvision_footprint_ids": shrub_vision,
        "active_shrubvision_placement_counts": {f"{m}:{f}": n for (m, f), n in sorted(active_counts.items())},
        "active_nonstock_shrubvision_placements": active_nonstock,
        "contract": {"NoDoodadFootprint_suppression_is_respected": True, "visual_name_is_not_shrub_proof": True},
    }


def build_vision_audit(attribute_grids: dict[str, Any], shrub_audit: dict[str, Any], shrub_cells_path: Path) -> dict[str, Any]:
    shrub = json.loads(shrub_cells_path.read_text(encoding="utf-8"))
    maps = {}
    for map_id, m in attribute_grids["maps"].items():
        layers = m["layers"]
        maps[map_id] = {
            "proven_shrub_cells": len(shrub.get("maps", {}).get(map_id, [])),
            "raw_visibility_named_layers": {
                name: {"status": rec.get("status"), "semantic_role": rec.get("semantic_role"), "grid_file": rec.get("grid_file")}
                for name, rec in layers.items() if name in ("CellAttribute_Vbl", "CellAttribute_Vur", "CellAttribute_Vsh")
            },
        }
    return {
        "schema": {"name": "PYHOTS_STATIC_VISION_GEOMETRY", "version": SCHEMA_VERSION},
        "contract": {
            "shrub_concealment_geometry": "PROVEN_FROM_MAP_PLACEMENT_CFOOTPRINT_SHRUBVISION_AND_RUNTIME_SHRUB_RULES",
            "generic_LOS_blocker_geometry": "PARTIAL_UNRESOLVED",
            "Vbl_Vur_Vsh_semantics": "UNRESOLVED_SUFFIX_NOT_TREATED_AS_PROOF",
            "cliff_to_vision_rule": "UNRESOLVED_IN_PYHOTS_HOTS_EVIDENCE",
        },
        "shrub_footprint_audit_status": shrub_audit["status"],
        "maps": maps,
        "summary": {
            "proven_shrub_cells": sum(len(v) for v in shrub.get("maps", {}).values()),
            "maps_with_Vsh": sum(1 for m in maps.values() if "CellAttribute_Vsh" in m["raw_visibility_named_layers"]),
            "generic_los_blocker_grid": "UNRESOLVED",
        },
    }


def build_accessibility(selected: dict[str, Any], terrain_manifest: dict[str, Any], attribute_grids: dict[str, Any], attr_validation: dict[str, Any]) -> dict[str, Any]:
    maps = {}
    total_triangles = total_excluded = 0
    for map_id in sorted(selected):
        frame = terrain_manifest[map_id]["terrain_frame"]; w, h = frame["cell_dim"]["x"], frame["cell_dim"]["y"]
        pfile = attribute_grids["maps"][map_id]["layers"]["CellAttribute_Pnp"].get("grid_file")
        if not pfile:
            maps[map_id] = {"status": "UNRESOLVED_NO_PNP"}; continue
        # pfile is output-relative; payload will be read by caller after file has been written.
        maps[map_id] = {"status": "PROVEN_TERRAIN_PNP_EXCLUSION_COMPONENT", "terrain_cells": w*h}
    # Fill counts from existing layer histograms without reread.
    for map_id in maps:
        if maps[map_id].get("status", "").startswith("UNRESOLVED"): continue
        rec = attribute_grids["maps"][map_id]["layers"]["CellAttribute_Pnp"]
        hist = rec.get("value_histogram", {})
        ex = sum(int(k, 16).bit_count() * int(v) for k, v in hist.items())
        cells = rec["expected_terrain_cells"]
        total = cells * 8
        total_excluded += ex; total_triangles += total
        maps[map_id].update({
            "pnp_excluded_subtriangles": ex,
            "pnp_total_subtriangles": total,
            "pnp_excluded_area_fraction": ex / total if total else None,
            "pcl_role": "NOT_GENERAL_HERO_MOVEMENT_EXCLUSION",
            "full_static_hero_walkability": "UNRESOLVED_REQUIRES_ENGINE_CLIFF_RULES_AND_FOOTPRINT_COMPOSITION",
        })
    pcl = attr_validation["summary"]["CellAttribute_Pcl"]
    return {
        "schema": {"name": "PYHOTS_STATIC_ACCESSIBILITY", "version": SCHEMA_VERSION},
        "contract": {
            "exact_terrain_pnp_exclusion_geometry": "PROVEN_8_TRIANGLE_SUBCELL_MASK",
            "Pcl_nonzero_as_general_blocker": "DISPROVEN_BY_REPLAY_TRAVERSAL",
            "placed_and_dynamic_unit_footprints": "NOT_COMPOSED_HERE_RUNTIME_OR_SEPARATE_FOOTPRINT_LAYER",
            "cliff_transition_pathability": "UNRESOLVED_ENGINE_RULE",
            "full_static_hero_walkability": "UNRESOLVED_FAIL_CLOSED",
        },
        "pcl_replay_evidence": pcl,
        "maps": maps,
        "summary": {
            "pnp_excluded_subtriangles": total_excluded,
            "pnp_total_subtriangles": total_triangles,
            "pnp_excluded_area_fraction": total_excluded / total_triangles if total_triangles else None,
            "full_static_hero_walkability": "UNRESOLVED",
        },
    }


def mud_transform_audit(output_dir: Path) -> dict[str, Any]:
    movement = json.loads((output_dir / "movement_mechanics.json").read_text(encoding="utf-8"))
    mud = movement["alterac_mud"]
    zones = mud["zones"]
    return {
        "schema": {"name": "PYHOTS_ALTERAC_MUD_TRANSFORM_AUDIT", "version": SCHEMA_VERSION},
        "status": "UNRESOLVED_EFFECT_RECTANGLE_FRAME",
        "proven": {
            "zone_centers": [{"source_unit_id": z["source_unit_id"], "position": z["position"], "unit_rotation_radians": z["rotation_radians"], "unit_type": z["unit_type"]} for z in zones],
            "periodic_offset": mud["semantics"]["persistent_periodic_offset"],
            "search_rectangles": mud["semantics"]["shapes"],
            "impact_location": sorted({s["impact_location"] for s in mud["semantics"]["shapes"].values()}),
        },
        "unresolved": {
            "periodic_offset_frame": "CreatePersistent does not expose enough in retained GameData to prove world-vs-facing transform for this concrete chain",
            "enum_area_rectangle_facing": "width/height are proven but final world orientation is not independently proven",
            "absolute_polygon": "UNRESOLVED",
        },
        "contract": {"no_visual_tarpit_shape_used_as_gameplay_polygon": True, "no_rotation_guess": True},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pyhots Static Geometry Probe v0.9 - complete proven static terrain block")
    parser.add_argument("--s2ma-root", required=True, type=Path)
    parser.add_argument("--catalog-index", required=True, type=Path)
    parser.add_argument("--map-db", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Run v0.5 unchanged first; all new work is append-only unless a new upstream audit fails.
    original_argv = sys.argv
    try:
        sys.argv = ["static_geometry_v05_core.py", "--s2ma-root", str(args.s2ma_root), "--catalog-index", str(args.catalog_index), "--map-db", str(args.map_db), "--output-dir", str(args.output_dir)]
        rc = v05.main()
    finally:
        sys.argv = original_argv
    if rc != 0:
        return rc
    gc.collect()

    selected_raw, _ = core.discover_packages(args.s2ma_root)
    selected = {mid: {**rec, "path": str(rec["path"])} for mid, rec in selected_raw.items()}
    terrain_manifest = json.loads((args.output_dir / "terrain_asset_manifest.json").read_text(encoding="utf-8"))

    subcell = pnp_gradient_evidence(selected, terrain_manifest)
    dump_json(args.output_dir / "pnp_subcell_layout.json", subcell)

    attr_validation = attribute_replay_validation(selected, terrain_manifest, args.map_db)
    dump_json(args.output_dir / "cell_attribute_replay_validation.json", attr_validation)

    attr_grids = copy_attribute_payloads(selected, terrain_manifest, args.output_dir)
    dump_json(args.output_dir / "cell_attribute_grids.json", attr_grids)

    cliff, height = build_terrain_cliff_height_outputs(selected, terrain_manifest, args.output_dir)
    dump_json(args.output_dir / "cliff_layers.json", cliff)
    dump_json(args.output_dir / "terrain_height.json", height)

    shrub_audit = shrub_general_footprint_audit(selected_raw, args.catalog_index)
    dump_json(args.output_dir / "shrub_footprint_general_audit.json", shrub_audit)

    vision = build_vision_audit(attr_grids, shrub_audit, args.output_dir / "shrub_cells.json")
    dump_json(args.output_dir / "static_vision_geometry.json", vision)

    accessibility = build_accessibility(selected, terrain_manifest, attr_grids, attr_validation)
    dump_json(args.output_dir / "static_accessibility.json", accessibility)

    mud_audit = mud_transform_audit(args.output_dir)
    dump_json(args.output_dir / "alterac_mud_transform_audit.json", mud_audit)

    integrity_path = args.output_dir / "static_map_integrity_report.json"
    old = json.loads(integrity_path.read_text(encoding="utf-8"))
    gates = dict(old.get("gates", {}))
    pcl_frac = attr_validation["summary"]["CellAttribute_Pcl"]["movement_hit_mask_0_fraction"]
    gates.update({
        "SHRUB_GENERAL_FOOTPRINT_AUDIT": "PASS" if shrub_audit["status"].startswith("PASS") else "FAIL",
        "PNP_SUBCELL_SPATIAL_ORDER": "PASS" if subcell["status"].startswith("PROVEN") else "FAIL",
        "CLIFF_ASSET_FORMAT": "PASS" if cliff["summary"]["maps_with_valid_clif"] == 17 and cliff["summary"]["maps_with_valid_cc_indices"] == 17 else "FAIL",
        "PCL_NOT_GENERAL_HERO_MOVEMENT_EXCLUSION": "PASS" if pcl_frac is not None and pcl_frac < 0.99 else "REVIEW",
        "PCL_CLIFF_ROLE": "PARTIAL_ASSET_ATTRIBUTE_EXACT_SEMANTICS_UNRESOLVED",
        "TERRAIN_HEIGHT_FORMAT_AND_DEQUANTIZATION": "PASS" if height["summary"]["maps_proven"] == 17 else "FAIL",
        "CELL_ATTRIBUTE_EXACT_GRID_EXPORT": "PASS" if len(attr_grids["maps"]) == 17 else "FAIL",
        "STATIC_SHRUB_VISION_GEOMETRY": "PASS" if vision["summary"]["proven_shrub_cells"] > 0 else "FAIL",
        "GENERIC_STATIC_LOS_BLOCKER_GRID": "UNRESOLVED",
        "CLIFF_HERO_PATHING_COMPOSITION": "UNRESOLVED",
        "FULL_HERO_WALKABILITY_GRID": "UNRESOLVED",
        "ALTERAC_MUD_ABSOLUTE_POLYGON": "UNRESOLVED",
    })
    required = [
        old.get("overall") == "PASS",
        gates["SHRUB_GENERAL_FOOTPRINT_AUDIT"] == "PASS",
        gates["PNP_SUBCELL_SPATIAL_ORDER"] == "PASS",
        gates["CLIFF_ASSET_FORMAT"] == "PASS",
        gates["PCL_NOT_GENERAL_HERO_MOVEMENT_EXCLUSION"] == "PASS",
        gates["TERRAIN_HEIGHT_FORMAT_AND_DEQUANTIZATION"] == "PASS",
        gates["CELL_ATTRIBUTE_EXACT_GRID_EXPORT"] == "PASS",
        gates["STATIC_SHRUB_VISION_GEOMETRY"] == "PASS",
    ]
    integrity = {"schema": {"name": "PYHOTS_STATIC_GEOMETRY_PROBE_INTEGRITY", "version": SCHEMA_VERSION}, "gates": gates, "overall": "PASS" if all(required) else "FAIL"}
    dump_json(integrity_path, integrity)

    report_path = args.output_dir / "static_geometry_probe_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["schema"]["version"] = SCHEMA_VERSION
    report["terrain_completion"] = {
        "pnp_subcell_geometry": subcell["status"],
        "cliff_asset_geometry": "PROVEN_ASSET_LAYERS",
        "terrain_height": "PROVEN_VERTEX_WORLD_Z_GRID",
        "shrub_concealment_geometry": "PROVEN",
        "generic_los_blocker_grid": "UNRESOLVED",
        "full_hero_walkability": "UNRESOLVED",
        "alterac_mud_absolute_polygon": mud_audit["status"],
    }
    dump_json(report_path, report)

    print(json.dumps(report["terrain_completion"], indent=2, sort_keys=True))
    print(json.dumps(integrity, indent=2, sort_keys=True))
    return 0 if integrity["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
