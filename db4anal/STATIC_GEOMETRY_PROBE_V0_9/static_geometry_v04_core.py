from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

import static_geometry_v03_core as core
from mpq_reader import MPQArchive

# Backward-compatible exports for v0.2/v0.3 regression tests.
mapinfo_label = core.mapinfo_label
parse_position = core.parse_position
parse_scale = core.parse_scale
parse_object_doodads = core.parse_object_doodads
doodad_footprint_enabled = core.doodad_footprint_enabled
parse_terrain_frame = core.parse_terrain_frame
footprint_place_area = core.footprint_place_area
absolute_shrub_geometry = core.absolute_shrub_geometry

SCHEMA_VERSION = "0.4"

CONVEYOR_UNIT_TYPES = {
    "ConveyorBelt3x3East": ("EAST", 1.0, 0.0),
    "ConveyorBelt3x3West": ("WEST", -1.0, 0.0),
    "ConveyorBelt3x3North": ("NORTH", 0.0, 1.0),
    "ConveyorBelt3x3South": ("SOUTH", 0.0, -1.0),
}
CONVEYOR_VISUAL_ACTORS = {
    "Storm_Doodad_OWVol_ConveyorBelt",
    "Storm_Doodad_VolVilBrawl_ConveyorBelt",
}
SNOWDRIFT_UNIT_TYPES = {
    "SnowDriftSmall": "SnowDriftSmallSearchArea",
    "SnowDriftMedium": "SnowDriftMediumSearchArea",
    "SnowDriftLarge": "SnowDriftLargeSearchArea",
}


def dump_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_object_elements(raw: bytes) -> dict[str, list[dict[str, Any]]]:
    root = ET.fromstring(raw)
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tag in ("ObjectUnit", "ObjectDoodad", "ObjectPoint"):
        for element in root.iter(tag):
            item = dict(element.attrib)
            flags: dict[str, str] = {}
            for child in list(element):
                if child.tag == "Flag" and child.get("Index") is not None:
                    flags[child.get("Index")] = child.get("Value", "")
            if flags:
                item["_flags"] = flags
            result[tag].append(item)
    return dict(result)


def pick_record(catalog: dict[str, list[dict[str, Any]]], object_id: str, xml_type: str, allowed_mods: set[str]) -> dict[str, Any] | None:
    records = [
        record for record in catalog.get(object_id, [])
        if record.get("xml_type") == xml_type and (record.get("mod") or "").lower() in allowed_mods
    ]
    return sorted(records, key=lambda r: r.get("order", 0))[-1] if records else None


def child_value(record: dict[str, Any] | None, tag: str, attr: str = "value") -> str | None:
    if not record:
        return None
    value = None
    for child in record.get("children", []):
        if child.get("tag") == tag and child.get("attrs", {}).get(attr) is not None:
            value = child["attrs"][attr]
    return value


def descendant_value(record: dict[str, Any] | None, path: list[str], attr: str = "value") -> str | None:
    if not record:
        return None
    value = None
    for desc in record.get("descendants", []):
        if desc.get("path") == path and desc.get("attrs", {}).get(attr) is not None:
            value = desc["attrs"][attr]
    return value


def descendant_attrs(record: dict[str, Any] | None, path: list[str]) -> dict[str, Any] | None:
    if not record:
        return None
    value = None
    for desc in record.get("descendants", []):
        if desc.get("path") == path:
            value = dict(desc.get("attrs", {}))
    return value


def rect_from_center(x: float, y: float, width: float, height: float) -> dict[str, float]:
    return {
        "min_x": x - width / 2.0,
        "min_y": y - height / 2.0,
        "max_x": x + width / 2.0,
        "max_y": y + height / 2.0,
    }


def rectangles_touch(a: dict[str, float], b: dict[str, float], eps: float = 1e-4) -> bool:
    return not (
        a["max_x"] < b["min_x"] - eps or b["max_x"] < a["min_x"] - eps
        or a["max_y"] < b["min_y"] - eps or b["max_y"] < a["min_y"] - eps
    )


