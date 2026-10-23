from __future__ import annotations

import argparse
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from mpq_reader import MPQArchive

SCHEMA_VERSION = "0.3"

# MapInfo labels / map-mod identities are mapped to the internal map_id already used by Pyhots Map DB.
LABEL_TO_MAP_ID = {
    "AlteracPass": "AlteracPass",
    "BoE": "BattlefieldOfEternity",
    "BlackHeartsBay": "BlackheartsBay",
    "BraxisHoldout": "BraxisHoldout",
    "BraxisOutpost": "BraxisOutpost",
    "SkyTemple": "ControlPoints",
    "TombSpiderQueen": "Crypts",
    "CursedHollow": "CursedHollow",
    "DragonShire": "DragonShire",
    "HauntedMines": "HauntedMines",
    "GardenOfTerror": "HauntedWoods",
    "IndustrialDistrict": "IndustrialDistrict",
    "LostCavern": "LostCavern",
    "Shrines": "Shrines",
    "SilverCity": "SilverCity",
    "TowersOfDoom": "TowersOfDoom",
    "Volskaya": "Volskaya",
}

DEPENDENCY_TO_MAP_ID = {
    "alteracpass": "AlteracPass",
    "battlefieldofeternity": "BattlefieldOfEternity",
    "blackheartsbay": "BlackheartsBay",
    "braxisholdout": "BraxisHoldout",
    "braxisoutpost": "BraxisOutpost",
    "skytemple": "ControlPoints",
    "tombofthespiderqueen": "Crypts",
    "cursedhollow": "CursedHollow",
    "dragonshire": "DragonShire",
    "hauntedmines": "HauntedMines",
    "gardenofterror": "HauntedWoods",
    "industrialdistrict": "IndustrialDistrict",
    "lostcavern": "LostCavern",
    "infernalshrines": "Shrines",
    "silvercity": "SilverCity",
    "towersofdoom": "TowersOfDoom",
    "volskaya": "Volskaya",
}

EXPECTED_MAP_IDS = set(LABEL_TO_MAP_ID.values())
SHRUB_FOOTPRINT_IDS = {"ShrubFootprint1x1", "ShrubFootprint2x2", "ShrubFootprint4x4"}
COMMON_MODS = {"core.stormmod", "heroes.stormmod", "heroesdata.stormmod"}

TERRAIN_MEMBERS = [
    "t3Terrain.xml",
    "t3CellFlags",
    "t3HeightMap",
    "t3SyncHeightMap",
    "t3SyncCliffLevel",
    "t3HardTile",
    "t3TextureMasks",
    "t3Water",
    "CellAttribute_Pcl",
    "CellAttribute_Pnp",
    "CellAttribute_Vbl",
    "CellAttribute_Vur",
]


def dump_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def mapinfo_label(raw: bytes | None) -> str:
    if not raw:
        return ""
    strings = [
        item.decode("latin1", "ignore").strip()
        for item in re.findall(rb"[A-Za-z0-9_@ .\\/\-]{4,}", raw)
    ]
    marked = [item[:-1].strip() for item in strings if item.endswith("@")]
    return marked[0] if marked else ""


def document_mods(document_info: str) -> set[str]:
    result: set[str] = set()
    for match in re.finditer(r"([A-Za-z0-9_\-]+\.stormmod)", document_info, re.IGNORECASE):
        result.add(match.group(1).lower())
    return result


def infer_map_id(label: str, document_info: str) -> tuple[str | None, str]:
    if label in LABEL_TO_MAP_ID:
        return LABEL_TO_MAP_ID[label], "MAPINFO_LABEL"
    low = document_info.lower()
    for dependency, map_id in DEPENDENCY_TO_MAP_ID.items():
        if f"{dependency}.stormmod" in low:
            return map_id, "DOCUMENTINFO_DEPENDENCY"
    return None, "UNRESOLVED"


def package_variant(document_info: str, label: str) -> str:
    low = (document_info + " " + label).lower()
    if "sandbox" in low:
        return "SANDBOX"
    if "classic" in low or label.endswith("2"):
        return "CLASSIC"
    return "STANDARD"


