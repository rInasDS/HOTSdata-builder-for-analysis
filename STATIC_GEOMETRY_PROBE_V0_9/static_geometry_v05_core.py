from __future__ import annotations

import argparse
import gc
import json
import mmap
import struct
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import static_geometry_v04_core as v04
from mpq_reader import MPQArchive

# Backward-compatible exports for v0.2-v0.4 regression tests.
rect_from_center = v04.rect_from_center
point_in_rect = v04.point_in_rect
connected_components = v04.connected_components
mapinfo_label = v04.mapinfo_label
parse_position = v04.parse_position
parse_scale = v04.parse_scale
parse_object_doodads = v04.parse_object_doodads
doodad_footprint_enabled = v04.doodad_footprint_enabled
parse_terrain_frame = v04.parse_terrain_frame
footprint_place_area = v04.footprint_place_area
absolute_shrub_geometry = v04.absolute_shrub_geometry

SCHEMA_VERSION = "0.5"
CORE_LAYERS = ("CellAttribute_Pcl", "CellAttribute_Pnp", "CellAttribute_Vbl", "CellAttribute_Vur")


def dump_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_cell_attribute(raw: bytes | None, width: int, height: int) -> dict[str, Any]:
    if not raw or len(raw) < 4:
        return {"status": "UNRESOLVED_MISSING_OR_TRUNCATED"}
    bit_count = struct.unpack_from("<I", raw, 0)[0]
    payload = raw[4:]
    expected_cells = width * height
    expected_bits = expected_cells * 8
    format_ok = bit_count == expected_bits and len(payload) == expected_cells
    counts = Counter(payload)
    return {
        "status": "PROVEN_CELLATTRIBUTE_BITFIELD" if format_ok else "UNRESOLVED_DIMENSION_MISMATCH",
        "bit_count_header": bit_count,
        "payload_bytes": len(payload),
        "expected_terrain_cells": expected_cells,
        "expected_bits_at_8_per_cell": expected_bits,
        "bits_per_terrain_cell": 8 if format_ok else None,
        "zero_cells": counts.get(0, 0),
        "full_cells": counts.get(255, 0),
        "partial_cells": len(payload) - counts.get(0, 0) - counts.get(255, 0),
        "value_histogram": {f"0x{k:02x}": v for k, v in sorted(counts.items())},
    }