def point_in_rect(x: float, y: float, rect: dict[str, float], eps: float = 1e-6) -> bool:
    return rect["min_x"] - eps <= x <= rect["max_x"] + eps and rect["min_y"] - eps <= y <= rect["max_y"] + eps


def connected_components(items: list[dict[str, Any]]) -> list[list[int]]:
    adj: dict[int, set[int]] = defaultdict(set)
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if rectangles_touch(items[i]["rectangle"], items[j]["rectangle"]):
                adj[i].add(j)
                adj[j].add(i)
    seen: set[int] = set()
    comps: list[list[int]] = []
    for i in range(len(items)):
        if i in seen:
            continue
        q = deque([i]); seen.add(i); comp = []
        while q:
            u = q.popleft(); comp.append(u)
            for v in sorted(adj[u]):
                if v not in seen:
                    seen.add(v); q.append(v)
        comps.append(sorted(comp))
    return comps


def build_catalog(catalog_index: Path) -> dict[str, list[dict[str, Any]]]:
    ids = {
        "ConveyorBeltParent", "ConveyorBeltSearchParent", "ConveyorBeltPushSetParent",
        "ConveyorBeltEastUnit", "ConveyorBeltWestUnit", "ConveyorBeltNorthUnit", "ConveyorBeltSouthUnit",
        "ConveyorBeltEastSearchArea3x3", "ConveyorBeltWestSearchArea3x3", "ConveyorBeltNorthSearchArea3x3", "ConveyorBeltSouthSearchArea3x3",
        "ConveyorBeltEastPushSet", "ConveyorBeltWestPushSet", "ConveyorBeltNorthPushSet", "ConveyorBeltSouthPushSet",
        "ConveyorBeltEastApplyMoverBehavior", "ConveyorBeltWestApplyMoverBehavior", "ConveyorBeltNorthApplyMoverBehavior", "ConveyorBeltSouthApplyMoverBehavior",
        "ConveyorBeltEastPush", "ConveyorBeltWestPush", "ConveyorBeltNorthPush", "ConveyorBeltSouthPush",
        "ConveyorBeltEastApplyKinetic", "ConveyorBeltWestApplyKinetic", "ConveyorBeltNorthApplyKinetic", "ConveyorBeltSouthApplyKinetic",
        "ConveyorBeltPushEast", "ConveyorBeltPushWest", "ConveyorBeltPushNorth", "ConveyorBeltPushSouth",
        "ConveyorBeltTargetDoesNotHaveConveyorBeltPushImmunity", "DoesNotHaveImmuneToConveyors", "HeroGenericImmuneToConveyers",
        "ConveyorBeltApplyConveyorTrackerBehavior", "CurrentlyAffectedByConveyors",
        "SnowDriftParent", "SnowDriftSmall", "SnowDriftMedium", "SnowDriftLarge",
        "SnowDriftSmallSlowCarry", "SnowDriftMediumSlowCarry", "SnowDriftLargeSlowCarry",
        "SnowDriftSmallInitialSet", "SnowDriftMediumInitialSet", "SnowDriftLargeInitialSet",
        "SnowDriftCreatePersistentParent", "SnowDriftSmallCreatePersistent", "SnowDriftMediumCreatePersistent", "SnowDriftLargeCreatePersistent",
        "SnowDriftSmallSearchArea", "SnowDriftMediumSearchArea", "SnowDriftLargeSearchArea",
        "ApplySnowDriftSlowMonitor", "SnowDriftSlowMonitor", "SnowDriftSlowIncrementer", "IsInSnowDrift", "ApplySnowDriftSlow", "SnowDriftSlow", "StormSlowParent", "StormSlowApply",
    }
    ids |= set(CONVEYOR_UNIT_TYPES)
    return core.scan_catalog_objects(catalog_index, ids)


