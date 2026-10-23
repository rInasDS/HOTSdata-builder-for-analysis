from __future__ import annotations

from collections import Counter
from fact_utils import load, save, index_by_record_key, ability_records
from spatial_game_data_semantics import enrich_spatial_models


RANK = {"HIGH": 3, "MEDIUM": 2, "LOW": 1, None: 0}


def vals(facts, kind):
    return [x for x in facts if x.get("kind") == kind]


def best_fact(facts, kinds, min_conf=None):
    if isinstance(kinds, str):
        kinds = {kinds}
    else:
        kinds = set(kinds)

    min_rank = RANK.get(min_conf, 0)
    rows = [
        x for x in facts
        if x.get("kind") in kinds
        and isinstance(x.get("value"), (int, float))
        and RANK.get(x.get("confidence"), 0) >= min_rank
    ]
    if not rows:
        return None
    rows.sort(
        key=lambda x: (
            RANK.get(x.get("confidence"), 0),
            -((x.get("source") or {}).get("graph_depth") or 0),
        ),
        reverse=True,
    )
    return rows[0]


def best_numeric(facts, kinds, min_conf=None):
    f = best_fact(facts, kinds, min_conf)
    return f.get("value") if f else None


def strong_effect_radius(facts):
    for f in facts:
        if f.get("kind") != "RADIUS" or f.get("confidence") != "HIGH":
            continue
        src = f.get("source") or {}
        if src.get("xml_type") in {"CEffectEnumArea", "CEffectSearch"}:
            return f.get("value")
    return None


def actor_width(facts):
    return best_numeric(facts, "WIDTH", "MEDIUM")


def derive_shape(semantic_type, targeting, delivery, area_facts):
    radius = strong_effect_radius(area_facts)
    width = actor_width(area_facts)
    arc = best_numeric(area_facts, "ARC", "MEDIUM")

    if semantic_type == "AOE" and radius is not None:
        return "CIRCLE", "HIGH", "AOE_EFFECT_RADIUS"

    if semantic_type == "PROJECTILE":
        if width is not None:
            return "LINE", "MEDIUM", "PROJECTILE_WIDTH"
        return "PROJECTILE_PATH", "MEDIUM", "PROJECTILE_PATH"

    if semantic_type in {"LINE", "BEAM"}:
        if width is not None:
            return "LINE", "MEDIUM", "LINE_WIDTH"
        return "LINE_PATH", "LOW", "LINE_SEMANTIC_WIDTH_MISSING"

    if semantic_type == "CONE":
        if arc is not None:
            return "CONE", "MEDIUM", "CONE_ARC"
        return "CONE", "LOW", "CONE_SEMANTIC_ARC_MISSING"

    if semantic_type == "MOVEMENT":
        return "MOVEMENT_PATH", "MEDIUM", "MOVEMENT_SEMANTIC"

    if semantic_type in {"POINT_TARGET", "UNIT_TARGET", "TARGETED_HEAL"}:
        return "POINT", "MEDIUM", "TARGET_POINT_MODEL"

    if semantic_type in {
        "SELF", "SELF_BUFF", "SELF_EFFECT", "TOGGLE", "TRANSFORM", "CHANNEL",
        "PASSIVE", "STATE", "CONTROL", "CANCEL",
    }:
        return "NONE", "HIGH", "NO_PRIMARY_SHAPE_REQUIRED"

    return "UNKNOWN", "LOW", "NO_SHAPE_RULE"