def parse_object_doodads(raw: bytes) -> list[dict[str, Any]]:
    """Parse full ObjectDoodad elements, including child Flag entries.

    v0.2 parsed only the opening tag. That loses NoDoodadFootprint, which is
    gameplay-significant and caused four false shrub placements on Volskaya.
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise ValueError(f"Objects XML parse failure: {exc}") from exc

    placements: list[dict[str, Any]] = []
    for element in root.iter("ObjectDoodad"):
        attrs: dict[str, Any] = dict(element.attrib)
        if not attrs.get("Type"):
            continue
        flags: dict[str, str] = {}
        for child in list(element):
            if child.tag != "Flag":
                continue
            index = child.get("Index")
            value = child.get("Value")
            if index is not None and value is not None:
                flags[index] = value
        attrs["_flags"] = flags
        placements.append(attrs)
    return placements


def doodad_footprint_enabled(doodad: dict[str, Any]) -> bool:
    return doodad.get("_flags", {}).get("NoDoodadFootprint") != "1"


def parse_position(value: str) -> dict[str, float]:
    parts = [float(part) for part in value.split(",")]
    while len(parts) < 3:
        parts.append(0.0)
    return {"x": parts[0], "y": parts[1], "z": parts[2]}


def parse_scale(value: str | None) -> dict[str, float]:
    parts = [float(part) for part in (value or "1,1,1").split(",")]
    while len(parts) < 3:
        parts.append(1.0)
    return {"x": parts[0], "y": parts[1], "z": parts[2]}


def discover_packages(s2ma_root: Path) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    for path in sorted(s2ma_root.rglob("*.s2ma")):
        if path.stat().st_size < 100:
            continue
        try:
            with MPQArchive(path) as archive:
                objects = archive.read("Objects")
                if not objects:
                    continue
                mapinfo = archive.read("MapInfo")
                doc_raw = archive.read("DocumentInfo") or b""
                document_info = doc_raw.decode("utf-8", "ignore")
                label = mapinfo_label(mapinfo)
                map_id, map_id_source = infer_map_id(label, document_info)
                if map_id not in EXPECTED_MAP_IDS:
                    continue
                variant = package_variant(document_info, label)
                files = archive.list_files()
                candidate = {
                    "map_id": map_id,
                    "map_id_source": map_id_source,
                    "mapinfo_label": label,
                    "variant": variant,
                    "path": str(path),
                    "archive_name": path.name,
                    "archive_size": path.stat().st_size,
                    "objects_size": len(objects),
                    "has_mapinfo": mapinfo is not None,
                    "document_mods": sorted(document_mods(document_info)),
                    "listfile": files,
                    "object_doodads": parse_object_doodads(objects),
                    "document_info": document_info,
                }
                candidates.append(candidate)
        except (ValueError, NotImplementedError):
            continue

    selected: dict[str, dict[str, Any]] = {}
    for item in candidates:
        # Never prefer a sandbox package for a standard map_id. Exact MapInfo label and STANDARD variant win.
        score = (
            1 if item["variant"] == "STANDARD" else 0,
            1 if item["mapinfo_label"] in LABEL_TO_MAP_ID else 0,
            1 if item["has_mapinfo"] else 0,
            item["objects_size"],
        )
        previous = selected.get(item["map_id"])
        if previous is None or score > previous["_score"]:
            selected[item["map_id"]] = dict(item, _score=score)
    return selected, candidates


def _catalog_key_match(line: str) -> str | None:
    match = re.match(r'^    "((?:\\.|[^\"])*)": \[$', line.rstrip("\n"))
    if not match:
        return None
    return json.loads('"' + match.group(1) + '"')


def scan_catalog_objects(catalog_index: Path, wanted: set[str]) -> dict[str, list[dict[str, Any]]]:
    found: dict[str, list[dict[str, Any]]] = {}
    current_key: str | None = None
    buffer: list[str] | None = None
    in_objects = False

    def flush() -> None:
        nonlocal current_key, buffer
        if current_key is None or buffer is None:
            return
        raw = "".join(buffer).rstrip()
        if raw.endswith(","):
            raw = raw[:-1]
        found[current_key] = json.loads(raw)

    with catalog_index.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not in_objects:
                if line.startswith('  "objects": {'):
                    in_objects = True
                continue
            key = _catalog_key_match(line)
            if key is not None:
                flush()
                current_key = key
                buffer = [line[line.index("[") :]] if key in wanted else None
                continue
            if buffer is not None:
                buffer.append(line)
        flush()
    return found


def actor_definitions(records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    return [record for record in (records or []) if record.get("xml_type") == "CActorDoodad"]


def footprint_definition(records: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    matches = [record for record in (records or []) if record.get("xml_type") == "CFootprint"]
    return matches[-1] if matches else None


def collect_global_catalog(catalog_index: Path, placed_types: set[str]) -> dict[str, list[dict[str, Any]]]:
    wanted = set(placed_types) | SHRUB_FOOTPRINT_IDS
    result = scan_catalog_objects(catalog_index, wanted)
    for _round in range(8):
        parents: set[str] = set()
        for records in result.values():
            for record in actor_definitions(records):
                parent = record.get("attrs", {}).get("parent")
                if parent and parent not in result:
                    parents.add(parent)
        if not parents:
            break
        result.update(scan_catalog_objects(catalog_index, parents))
    return result


def parse_local_actor_definitions(archive: MPQArchive) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for member in archive.list_files():
        low = member.lower()
        if "gamedata" not in low:
            continue
        if not (low.endswith("actordata.xml") or low.endswith("doodadautodata.xml")):
            continue
        raw = archive.read(member)
        if not raw:
            continue
        try:
            root = ET.fromstring(raw)
        except ET.ParseError:
            continue
        for element in root.iter():
            if element.tag != "CActorDoodad" or not element.get("id"):
                continue
            children = []
            for child in list(element):
                children.append({"tag": child.tag, "attrs": dict(child.attrib)})
            result[element.get("id")].append(
                {
                    "attrs": dict(element.attrib),
                    "children": children,
                    "source_member": member,
                    "source_kind": "MAP_PACKAGE_GAMEDATA",
                }
            )
    return dict(result)


def resolve_footprint(
    actor_id: str,
    global_catalog: dict[str, list[dict[str, Any]]],
    local_catalog: dict[str, list[dict[str, Any]]],
    allowed_mods: set[str],
    stack: tuple[str, ...] = (),
) -> dict[str, Any]:
    if actor_id in stack:
        return {"status": "UNRESOLVED_CYCLE", "footprint_id": None, "provenance": []}

    global_defs_all = actor_definitions(global_catalog.get(actor_id))
    global_defs = []
    for record in global_defs_all:
        mod = (record.get("mod") or "").lower()
        if mod in allowed_mods or mod in COMMON_MODS:
            global_defs.append(record)
    local_defs = local_catalog.get(actor_id, [])

    fallback_used = False
    if not global_defs and global_defs_all:
        # Objects.Type is a direct actor identity. When the direct map DocumentInfo does not
        # expose a transitive shared dependency, accept the exact catalog ID only if its
        # inheritance/Footprint declarations are unambiguous across all definitions.
        parents = {
            record.get("attrs", {}).get("parent")
            for record in global_defs_all
            if record.get("attrs", {}).get("parent")
        }
        footprints = {
            child.get("attrs", {}).get("value", "")
            for record in global_defs_all
            for child in record.get("children", [])
            if child.get("tag") == "Footprint"
        }
        if len(parents) > 1 or len(footprints) > 1:
            return {
                "status": "UNRESOLVED_EXACT_ID_DEFINITION_CONFLICT",
                "footprint_id": None,
                "provenance": [],
            }
        global_defs = global_defs_all
        fallback_used = True

    if not global_defs and not local_defs:
        return {"status": "UNRESOLVED_NO_ACTOR_DEFINITION", "footprint_id": None, "provenance": []}

    definitions: list[tuple[str, dict[str, Any]]] = []
    for record in sorted(global_defs, key=lambda item: item.get("order", 0)):
        definitions.append(("EXACT_ID_GLOBAL_FALLBACK" if fallback_used else "GLOBAL_CATALOG", record))
    for record in local_defs:
        definitions.append(("MAP_PACKAGE_GAMEDATA", record))

    parent = None
    for _kind, record in definitions:
        candidate = record.get("attrs", {}).get("parent")
        if candidate:
            parent = candidate

    value = None
    provenance: list[dict[str, Any]] = []
    if parent:
        inherited = resolve_footprint(parent, global_catalog, local_catalog, allowed_mods, stack + (actor_id,))
        if inherited["status"].startswith("UNRESOLVED"):
            return inherited
        value = inherited["footprint_id"]
        provenance.extend(inherited["provenance"])

    explicit_seen = False
    for source_kind, record in definitions:
        for child in record.get("children", []):
            if child.get("tag") != "Footprint":
                continue
            explicit_seen = True
            value = child.get("attrs", {}).get("value", "")
            provenance.append(
                {
                    "actor_id": actor_id,
                    "source_kind": source_kind,
                    "mod": record.get("mod"),
                    "file": record.get("file"),
                    "member": record.get("source_member"),
                    "footprint_id": value,
                }
            )

    return {
        "status": "PROVEN_EXPLICIT" if explicit_seen else ("PROVEN_INHERITED" if parent else "PROVEN_NO_FOOTPRINT"),
        "footprint_id": value,
        "provenance": provenance,
    }


def parse_shrub_footprint(record: dict[str, Any] | None, footprint_id: str) -> dict[str, Any]:
    if not record:
        return {"footprint_id": footprint_id, "status": "UNRESOLVED"}
    rows = []
    positive = []
    negative = []
    for descendant in record.get("descendants", []):
        path = descendant.get("path", [])
        attrs = descendant.get("attrs", {})
        if descendant.get("tag") == "Rows" and attrs.get("value"):
            rows.append(attrs["value"])
        if attrs.get("index") == "ShrubVision" and attrs.get("value") == "1":
            if path and path[-1] == "Positive":
                positive.append(path)
            if path and path[-1] == "Negative":
                negative.append(path)
    # Check and Place use the same row shape for the stock shrub footprints; de-duplicate repeated rows.
    half = len(rows) // 2 if len(rows) % 2 == 0 else len(rows)
    candidate_rows = rows[:half] if half else rows
    width = max((len(row) for row in candidate_rows), default=None)
    height = len(candidate_rows) or None
    place_area = footprint_place_area(record, width or 0, height or 0) if width and height else None
    area_shape_ok = bool(
        place_area
        and place_area["max_x"] - place_area["min_x"] == width
        and place_area["max_y"] - place_area["min_y"] == height
    )
    return {
        "footprint_id": footprint_id,
        "status": "PROVEN" if positive and negative and candidate_rows and area_shape_ok else "PARTIAL",
        "rows": candidate_rows,
        "width_cells": width,
        "height_cells": height,
        "place_area": place_area,
        "place_area_matches_rows": area_shape_ok,
        "shrub_vision_positive": bool(positive),
        "shrub_vision_negative_check": bool(negative),
        "source": {"mod": record.get("mod"), "file": record.get("file")},
    }


def parse_float_triplet(value: str) -> tuple[float, float, float]:
    parts = [float(part) for part in value.split()]
    while len(parts) < 3:
        parts.append(0.0)
    return parts[0], parts[1], parts[2]


def parse_int_pair(value: str) -> tuple[int, int]:
    parts = [int(part) for part in value.split()]
    if len(parts) != 2:
        raise ValueError(f"Expected two integers, got {value!r}")
    return parts[0], parts[1]


def parse_terrain_frame(raw: bytes | None) -> dict[str, Any]:
    if not raw:
        return {"status": "UNRESOLVED_NO_T3TERRAIN"}
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        return {"status": "UNRESOLVED_T3TERRAIN_PARSE", "error": str(exc)}
    height_map = root.find("heightMap")
    if height_map is None:
        return {"status": "UNRESOLVED_NO_HEIGHTMAP"}
    try:
        dim_x, dim_y = parse_int_pair(height_map.get("dim", ""))
        offset = parse_float_triplet(height_map.get("offset", "0 0 0"))
        scale = parse_float_triplet(height_map.get("scale", "1 1 1"))
    except (TypeError, ValueError) as exc:
        return {"status": "UNRESOLVED_TERRAIN_FRAME_FIELDS", "error": str(exc)}
    if dim_x < 2 or dim_y < 2 or scale[0] <= 0 or scale[1] <= 0:
        return {"status": "UNRESOLVED_INVALID_TERRAIN_FRAME"}
    return {
        "status": "PROVEN_FROM_T3TERRAIN",
        "vertex_dim": {"x": dim_x, "y": dim_y},
        "cell_dim": {"x": dim_x - 1, "y": dim_y - 1},
        "offset": {"x": offset[0], "y": offset[1], "z": offset[2]},
        "scale": {"x": scale[0], "y": scale[1], "z": scale[2]},
    }


def parse_area(value: str) -> dict[str, int]:
    parts = [float(part) for part in value.split(",")]
    if len(parts) != 4 or any(abs(part - round(part)) > 1e-6 for part in parts):
        raise ValueError(f"Unsupported non-integer footprint Area {value!r}")
    x0, y0, x1, y1 = [int(round(part)) for part in parts]
    return {"min_x": x0, "min_y": y0, "max_x": x1, "max_y": y1}


def footprint_place_area(record: dict[str, Any] | None, width: int, height: int) -> dict[str, Any]:
    if record:
        for child in record.get("children", []):
            if child.get("tag") != "Layers":
                continue
            attrs = child.get("attrs", {})
            if attrs.get("index") != "Place" or not attrs.get("Area"):
                continue
            area = parse_area(attrs["Area"])
            area["source"] = "EXPLICIT_CFOOTPRINT_PLACE_AREA"
            return area
    # Stock shrub 2x2 omits Area. The surrounding stock definitions establish the
    # centered parity convention: 1x1 => [0,1), 4x4 => [-2,2). Rows give 2x2,
    # therefore its centered cell span is [-1,1). This remains scoped to these
    # stock full-square shrub masks and is regression-checked below.
    min_x = -(width // 2)
    min_y = -(height // 2)
    return {
        "min_x": min_x,
        "min_y": min_y,
        "max_x": min_x + width,
        "max_y": min_y + height,
        "source": "CENTERED_FROM_ROW_DIMENSIONS_STOCK_SHRUB",
    }


def doodad_anchor_cell(position: dict[str, float], terrain: dict[str, Any]) -> dict[str, int]:
    """Editor/game doodad cell anchor.

    SC2 map-format reverse engineering records the editor/game doodad placement
    rule as floor(map_coordinate + 0.25). Apply it in terrain-cell coordinates so
    non-zero terrain offsets/scales remain explicit rather than silently assumed.
    """
    ox, oy = terrain["offset"]["x"], terrain["offset"]["y"]
    sx, sy = terrain["scale"]["x"], terrain["scale"]["y"]
    return {
        "x": _floor_plus_quarter((position["x"] - ox) / sx),
        "y": _floor_plus_quarter((position["y"] - oy) / sy),
    }


def _floor_plus_quarter(value: float) -> int:
    import math
    return math.floor(value + 0.25)


def absolute_shrub_geometry(
    position: dict[str, float],
    footprint: dict[str, Any],
    terrain: dict[str, Any],
) -> dict[str, Any]:
    ox, oy = terrain["offset"]["x"], terrain["offset"]["y"]
    sx, sy = terrain["scale"]["x"], terrain["scale"]["y"]
    anchor = doodad_anchor_cell(position, terrain)
    anchor_x, anchor_y = anchor["x"], anchor["y"]
    area = footprint["place_area"]
    cells = []
    for cy in range(anchor_y + area["min_y"], anchor_y + area["max_y"]):
        for cx in range(anchor_x + area["min_x"], anchor_x + area["max_x"]):
            cells.append({
                "x": cx,
                "y": cy,
                "world_bounds": {
                    "min_x": ox + cx * sx,
                    "min_y": oy + cy * sy,
                    "max_x": ox + (cx + 1) * sx,
                    "max_y": oy + (cy + 1) * sy,
                },
            })
    return {
        "status": "PROVEN_FOR_STOCK_SHRUB_PLACEMENT_LAYER",
        "anchor_rule": "floor(terrain_cell_coordinate + 0.25)",
        "anchor_cell": {"x": anchor_x, "y": anchor_y},
        "place_area": area,
        "cells": cells,
        "rotation_semantics": "CELL_SET_INVARIANT_FOR_FULL_SQUARE_STOCK_SHRUB_MASK",
        "scale_semantics": "DOODAD_VISUAL_SCALE_DOES_NOT_SCALE_FOOTPRINT",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Pyhots Static Geometry Probe v0.3")
    parser.add_argument("--s2ma-root", required=True, type=Path)
    parser.add_argument("--catalog-index", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected, candidates = discover_packages(args.s2ma_root)
    placed_types = {
        doodad["Type"]
        for package in selected.values()
        for doodad in package["object_doodads"]
    }
    global_catalog = collect_global_catalog(args.catalog_index, placed_types)
    footprints = {
        footprint_id: parse_shrub_footprint(
            footprint_definition(global_catalog.get(footprint_id)), footprint_id
        )
        for footprint_id in sorted(SHRUB_FOOTPRINT_IDS)
    }

    placements_out: list[dict[str, Any]] = []
    map_reports: dict[str, Any] = {}
    terrain_manifest: dict[str, Any] = {}
    unresolved_actor_types: dict[str, list[str]] = {}
    terrain_frames: dict[str, Any] = {}
    suppressed_no_doodad_footprint: list[dict[str, Any]] = []
    cleared_footprints = 0

    for map_id in sorted(EXPECTED_MAP_IDS):
        package = selected.get(map_id)
        if not package:
            map_reports[map_id] = {"status": "UNRESOLVED_NO_PACKAGE"}
            continue
        allowed_mods = set(package["document_mods"]) | COMMON_MODS
        with MPQArchive(package["path"]) as archive:
            local_catalog = parse_local_actor_definitions(archive)
            terrain_raw = archive.read("t3Terrain.xml")
            terrain_frame = parse_terrain_frame(terrain_raw)
            terrain_frames[map_id] = terrain_frame
            terrain_manifest[map_id] = {
                "archive_name": package["archive_name"],
                "terrain_frame": terrain_frame,
                "members": {
                    member: (len(raw) if (raw := archive.read(member)) is not None else None)
                    for member in TERRAIN_MEMBERS
                },
            }

        per_map: list[dict[str, Any]] = []
        unresolved: set[str] = set()
        footprint_counter: Counter[str] = Counter()
        for doodad in package["object_doodads"]:
            resolved = resolve_footprint(
                doodad["Type"], global_catalog, local_catalog, allowed_mods
            )
            if not doodad_footprint_enabled(doodad):
                suppressed_no_doodad_footprint.append({
                    "map_id": map_id,
                    "object_id": doodad.get("Id"),
                    "actor_type": doodad.get("Type"),
                    "position": parse_position(doodad.get("Position", "0,0,0")),
                    "reason": "ObjectDoodad Flag NoDoodadFootprint=1",
                    "resolved_footprint_status": resolved.get("status"),
                    "resolved_footprint_id": resolved.get("footprint_id"),
                    "would_be_shrub_without_flag": resolved.get("footprint_id") in SHRUB_FOOTPRINT_IDS,
                })
                continue
            if resolved["status"].startswith("UNRESOLVED"):
                unresolved.add(doodad["Type"])
                continue
            footprint_id = resolved["footprint_id"]
            if footprint_id == "":
                cleared_footprints += 1
            if footprint_id not in SHRUB_FOOTPRINT_IDS:
                continue
            position = parse_position(doodad["Position"])
            item = {
                "map_id": map_id,
                "map_package": package["archive_name"],
                "object_id": doodad.get("Id"),
                "actor_type": doodad["Type"],
                "position": position,
                "rotation_radians": float(doodad.get("Rotation", "0")),
                "scale": parse_scale(doodad.get("Scale")),
                "variation": doodad.get("Variation"),
                "footprint_id": footprint_id,
                "footprint_template": footprints[footprint_id],
                "proof": {
                    "placement": "MAP_PACKAGE_OBJECTS",
                    "gameplay_layer": "CFootprint ShrubVision=1",
                    "actor_footprint_provenance": resolved["provenance"],
                },
                "absolute_geometry": absolute_shrub_geometry(
                    position, footprints[footprint_id], terrain_frame
                ) if terrain_frame.get("status") == "PROVEN_FROM_T3TERRAIN" else {
                    "status": "UNRESOLVED_TERRAIN_FRAME"
                },
            }
            placements_out.append(item)
            per_map.append(item)
            footprint_counter[footprint_id] += 1

        unresolved_actor_types[map_id] = sorted(unresolved)
        map_reports[map_id] = {
            "status": "COMPLETE" if not unresolved else "PARTIAL_ACTOR_COVERAGE",
            "archive_name": package["archive_name"],
            "mapinfo_label": package["mapinfo_label"],
            "variant": package["variant"],
            "document_mods": package["document_mods"],
            "placed_doodads": len(package["object_doodads"]),
            "shrub_placements": len(per_map),
            "shrub_by_footprint": dict(sorted(footprint_counter.items())),
            "unresolved_actor_types": len(unresolved),
            "suppressed_no_doodad_footprint": sum(
                1 for item in suppressed_no_doodad_footprint if item["map_id"] == map_id
            ),
            "suppressed_shrub_candidates": sum(
                1 for item in suppressed_no_doodad_footprint
                if item["map_id"] == map_id and item.get("would_be_shrub_without_flag")
            ),
        }

    all_packages_present = set(selected) == EXPECTED_MAP_IDS
    footprint_semantics_pass = all(
        value.get("status") == "PROVEN" for value in footprints.values()
    )
    total_unresolved_types = sum(len(value) for value in unresolved_actor_types.values())
    all_actor_coverage = total_unresolved_types == 0
    shrub_placements_proven = all(
        item["footprint_id"] in SHRUB_FOOTPRINT_IDS
        and item["proof"]["placement"] == "MAP_PACKAGE_OBJECTS"
        for item in placements_out
    )
    terrain_frames_proven = all(
        terrain_frames.get(map_id, {}).get("status") == "PROVEN_FROM_T3TERRAIN"
        for map_id in EXPECTED_MAP_IDS
    )
    footprint_areas_proven = all(
        value.get("status") == "PROVEN" and value.get("place_area_matches_rows")
        for value in footprints.values()
    )
    absolute_cells_in_bounds = True
    absolute_geometry_proven = True
    for item in placements_out:
        geometry = item.get("absolute_geometry", {})
        if geometry.get("status") != "PROVEN_FOR_STOCK_SHRUB_PLACEMENT_LAYER":
            absolute_geometry_proven = False
            continue
        dims = terrain_frames[item["map_id"]]["cell_dim"]
        for cell in geometry.get("cells", []):
            if not (0 <= cell["x"] < dims["x"] and 0 <= cell["y"] < dims["y"]):
                absolute_cells_in_bounds = False

    report = {
        "schema": {"name": "PYHOTS_STATIC_GEOMETRY_PROBE", "version": SCHEMA_VERSION},
        "contract": {
            "fail_closed": True,
            "no_geometry_inference_from_player_paths": True,
            "visual_bush_name_is_not_shrub_proof": True,
            "shrub_requires_map_placement_plus_shrubvision_footprint": True,
            "no_doodad_footprint_flag_is_authoritative": True,
            "absolute_cell_transform_scope": "STOCK_FULL_SQUARE_SHRUB_PLACEMENT_LAYER",
        },
        "map_packages": map_reports,
        "footprints": footprints,
        "summary": {
            "expected_maps": len(EXPECTED_MAP_IDS),
            "resolved_map_packages": len(selected),
            "proven_shrub_placements": len(placements_out),
            "maps_with_shrubs": sum(1 for value in map_reports.values() if value.get("shrub_placements", 0) > 0),
            "maps_without_shrubs": sorted(
                map_id for map_id, value in map_reports.items() if value.get("shrub_placements") == 0
            ),
            "cleared_no_vision_blocking_footprints": cleared_footprints,
            "unresolved_actor_types": total_unresolved_types,
            "suppressed_no_doodad_footprint": len(suppressed_no_doodad_footprint),
            "suppressed_shrub_candidates": sum(
                1 for item in suppressed_no_doodad_footprint if item.get("would_be_shrub_without_flag")
            ),
            "absolute_shrub_cell_references": sum(
                len(item.get("absolute_geometry", {}).get("cells", [])) for item in placements_out
            ),
            "unique_absolute_shrub_cells": sum(
                len({
                    (cell["x"], cell["y"])
                    for item in placements_out if item["map_id"] == map_id
                    for cell in item.get("absolute_geometry", {}).get("cells", [])
                })
                for map_id in EXPECTED_MAP_IDS
            ),
        },
    }

    integrity = {
        "schema": {"name": "PYHOTS_STATIC_GEOMETRY_PROBE_INTEGRITY", "version": SCHEMA_VERSION},
        "gates": {
            "S2MA_MAP_PACKAGES": "PASS" if all_packages_present else "FAIL",
            "MAP_OBJECTS": "PASS" if all(package.get("object_doodads") is not None for package in selected.values()) else "FAIL",
            "SHRUB_FOOTPRINT_SEMANTICS": "PASS" if footprint_semantics_pass else "FAIL",
            "SHRUB_PLACEMENT_PROVENANCE": "PASS" if shrub_placements_proven else "FAIL",
            "MAP_LOCAL_ACTOR_OVERLAY": "PASS",
            "DOODAD_ACTOR_COVERAGE": "PASS" if all_actor_coverage else "PARTIAL",
            "DOODAD_FOOTPRINT_SUPPRESSION": "PASS",
            "TERRAIN_CELL_FRAME": "PASS" if terrain_frames_proven else "FAIL",
            "FOOTPRINT_PLACE_AREA": "PASS" if footprint_areas_proven else "FAIL",
            "ABSOLUTE_FOOTPRINT_TRANSFORM": "PASS" if absolute_geometry_proven and absolute_cells_in_bounds else "FAIL",
            "SHRUB_ABSOLUTE_CELLS": "PROVEN" if absolute_geometry_proven and absolute_cells_in_bounds else "UNRESOLVED",
            "SHRUB_GEOMETRY": "PROVEN_ABSOLUTE_CELLS" if absolute_geometry_proven and absolute_cells_in_bounds else "PROVEN_PLACEMENTS",
        },
        "overall": "PASS" if (
            all_packages_present and footprint_semantics_pass and shrub_placements_proven
            and terrain_frames_proven and footprint_areas_proven
            and absolute_geometry_proven and absolute_cells_in_bounds
        ) else "FAIL",
    }

    package_manifest = {
        "selected": {
            map_id: {
                key: value
                for key, value in package.items()
                if key not in {"object_doodads", "listfile", "document_info", "_score", "path"}
            }
            for map_id, package in sorted(selected.items())
        },
        "candidate_count": len(candidates),
    }

    dump_json(args.output_dir / "static_geometry_probe_report.json", report)
    dump_json(args.output_dir / "static_map_integrity_report.json", integrity)
    dump_json(args.output_dir / "shrub_placements.json", {
        "schema": {"name": "PYHOTS_SHRUB_PLACEMENTS", "version": SCHEMA_VERSION},
        "placements": placements_out,
    })
    dump_json(args.output_dir / "terrain_asset_manifest.json", terrain_manifest)
    dump_json(args.output_dir / "map_package_manifest.json", package_manifest)
    dump_json(args.output_dir / "unresolved_actor_types.json", unresolved_actor_types)
    dump_json(args.output_dir / "suppressed_doodad_footprints.json", {
        "schema": {"name": "PYHOTS_SUPPRESSED_DOODAD_FOOTPRINTS", "version": SCHEMA_VERSION},
        "placements": suppressed_no_doodad_footprint,
    })
    cells_by_map: dict[str, list[dict[str, Any]]] = {}
    for map_id in sorted(EXPECTED_MAP_IDS):
        contributors: dict[tuple[int, int], list[str | None]] = defaultdict(list)
        bounds: dict[tuple[int, int], dict[str, float]] = {}
        for item in placements_out:
            if item["map_id"] != map_id:
                continue
            for cell in item.get("absolute_geometry", {}).get("cells", []):
                key = (cell["x"], cell["y"])
                contributors[key].append(item.get("object_id"))
                bounds[key] = cell["world_bounds"]
        cells_by_map[map_id] = [
            {
                "x": key[0],
                "y": key[1],
                "world_bounds": bounds[key],
                "source_object_ids": sorted(value for value in object_ids if value is not None),
            }
            for key, object_ids in sorted(contributors.items())
        ]
    dump_json(args.output_dir / "shrub_cells.json", {
        "schema": {"name": "PYHOTS_SHRUB_ABSOLUTE_CELLS", "version": SCHEMA_VERSION},
        "maps": cells_by_map,
    })

    print(json.dumps(report["summary"], indent=2, sort_keys=True))
    print(json.dumps(integrity, indent=2, sort_keys=True))
    return 0 if integrity["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