def conveyor_semantics(catalog: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    allowed = {"conveyorbelts.stormmod", "heroesdata.stormmod"}
    parent = pick_record(catalog, "ConveyorBeltParent", "CBehaviorBuff", allowed)
    search_parent = pick_record(catalog, "ConveyorBeltSearchParent", "CEffectEnumArea", allowed)
    push_parent = pick_record(catalog, "ConveyorBeltPushSetParent", "CEffectSet", allowed)
    period = float(child_value(parent, "Period") or "nan")
    search_filters = child_value(search_parent, "SearchFilters")
    validators = [
        child.get("attrs", {}).get("value")
        for child in (push_parent or {}).get("children", [])
        if child.get("tag") == "ValidatorArray"
    ]
    directions: dict[str, Any] = {}
    for unit_type, (direction, dx, dy) in CONVEYOR_UNIT_TYPES.items():
        behavior_id = f"ConveyorBelt{direction.title()}Unit"
        search_id = f"ConveyorBelt{direction.title()}SearchArea3x3"
        kinetic_id = f"ConveyorBeltPush{direction.title()}"
        behavior = pick_record(catalog, behavior_id, "CBehaviorBuff", allowed)
        search = pick_record(catalog, search_id, "CEffectEnumArea", allowed)
        kinetic = pick_record(catalog, kinetic_id, "CKineticTranslate", allowed)
        width = float(descendant_value(search, ["AreaArray", "RectangleWidth"]) or "nan")
        height = float(descendant_value(search, ["AreaArray", "RectangleHeight"]) or "nan")
        duration = float(child_value(kinetic, "Duration") or "nan")
        xoff = float(child_value(kinetic, "XOffset") or "0")
        yoff = float(child_value(kinetic, "YOffset") or "0")
        apply_as_velocity = child_value(kinetic, "ApplyAsVelocity")
        directions[direction] = {
            "unit_type": unit_type,
            "behavior_id": behavior_id,
            "periodic_effect": child_value(behavior, "PeriodicEffect"),
            "search_effect": search_id,
            "search_rectangle": {"width": width, "height": height},
            "kinetic_id": kinetic_id,
            "kinetic": {
                "duration": duration,
                "x_offset": xoff,
                "y_offset": yoff,
                "apply_as_velocity": apply_as_velocity,
                "derived_offset_over_duration": {
                    "x": xoff / duration if duration else None,
                    "y": yoff / duration if duration else None,
                    "status": "DERIVED_ARITHMETIC_FROM_GAMEDATA_NOT_INDEPENDENT_RUNTIME_MEASUREMENT",
                },
            },
            "direction_vector": {"x": dx, "y": dy},
            "proof": {
                "behavior_mod": behavior.get("mod") if behavior else None,
                "search_mod": search.get("mod") if search else None,
                "kinetic_mod": kinetic.get("mod") if kinetic else None,
            },
        }
    immunity = pick_record(catalog, "DoesNotHaveImmuneToConveyors", "CValidatorUnitCompareBehaviorCount", allowed)
    return {
        "status": "PROVEN_GAMEDATA_CHAIN" if all(
            math.isfinite(value["search_rectangle"]["width"]) and value["search_rectangle"]["width"] == 3.0
            and math.isfinite(value["kinetic"]["duration"])
            for value in directions.values()
        ) else "PARTIAL",
        "period_seconds": period,
        "search_filters": search_filters,
        "push_validators": validators,
        "immunity_behavior": child_value(immunity, "Behavior"),
        "directions": directions,
        "execution_chain": [
            "CUnit.BehaviorArray", "CBehaviorBuff.PeriodicEffect", "CEffectEnumArea",
            "CEffectSet", "CEffectApplyBehavior", "CBehaviorBuff.InitialEffect", "CEffectApplyKinetic", "CKineticTranslate",
        ],
    }


def mud_semantics(catalog: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    allowed = {"alteracpass.stormmod", "heroesdata.stormmod"}
    persistent_parent = pick_record(catalog, "SnowDriftCreatePersistentParent", "CEffectCreatePersistent", allowed)
    monitor = pick_record(catalog, "SnowDriftSlowMonitor", "CBehaviorBuff", allowed)
    incrementer = pick_record(catalog, "SnowDriftSlowIncrementer", "CEffectCreatePersistent", allowed)
    slow = pick_record(catalog, "SnowDriftSlow", "CBehaviorBuff", allowed)
    storm_slow = pick_record(catalog, "StormSlowParent", "CBehaviorBuff", allowed)
    period = float(child_value(incrementer, "PeriodicPeriodArray") or "nan")
    monitor_duration = float(child_value(monitor, "Duration") or "nan")
    factor = float(descendant_value(slow, ["Modification", "UnifiedMoveSpeedFactor"]) or "nan")
    max_stacks = int(child_value(slow, "MaxStackCount") or "0")
    offset = None
    if persistent_parent:
        attrs = next((c.get("attrs", {}) for c in persistent_parent.get("children", []) if c.get("tag") == "PeriodicOffsetArray"), None)
        if attrs:
            offset = {"x": float(attrs.get("X", 0)), "y": float(attrs.get("Y", 0))}
    shapes: dict[str, Any] = {}
    for unit_type, search_id in SNOWDRIFT_UNIT_TYPES.items():
        search = pick_record(catalog, search_id, "CEffectEnumArea", allowed)
        shapes[unit_type] = {
            "search_effect": search_id,
            "rectangle_width": float(descendant_value(search, ["AreaArray", "RectangleWidth"]) or "nan"),
            "rectangle_height": float(descendant_value(search, ["AreaArray", "RectangleHeight"]) or "nan"),
            "impact_location": (next((c.get("attrs", {}).get("Value") for c in (search or {}).get("children", []) if c.get("tag") == "ImpactLocation"), None)),
            "search_filters": child_value(search, "SearchFilters"),
        }
    storm_validators = [
        c.get("attrs", {}).get("value") for c in (storm_slow or {}).get("children", []) if c.get("tag") == "RemoveValidatorArray"
    ]
    return {
        "status": "PROVEN_GAMEDATA_CHAIN" if all(math.isfinite(v["rectangle_width"]) for v in shapes.values()) and math.isfinite(factor) else "PARTIAL",
        "shapes": shapes,
        "persistent_periodic_offset": offset,
        "absolute_rectangle_transform": "UNRESOLVED_ENGINE_PERIODIC_OFFSET_AND_RECTANGLE_FRAME",
        "monitor_duration_seconds": monitor_duration,
        "increment_period_seconds": period,
        "slow_behavior": "SnowDriftSlow",
        "per_stack_unified_move_speed_factor": factor,
        "max_stack_count": max_stacks,
        "derived": {
            "stack_rate_per_second": (1.0 / period) if period else None,
            "time_to_max_stacks_seconds": (max_stacks * period) if period else None,
            "max_stacked_unified_move_speed_factor": factor * max_stacks,
            "status": "DERIVED_ARITHMETIC_FROM_GAMEDATA_FIELDS",
        },
        "remove_validators": ["IsInSnowDrift"] + storm_validators,
        "execution_chain": [
            "SnowDrift CUnit", "CBehaviorBuff.InitialEffect", "CEffectCreatePersistent",
            "CEffectEnumArea", "ApplySnowDriftSlowMonitor", "SnowDriftSlowMonitor",
            "SnowDriftSlowIncrementer", "ApplySnowDriftSlow", "SnowDriftSlow",
        ],
    }


def map_objects(package: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    with MPQArchive(package["path"]) as archive:
        raw = archive.read("Objects")
        return parse_object_elements(raw or b"<Objects/>")


def build_mud_zones(selected: dict[str, dict[str, Any]], semantics: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    package = selected.get("AlteracPass")
    if not package:
        return [], {"status": "UNRESOLVED_NO_ALTERAC_PACKAGE"}
    objects = map_objects(package)
    units = [u for u in objects.get("ObjectUnit", []) if u.get("UnitType") in SNOWDRIFT_UNIT_TYPES]
    tarpits = [d for d in objects.get("ObjectDoodad", []) if d.get("Type") == "Storm_Doodad_WCAV_TarPit"]
    zones = []
    unmatched = []
    used_tar: set[str] = set()
    for unit in units:
        p = core.parse_position(unit["Position"])
        candidates = []
        for d in tarpits:
            dp = core.parse_position(d["Position"])
            dist = math.hypot(p["x"] - dp["x"], p["y"] - dp["y"])
            candidates.append((dist, d))
        dist, nearest = min(candidates, key=lambda x: x[0]) if candidates else (math.inf, None)
        if nearest is None or dist > 0.05:
            unmatched.append({"unit_id": unit.get("Id"), "nearest_distance": dist})
            visual = None
        else:
            visual = nearest; used_tar.add(nearest.get("Id"))
        shape = semantics["shapes"][unit["UnitType"]]
        zones.append({
            "map_id": "AlteracPass",
            "mechanic": "MUD_SLOW_ZONE",
            "source_unit_id": unit.get("Id"),
            "unit_type": unit["UnitType"],
            "position": p,
            "rotation_radians": float(unit.get("Rotation", "0")),
            "paired_tarpit_doodad": {
                "object_id": visual.get("Id") if visual else None,
                "type": visual.get("Type") if visual else None,
                "position": core.parse_position(visual["Position"]) if visual else None,
                "distance_to_unit": dist if visual else None,
            },
            "search_rectangle": {
                "width": shape["rectangle_width"],
                "height": shape["rectangle_height"],
                "impact_location": shape["impact_location"],
                "periodic_offset": semantics["persistent_periodic_offset"],
                "absolute_polygon_status": semantics["absolute_rectangle_transform"],
            },
            "effect": {
                "per_stack_unified_move_speed_factor": semantics["per_stack_unified_move_speed_factor"],
                "max_stack_count": semantics["max_stack_count"],
                "increment_period_seconds": semantics["increment_period_seconds"],
                "monitor_duration_seconds": semantics["monitor_duration_seconds"],
                "derived": semantics["derived"],
                "remove_validators": semantics["remove_validators"],
                "search_filters": shape["search_filters"],
            },
            "proof": {
                "placement": "MAP_PACKAGE_OBJECTUNIT",
                "visual_pair": "MAP_PACKAGE_OBJECTDOODAD_CENTER_MATCH" if visual else "UNRESOLVED",
                "semantics": "GAMEDATA_EFFECT_CHAIN",
            },
        })
    report = {
        "status": "PROVEN_PLACEMENTS_AND_EFFECT_PARAMETERS" if len(zones) == 6 and not unmatched and len(used_tar) == 6 else "PARTIAL",
        "zone_count": len(zones),
        "tarpit_doodad_count": len(tarpits),
        "paired_tarpits": len(used_tar),
        "unmatched_units": unmatched,
        "absolute_polygon_status": semantics["absolute_rectangle_transform"],
    }
    return zones, report


def build_conveyor_zones(selected: dict[str, dict[str, Any]], semantics: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    emitters: list[dict[str, Any]] = []
    map_objects_cache: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for map_id, package in selected.items():
        objects = map_objects(package); map_objects_cache[map_id] = objects
        for unit in objects.get("ObjectUnit", []):
            if unit.get("UnitType") not in CONVEYOR_UNIT_TYPES:
                continue
            direction, dx, dy = CONVEYOR_UNIT_TYPES[unit["UnitType"]]
            sem = semantics["directions"][direction]
            p = core.parse_position(unit["Position"])
            width = sem["search_rectangle"]["width"]; height = sem["search_rectangle"]["height"]
            emitters.append({
                "map_id": map_id,
                "mechanic": "CONVEYOR_FORCED_MOVEMENT_ZONE",
                "object_unit_id": unit.get("Id"),
                "unit_type": unit["UnitType"],
                "position": p,
                "rotation_radians": float(unit.get("Rotation", "0")),
                "direction": direction,
                "direction_vector": {"x": dx, "y": dy},
                "rectangle": rect_from_center(p["x"], p["y"], width, height),
                "rectangle_size": {"width": width, "height": height},
                "period_seconds": semantics["period_seconds"],
                "kinetic": sem["kinetic"],
                "target_rules": {
                    "search_filters": semantics["search_filters"],
                    "push_validators": semantics["push_validators"],
                    "immunity_behavior": semantics["immunity_behavior"],
                },
                "proof": {"placement": "MAP_PACKAGE_OBJECTUNIT", "semantics": "GAMEDATA_EFFECT_CHAIN"},
            })
    components_out: list[dict[str, Any]] = []
    for map_id in sorted({e["map_id"] for e in emitters}):
        items = [e for e in emitters if e["map_id"] == map_id]
        comps = connected_components(items)
        for ci, comp in enumerate(comps, 1):
            members = [items[i] for i in comp]
            bounds = {
                "min_x": min(m["rectangle"]["min_x"] for m in members),
                "min_y": min(m["rectangle"]["min_y"] for m in members),
                "max_x": max(m["rectangle"]["max_x"] for m in members),
                "max_y": max(m["rectangle"]["max_y"] for m in members),
            }
            components_out.append({
                "map_id": map_id,
                "component_id": f"{map_id}:conveyor:{ci}",
                "emitter_count": len(members),
                "directions": dict(Counter(m["direction"] for m in members)),
                "bounds": bounds,
                "emitter_ids": [m["object_unit_id"] for m in members],
                "status": "DERIVED_CONNECTED_COMPONENT_OF_PROVEN_3X3_SEARCH_RECTANGLES",
            })
    objects_on: list[dict[str, Any]] = []
    by_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for e in emitters: by_map[e["map_id"]].append(e)
    for map_id, zones in by_map.items():
        objects = map_objects_cache[map_id]
        for tag in ("ObjectUnit", "ObjectDoodad", "ObjectPoint"):
            for obj in objects.get(tag, []):
                if not obj.get("Position"): continue
                if tag == "ObjectUnit" and obj.get("UnitType") in CONVEYOR_UNIT_TYPES: continue
                if tag == "ObjectDoodad" and obj.get("Type") in CONVEYOR_VISUAL_ACTORS: continue
                p = core.parse_position(obj["Position"])
                matches = [z for z in zones if point_in_rect(p["x"], p["y"], z["rectangle"])]
                if not matches: continue
                objects_on.append({
                    "map_id": map_id,
                    "object_kind": tag,
                    "object_id": obj.get("Id"),
                    "object_type": obj.get("UnitType") or obj.get("Type") or obj.get("Name"),
                    "position": p,
                    "join_kind": "OBJECT_CENTER_IN_PROVEN_CONVEYOR_SEARCH_RECTANGLE",
                    "conveyor_emitter_ids": [m["object_unit_id"] for m in matches],
                    "directions": sorted({m["direction"] for m in matches}),
                })
    report = {
        "status": "PROVEN" if emitters and all(e["rectangle_size"] == {"width": 3.0, "height": 3.0} for e in emitters) else "PARTIAL",
        "maps": {
            map_id: {
                "emitters": sum(1 for e in emitters if e["map_id"] == map_id),
                "components": sum(1 for c in components_out if c["map_id"] == map_id),
                "objects_with_centers_on_conveyors": sum(1 for o in objects_on if o["map_id"] == map_id),
                "directions": dict(Counter(e["direction"] for e in emitters if e["map_id"] == map_id)),
            }
            for map_id in sorted({e["map_id"] for e in emitters})
        },
        "total_emitters": len(emitters),
        "total_components": len(components_out),
        "objects_with_centers_on_conveyors": len(objects_on),
    }
    return emitters, report, objects_on + components_out


def main() -> int:
    parser = argparse.ArgumentParser(description="Pyhots Static Geometry Probe v0.4")
    parser.add_argument("--s2ma-root", required=True, type=Path)
    parser.add_argument("--catalog-index", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)

    # Run the proven v0.3 shrub layer unchanged first.
    original_argv = sys.argv
    try:
        sys.argv = ["static_geometry_v03_core.py", "--s2ma-root", str(args.s2ma_root), "--catalog-index", str(args.catalog_index), "--output-dir", str(args.output_dir)]
        rc = core.main()
    finally:
        sys.argv = original_argv
    if rc != 0:
        return rc

    selected, _ = core.discover_packages(args.s2ma_root)
    catalog = build_catalog(args.catalog_index)
    conv_sem = conveyor_semantics(catalog)
    mud_sem = mud_semantics(catalog)
    mud_zones, mud_report = build_mud_zones(selected, mud_sem)
    conveyor_emitters, conveyor_report, conveyor_aux = build_conveyor_zones(selected, conv_sem)
    conveyor_components = [x for x in conveyor_aux if "component_id" in x]
    objects_on_conveyors = [x for x in conveyor_aux if "component_id" not in x]

    movement_report = {
        "schema": {"name": "PYHOTS_STATIC_MOVEMENT_MECHANICS", "version": SCHEMA_VERSION},
        "contract": {
            "fail_closed": True,
            "map_placement_is_geometry_proof": True,
            "gamedata_execution_chain_is_mechanic_proof": True,
            "mud_absolute_polygon_not_inferred": True,
            "object_on_conveyor_join_is_center_only": True,
        },
        "alterac_mud": {"report": mud_report, "semantics": mud_sem, "zones": mud_zones},
        "conveyors": {"report": conveyor_report, "semantics": conv_sem, "emitters": conveyor_emitters, "components": conveyor_components},
        "summary": {
            "mud_zones": len(mud_zones),
            "conveyor_emitters": len(conveyor_emitters),
            "conveyor_components": len(conveyor_components),
            "conveyor_maps": sorted({e["map_id"] for e in conveyor_emitters}),
            "objects_with_centers_on_conveyors": len(objects_on_conveyors),
        },
    }
    dump_json(args.output_dir / "movement_mechanics.json", movement_report)
    dump_json(args.output_dir / "objects_on_conveyors.json", {
        "schema": {"name": "PYHOTS_OBJECTS_ON_CONVEYORS", "version": SCHEMA_VERSION},
        "join_semantics": "OBJECT_CENTER_IN_PROVEN_CONVEYOR_SEARCH_RECTANGLE",
        "objects": objects_on_conveyors,
    })

    old_integrity = json.loads((args.output_dir / "static_map_integrity_report.json").read_text(encoding="utf-8"))
    gates = dict(old_integrity.get("gates", {}))
    gates.update({
        "ALTERAC_MUD_PLACEMENTS": "PASS" if mud_report.get("zone_count") == 6 and mud_report.get("paired_tarpits") == 6 else "FAIL",
        "ALTERAC_MUD_EFFECT_CHAIN": "PASS" if mud_sem.get("status") == "PROVEN_GAMEDATA_CHAIN" else "FAIL",
        "ALTERAC_MUD_ABSOLUTE_POLYGON": "UNRESOLVED" if mud_sem.get("absolute_rectangle_transform", "").startswith("UNRESOLVED") else "PROVEN",
        "CONVEYOR_PLACEMENTS": "PASS" if conveyor_report.get("total_emitters", 0) > 0 else "FAIL",
        "CONVEYOR_EFFECT_CHAIN": "PASS" if conv_sem.get("status") == "PROVEN_GAMEDATA_CHAIN" else "FAIL",
        "CONVEYOR_CONTINUOUS_GEOMETRY": "PROVEN" if conveyor_report.get("status") == "PROVEN" else "UNRESOLVED",
        "CONVEYOR_OBJECT_CENTER_JOIN": "PASS",
    })
    required = [
        gates.get("ALTERAC_MUD_PLACEMENTS") == "PASS",
        gates.get("ALTERAC_MUD_EFFECT_CHAIN") == "PASS",
        gates.get("CONVEYOR_PLACEMENTS") == "PASS",
        gates.get("CONVEYOR_EFFECT_CHAIN") == "PASS",
        gates.get("CONVEYOR_CONTINUOUS_GEOMETRY") == "PROVEN",
        old_integrity.get("overall") == "PASS",
    ]
    integrity = {
        "schema": {"name": "PYHOTS_STATIC_GEOMETRY_PROBE_INTEGRITY", "version": SCHEMA_VERSION},
        "gates": gates,
        "overall": "PASS" if all(required) else "FAIL",
    }
    dump_json(args.output_dir / "static_map_integrity_report.json", integrity)

    report_path = args.output_dir / "static_geometry_probe_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["schema"]["version"] = SCHEMA_VERSION
    report["movement_mechanics"] = movement_report["summary"]
    dump_json(report_path, report)

    print(json.dumps(movement_report["summary"], indent=2, sort_keys=True))
    print(json.dumps(integrity, indent=2, sort_keys=True))
    return 0 if integrity["overall"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