def completeness_for(
    semantic_type,
    targeting,
    delivery,
    cast_range,
    shape,
    radius,
    width,
    arc,
    length,
):
    if semantic_type in {"PASSIVE", "STATE", "CONTROL", "CANCEL"}:
        return "NON_SPATIAL", "HIGH", "SEMANTIC_NON_SPATIAL"

    if semantic_type in {"SELF", "SELF_BUFF", "SELF_EFFECT", "TOGGLE", "TRANSFORM"}:
        return "SPATIAL_COMPLETE", "HIGH", "SELF_NO_GEOMETRY_REQUIRED"

    if semantic_type == "CHANNEL":
        return "SPATIAL_COMPLETE", "MEDIUM", "CHANNEL_SEMANTICS_SUFFICIENT"

    if semantic_type in {"UNIT_TARGET", "TARGETED_HEAL", "POINT_TARGET"}:
        if targeting == "POINT" and cast_range is not None:
            return "SPATIAL_COMPLETE", "HIGH", "TARGET_AND_RANGE_KNOWN"
        if targeting == "POINT":
            return "SPATIAL_PARTIAL", "MEDIUM", "TARGET_KNOWN_RANGE_MISSING"
        return "SPATIAL_PARTIAL", "LOW", "TARGETING_WEAK"

    if semantic_type == "AOE":
        if radius is not None:
            return "SPATIAL_COMPLETE", "HIGH", "AOE_RADIUS_KNOWN"
        return "SPATIAL_PARTIAL", "LOW", "AOE_RADIUS_MISSING"

    if semantic_type == "PROJECTILE":
        if delivery == "PROJECTILE" and cast_range is not None and width is not None:
            return "SPATIAL_COMPLETE", "HIGH", "PROJECTILE_RANGE_WIDTH_KNOWN"
        return "SPATIAL_PARTIAL", "MEDIUM", "PROJECTILE_PARTIAL"

    if semantic_type in {"LINE", "BEAM"}:
        if width is not None and (cast_range is not None or length is not None):
            return "SPATIAL_COMPLETE", "MEDIUM", "LINE_DIMENSIONS_KNOWN"
        return "SPATIAL_PARTIAL", "MEDIUM", "LINE_DIMENSIONS_PARTIAL"

    if semantic_type == "CONE":
        if arc is not None and (cast_range is not None or length is not None):
            return "SPATIAL_COMPLETE", "MEDIUM", "CONE_GEOMETRY_KNOWN"
        return "SPATIAL_PARTIAL", "MEDIUM", "CONE_GEOMETRY_PARTIAL"

    if semantic_type == "MOVEMENT":
        if cast_range is not None or length is not None:
            return "SPATIAL_COMPLETE", "MEDIUM", "MOVEMENT_DISTANCE_KNOWN"
        return "SPATIAL_PARTIAL", "MEDIUM", "MOVEMENT_DISTANCE_MISSING"

    if semantic_type in {"SUMMON", "RECAST_TRIGGER", "OTHER"}:
        return "SPATIAL_PARTIAL", "LOW", "SPECIAL_SEMANTIC_PARTIAL"

    return "SPATIAL_PARTIAL", "LOW", "DEFAULT_PARTIAL"