def _row_spans_for_value(payload: bytes, width: int, height: int, value: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for y in range(height):
        row = payload[y * width:(y + 1) * width]
        spans = []
        x = 0
        while x < width:
            if row[x] != value:
                x += 1
                continue
            start = x
            x += 1
            while x < width and row[x] == value:
                x += 1
            spans.append([start, x])  # half-open [x0,x1)
        if spans:
            out.append({"y": y, "x_spans_half_open": spans})
    return out


def _partial_cells(payload: bytes, width: int) -> list[dict[str, Any]]:
    return [
        {"x": i % width, "y": i // width, "mask": f"0x{b:02x}", "set_bit_count": int(b).bit_count()}
        for i, b in enumerate(payload)
        if b not in (0, 255)
    ]


def _read_map_object(path: Path, map_id: str) -> dict[str, Any]:
    """Read one named map object from the large Map DB without loading the full file."""
    needle = (json.dumps(map_id) + ": {").encode("utf-8")
    with path.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
        pos = mm.find(needle)
        if pos < 0:
            # tolerate compact formatting
            needle2 = (json.dumps(map_id) + ":{").encode("utf-8")
            pos = mm.find(needle2)
            if pos < 0:
                raise KeyError(f"Map {map_id!r} not found in {path}")
            brace = pos + len(needle2) - 1
        else:
            brace = pos + len(needle) - 1
        depth = 0
        in_string = False
        escape = False
        end = None
        for i in range(brace, len(mm)):
            c = mm[i]
            if in_string:
                if escape:
                    escape = False
                elif c == 0x5C:
                    escape = True
                elif c == 0x22:
                    in_string = False
                continue
            if c == 0x22:
                in_string = True
            elif c == 0x7B:
                depth += 1
            elif c == 0x7D:
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end is None:
            raise ValueError(f"Unterminated map object {map_id!r}")
        return json.loads(mm[brace:end].decode("utf-8"))


def _cell_byte(payload: bytes, width: int, height: int, x: float, y: float) -> int | None:
    ix, iy = int(x // 1), int(y // 1)
    if ix < 0 or iy < 0 or ix >= width or iy >= height:
        return None
    return payload[iy * width + ix]


def build_pathing_layer_outputs(
    selected: dict[str, Any],
    terrain_manifest: dict[str, Any],
    map_db: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    layers_report: dict[str, Any] = {
        "schema": {"name": "PYHOTS_CELL_ATTRIBUTE_LAYERS", "version": SCHEMA_VERSION},
        "contract": {
            "bitfield_format": "uint32 bit_count + ceil(bit_count/8) payload bytes",
            "expected_common_layer_bits": "8 bits per terrain cell",
            "t3CellFlags_is_not_pathing": True,
            "unknown_suffix_semantics_are_not_guessed": True,
        },
        "maps": {},
    }
    pnp_geometry: dict[str, Any] = {
        "schema": {"name": "PYHOTS_TERRAIN_PNP_MASK", "version": SCHEMA_VERSION},
        "semantics": {
            "role": "REPLAY_VALIDATED_TERRAIN_PATHING_EXCLUSION_ATTRIBUTE",
            "role_evidence": "MAP_ASSET_CELLATTRIBUTE_FORMAT_PLUS_REPLAY_MOVEMENT_AND_SPAWN_CROSS_VALIDATION",
            "mask_0x00": "PNP_CLEAR_ONLY_NOT_FULL_WALKABILITY_PROOF",
            "mask_0xff": "FULL_PNP_EXCLUSION_MASK_AT_TERRAIN_CELL",
            "partial_mask": "PROVEN_8_BIT_MASK_SUBCELL_SPATIAL_ORDER_UNRESOLVED",
            "full_walkability": "UNRESOLVED_NEEDS_PCL_CLIFF_AND_FOOTPRINT_COMPOSITION",
        },
        "maps": {},
    }
    validation_maps: dict[str, Any] = {}
    total_move_cells = total_move_clear_cells = total_hits = total_clear_hits = 0
    total_spawns = total_clear_spawns = 0

    for map_id in sorted(selected):
        package = selected[map_id]
        archive = MPQArchive(package["path"])
        frame = terrain_manifest[map_id]["terrain_frame"]
        w, h = frame["cell_dim"]["x"], frame["cell_dim"]["y"]
        listfile = archive.read("(listfile)") or b""
        attr_names = sorted({
            line.strip() for line in listfile.decode("utf-8", "ignore").splitlines()
            if line.strip().startswith("CellAttribute_")
        })
        map_layers = {}
        pnp_payload = None
        for name in attr_names:
            raw = archive.read(name)
            parsed = parse_cell_attribute(raw, w, h)
            map_layers[name] = parsed
            if name == "CellAttribute_Pnp" and parsed["status"] == "PROVEN_CELLATTRIBUTE_BITFIELD":
                pnp_payload = raw[4:]
        layers_report["maps"][map_id] = {
            "archive_name": Path(package["path"]).name,
            "terrain_cell_dim": {"x": w, "y": h},
            "layers": map_layers,
            "core_layer_coverage": {name: name in map_layers for name in CORE_LAYERS},
        }
        if pnp_payload is None:
            pnp_geometry["maps"][map_id] = {"status": "UNRESOLVED_NO_VALID_PNP"}
            validation_maps[map_id] = {"status": "UNRESOLVED_NO_VALID_PNP"}
            continue

        pnp_geometry["maps"][map_id] = {
            "status": "PROVEN_ASSET_MASK",
            "terrain_cell_dim": {"x": w, "y": h},
            "full_exclusion_0xff_row_spans": _row_spans_for_value(pnp_payload, w, h, 255),
            "partial_mask_cells": _partial_cells(pnp_payload, w),
            "counts": {
                "clear_0x00": pnp_payload.count(0),
                "full_exclusion_0xff": pnp_payload.count(255),
                "partial": sum(1 for b in pnp_payload if b not in (0, 255)),
            },
        }

        m = _read_map_object(map_db, map_id)
        move_cells = m.get("hero_movement_grid", {}).get("cells", [])
        mc = mch = hits = hitclear = 0
        nonclear = Counter()
        for cell in move_cells:
            b = _cell_byte(pnp_payload, w, h, float(cell["center_x"]), float(cell["center_y"]))
            if b is None:
                continue
            mc += 1
            cell_hits = int(cell.get("hits", 0))
            hits += cell_hits
            if b == 0:
                mch += 1
                hitclear += cell_hits
            else:
                nonclear[b] += cell_hits
        spawns = m.get("hero_spawn_regions", [])
        sc = scl = 0
        spawn_nonclear = []
        for spawn in spawns:
            b = _cell_byte(pnp_payload, w, h, float(spawn["x"]), float(spawn["y"]))
            if b is None:
                continue
            sc += 1
            if b == 0:
                scl += 1
            else:
                spawn_nonclear.append({"team": spawn.get("team"), "x": spawn["x"], "y": spawn["y"], "mask": f"0x{b:02x}"})
        total_move_cells += mc; total_move_clear_cells += mch
        total_hits += hits; total_clear_hits += hitclear
        total_spawns += sc; total_clear_spawns += scl
        validation_maps[map_id] = {
            "status": "PASS" if sc == scl and (hits == 0 or hitclear / hits >= 0.99) else "REVIEW",
            "movement_grid_is_coarse": True,
            "movement_grid_cell_size_world": m.get("hero_movement_grid", {}).get("cell_size"),
            "movement_cells_sampled": mc,
            "movement_cells_at_pnp_0": mch,
            "movement_hits_sampled": hits,
            "movement_hits_at_pnp_0": hitclear,
            "movement_hit_clear_fraction": (hitclear / hits) if hits else None,
            "movement_nonzero_mask_hit_histogram": {f"0x{k:02x}": v for k, v in sorted(nonclear.items())},
            "hero_spawn_regions_sampled": sc,
            "hero_spawn_regions_at_pnp_0": scl,
            "spawn_nonclear": spawn_nonclear,
        }

    common_valid = sum(
        1 for m in layers_report["maps"].values()
        if all(m["layers"].get(name, {}).get("status") == "PROVEN_CELLATTRIBUTE_BITFIELD" for name in CORE_LAYERS)
    )
    layers_report["summary"] = {
        "maps": len(selected),
        "maps_with_all_4_core_layers_valid": common_valid,
        "core_layers": list(CORE_LAYERS),
        "pnp_maps_valid": sum(1 for m in pnp_geometry["maps"].values() if m.get("status") == "PROVEN_ASSET_MASK"),
    }
    pnp_geometry["summary"] = {
        "maps": len(pnp_geometry["maps"]),
        "clear_0x00": sum(m.get("counts", {}).get("clear_0x00", 0) for m in pnp_geometry["maps"].values()),
        "full_exclusion_0xff": sum(m.get("counts", {}).get("full_exclusion_0xff", 0) for m in pnp_geometry["maps"].values()),
        "partial": sum(m.get("counts", {}).get("partial", 0) for m in pnp_geometry["maps"].values()),
    }
    validation = {
        "schema": {"name": "PYHOTS_PATHING_REPLAY_CROSS_VALIDATION", "version": SCHEMA_VERSION},
        "contract": {
            "replay_is_cross_validation_not_geometry_source": True,
            "coarse_movement_grid_cannot_prove_subcell_orientation": True,
            "spawn_centers_are_strong_clear_pathing_evidence": True,
        },
        "maps": validation_maps,
        "summary": {
            "movement_cells_sampled": total_move_cells,
            "movement_cells_at_pnp_0": total_move_clear_cells,
            "movement_cell_clear_fraction": total_move_clear_cells / total_move_cells if total_move_cells else None,
            "movement_hits_sampled": total_hits,
            "movement_hits_at_pnp_0": total_clear_hits,
            "movement_hit_clear_fraction": total_clear_hits / total_hits if total_hits else None,
            "hero_spawn_regions_sampled": total_spawns,
            "hero_spawn_regions_at_pnp_0": total_clear_spawns,
            "hero_spawn_clear_fraction": total_clear_spawns / total_spawns if total_spawns else None,
        },
    }
    return layers_report, pnp_geometry, validation


def main() -> int:
    parser = argparse.ArgumentParser(description="Pyhots Static Geometry Probe v0.5 - terrain pathing layer")
    parser.add_argument("--s2ma-root", required=True, type=Path)
    parser.add_argument("--catalog-index", required=True, type=Path)
    parser.add_argument("--map-db", required=True, type=Path, help="Map DB v0.2+ used only for replay cross-validation")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Preserve v0.4 exactly, then append pathing evidence.
    original_argv = sys.argv
    try:
        sys.argv = ["static_geometry_v04_core.py", "--s2ma-root", str(args.s2ma_root), "--catalog-index", str(args.catalog_index), "--output-dir", str(args.output_dir)]
        rc = v04.main()
    finally:
        sys.argv = original_argv
    if rc != 0:
        return rc
    gc.collect()

    selected_raw, _ = v04.core.discover_packages(args.s2ma_root)
    selected = {mid: {**rec, "path": str(rec["path"])} for mid, rec in selected_raw.items()}
    terrain_manifest = json.loads((args.output_dir / "terrain_asset_manifest.json").read_text(encoding="utf-8"))
    layers, pnp, validation = build_pathing_layer_outputs(selected, terrain_manifest, args.map_db)
    dump_json(args.output_dir / "cell_attribute_layers.json", layers)
    dump_json(args.output_dir / "terrain_pnp_mask.json", pnp)
    dump_json(args.output_dir / "pathing_replay_validation.json", validation)

    integrity_path = args.output_dir / "static_map_integrity_report.json"
    integrity = json.loads(integrity_path.read_text(encoding="utf-8"))
    gates = dict(integrity.get("gates", {}))
    s = validation["summary"]
    gates.update({
        "CELL_ATTRIBUTE_CORE_FORMAT": "PASS" if layers["summary"]["maps_with_all_4_core_layers_valid"] == 17 else "FAIL",
        "PNP_LAYER_COVERAGE": "PASS" if layers["summary"]["pnp_maps_valid"] == 17 else "FAIL",
        "PNP_HERO_SPAWN_CROSS_VALIDATION": "PASS" if s["hero_spawn_regions_sampled"] == s["hero_spawn_regions_at_pnp_0"] and s["hero_spawn_regions_sampled"] >= 34 else "FAIL",
        "PNP_HERO_MOVEMENT_CROSS_VALIDATION": "PASS" if (s["movement_hit_clear_fraction"] or 0) >= 0.999 else "FAIL",
        "PNP_SUBCELL_SPATIAL_ORDER": "UNRESOLVED",
        "FULL_HERO_WALKABILITY_GRID": "UNRESOLVED",
        "PCL_CLIFF_ROLE": "UNRESOLVED_NEXT_STATIC_CLIFF_STAGE",
    })
    required = [
        integrity.get("overall") == "PASS",
        gates["CELL_ATTRIBUTE_CORE_FORMAT"] == "PASS",
        gates["PNP_LAYER_COVERAGE"] == "PASS",
        gates["PNP_HERO_SPAWN_CROSS_VALIDATION"] == "PASS",
        gates["PNP_HERO_MOVEMENT_CROSS_VALIDATION"] == "PASS",
    ]
    integrity = {
        "schema": {"name": "PYHOTS_STATIC_GEOMETRY_PROBE_INTEGRITY", "version": SCHEMA_VERSION},
        "gates": gates,
        "overall": "PASS" if all(required) else "FAIL",
    }
    dump_json(integrity_path, integrity)

    report_path = args.output_dir / "static_geometry_probe_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["schema"]["version"] = SCHEMA_VERSION
    report["pathing"] = {
        "status": "PROVEN_TERRAIN_PNP_EXCLUSION_LAYER_FULL_WALKABILITY_UNRESOLVED",
        "core_attribute_maps": layers["summary"]["maps_with_all_4_core_layers_valid"],
        "pnp_maps": layers["summary"]["pnp_maps_valid"],
        "replay_movement_hit_clear_fraction": s["movement_hit_clear_fraction"],
        "spawn_clear_fraction": s["hero_spawn_clear_fraction"],
        "full_walkability": "UNRESOLVED_NEEDS_CLIFF_AND_FOOTPRINT_COMPOSITION",
    }
    dump_json(report_path, report)

    print(json.dumps(report["pathing"], indent=2, sort_keys=True))
    print(json.dumps(integrity, indent=2, sort_keys=True))
    return 0 if integrity["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