def run_spatial_model(
    herodata_path,
    catalog_facts_path,
    semantic_path,
    targeting_path,
    projectile_path,
    area_path,
    output_path,
    report_path,
    replay_catalog_path=None,
    typed_graph_path=None,
):
    hd = load(herodata_path)
    roles = index_by_record_key(load(catalog_facts_path))
    semantics = index_by_record_key(load(semantic_path))
    t_idx = index_by_record_key(load(targeting_path))
    p_idx = index_by_record_key(load(projectile_path))
    a_idx = index_by_record_key(load(area_path))

    heroes = {}
    counts = Counter()
    statuses = Counter()
    shapes = Counter()
    semantic_counts = Counter()
    reasons = Counter()
    review = []

    for hero, ability in ability_records(hd):
        counts["abilities_total"] += 1
        rk = ability.get("record_key")

        role = roles.get(hero, {}).get(rk, {}).get("ability_role", "UNRESOLVED")
        sem = semantics.get(hero, {}).get(rk, {})
        semantic_type = sem.get("semantic_type", "OTHER")
        semantic_counts[semantic_type] += 1

        tf = t_idx.get(hero, {}).get(rk, {}).get("facts", [])
        pf = p_idx.get(hero, {}).get(rk, {}).get("facts", [])
        af = a_idx.get(hero, {}).get(rk, {}).get("facts", [])

        targeting = "UNKNOWN"
        for f in tf:
            if f.get("kind") == "TARGETING":
                raw = f.get("value")
                targeting = (
                    "POINT" if raw == "POINT"
                    else "SELF_OR_INSTANT" if raw == "SELF_OR_INSTANT"
                    else "OTHER"
                )
                break

        delivery = "PROJECTILE" if vals(pf, "DELIVERY") else "INSTANT"

        cast_range = best_numeric(tf, "CAST_RANGE")
        radius = strong_effect_radius(af)
        width = actor_width(af)
        length = best_numeric(af, "LENGTH")
        arc = best_numeric(af, "ARC")
        speed = best_numeric(pf, {"MOVER_SPEED", "MOVER_MAXSPEED"})

        shape, shape_conf, shape_reason = derive_shape(
            semantic_type, targeting, delivery, af
        )

        status, confidence, status_reason = completeness_for(
            semantic_type, targeting, delivery, cast_range, shape,
            radius, width, arc, length
        )

        statuses[status] += 1
        shapes[shape] += 1
        reasons[status_reason] += 1

        if status == "SPATIAL_COMPLETE":
            counts["spatial_complete"] += 1
        elif status == "SPATIAL_PARTIAL":
            counts["spatial_partial"] += 1
        elif status == "SPATIAL_UNRESOLVED":
            counts["spatial_unresolved"] += 1
        elif status == "NON_SPATIAL":
            counts["non_spatial"] += 1

        if status in {"SPATIAL_UNRESOLVED"}:
            review.append({
                "hero": hero,
                "record_key": rk,
                "ability_id": ability.get("id"),
                "name": ability.get("name"),
                "semantic_type": semantic_type,
                "reason": status_reason,
            })

        heroes.setdefault(hero, []).append({
            "record_key": rk,
            "ability_id": ability.get("id"),
            "name": ability.get("name"),
            "ability_role": role,
            "semantic_type": semantic_type,
            "semantic_reason": sem.get("semantic_reason"),
            "semantic_confidence": sem.get("semantic_confidence"),
            "spatial_status": status,
            "status_reason": status_reason,
            "targeting": targeting,
            "delivery": delivery,
            "shape": shape,
            "shape_reason": shape_reason,
            "cast_range": cast_range,
            "travel_range": length if delivery == "PROJECTILE" else None,
            "radius": radius,
            "width": width,
            "length": length,
            "arc": arc,
            "speed": speed,
            "confidence": confidence,
            "facts_used": {
                "targeting": tf,
                "projectile": pf,
                "area_geometry": af,
            },
        })

    data = {
        "metadata": {
            "builder": "spatial_model_builder_v3_10",
            "schema_version": "3.10",
        },
        "heroes": heroes,
    }

    game_data_semantics = {"summary": {}, "examples": []}
    if replay_catalog_path and typed_graph_path:
        game_data_semantics = enrich_spatial_models(
            data, hd, load(replay_catalog_path), load(typed_graph_path)
        )

        # Recompute status/shape counters after generic GameData enrichment.
        counts["spatial_complete"] = 0
        counts["spatial_partial"] = 0
        counts["spatial_unresolved"] = 0
        counts["non_spatial"] = 0
        statuses.clear(); shapes.clear(); reasons.clear()
        for _hero_rows in data.get("heroes", {}).values():
            for _row in _hero_rows:
                _status = _row.get("spatial_status")
                statuses[_status] += 1
                shapes[_row.get("shape")] += 1
                reasons[_row.get("status_reason")] += 1
                if _status == "SPATIAL_COMPLETE": counts["spatial_complete"] += 1
                elif _status == "SPATIAL_PARTIAL": counts["spatial_partial"] += 1
                elif _status == "SPATIAL_UNRESOLVED": counts["spatial_unresolved"] += 1
                elif _status == "NON_SPATIAL": counts["non_spatial"] += 1

    report = {
        "summary": {
            "abilities_total": counts["abilities_total"],
            "spatial_complete": counts["spatial_complete"],
            "spatial_partial": counts["spatial_partial"],
            "spatial_unresolved": counts["spatial_unresolved"],
            "non_spatial": counts["non_spatial"],
        },
        "semantic_types": dict(semantic_counts),
        "spatial_status": dict(statuses),
        "shape": dict(shapes),
        "status_reasons": dict(reasons),
        "review": review,
        "game_data_spatial_semantics": game_data_semantics,
    }
    save(output_path, data)
    save(report_path, report)
    return report
