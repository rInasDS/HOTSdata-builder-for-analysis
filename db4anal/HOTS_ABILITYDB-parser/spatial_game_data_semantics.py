from __future__ import annotations

import math
import re
from collections import defaultdict, deque


FARTHER_RE = re.compile(r"travels\s+([0-9]+(?:\.[0-9]+)?)%\s+farther", re.I)


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _field_rows(record, field):
    return (record.get("fields") or {}).get(field) or []


def _field_values(record, field, attr="value"):
    out = []
    for row in _field_rows(record, field):
        value = (row.get("attrs") or {}).get(attr)
        if value is not None:
            out.append(value)
    return out


def _field_number(record, field, attr="value"):
    values = _field_values(record, field, attr)
    for value in reversed(values):
        n = _num(value)
        if n is not None:
            return n
    return None


def _nested_number(record, *, tag=None, path_contains=()):
    out = []
    for fact in record.get("nested_facts") or []:
        if tag is not None and fact.get("tag") != tag:
            continue
        path = fact.get("path") or []
        if any(x not in path for x in path_contains):
            continue
        attrs = fact.get("attrs") or {}
        for key in ("value", "Value", "X", "Y"):
            if key in attrs:
                n = _num(attrs.get(key))
                if n is not None:
                    out.append(n)
                    break
    return out


def _records_by_id(records):
    out = defaultdict(list)
    for key, record in records.items():
        out[str(record.get("object_id") or "")].append((key, record))
    return out


def _ability_keys(by_id, ability_id):
    return [key for key, record in by_id.get(str(ability_id), []) if str(record.get("xml_type") or "").startswith("CAbil")]


def _execution_adjacency(edges):
    out = defaultdict(list)
    for edge in edges:
        prop = edge.get("propagation") or {}
        if not prop.get("execution"):
            continue
        source = edge.get("source_key")
        target = edge.get("target_key")
        if source and target:
            out[source].append(edge)
    return out


def _formula_adjacency(edges):
    out = defaultdict(list)
    for edge in edges:
        prop = edge.get("propagation") or {}
        if not prop.get("formula"):
            continue
        source = edge.get("source_key")
        target = edge.get("target_key")
        if source and target:
            out[source].append(edge)
    return out


def _closure(starts, adjacency, max_depth=10):
    seen = set(starts)
    q = deque((s, 0) for s in starts)
    while q:
        key, depth = q.popleft()
        if depth >= max_depth:
            continue
        for edge in adjacency.get(key, []):
            target = edge.get("target_key")
            if target and target not in seen:
                seen.add(target)
                q.append((target, depth + 1))
    return seen


def _area_radius(record):
    values = _nested_number(record, tag="Radius", path_contains=("AreaArray",))
    return values[-1] if values else None


def _parse_offsets(record):
    offsets = []
    for row in _field_rows(record, "PeriodicOffsetArray"):
        attrs = row.get("attrs") or {}
        x = _num(attrs.get("X")) or 0.0
        y = _num(attrs.get("Y")) or 0.0
        offsets.append((x, y))
    return offsets


def _uniform_radial_distance(offsets, tolerance=0.02):
    if len(offsets) < 6:
        return None
    radii = [math.hypot(x, y) for x, y in offsets]
    if not radii or min(radii) <= 0:
        return None
    if max(radii) - min(radii) > tolerance:
        return None
    value = sum(radii) / len(radii)
    # XML often approximates circle coordinates (e.g. 13.856 for sqrt(3)/2*16).
    # Normalize only when the derived radius is already extremely close to a clean millesimal.
    rounded = round(value, 3)
    return rounded if abs(value - rounded) <= 0.001 else value


def _parse_talent_modifications(record):
    """Parse ordered nested AbilityModificationArray facts into generic mutation rows."""
    rows = []
    cur = None
    for fact in record.get("nested_facts") or []:
        path = fact.get("path") or []
        if not (path and path[0] == "AbilityModificationArray" and "Modifications" in path):
            continue
        tag = str(fact.get("tag") or "")
        attrs = fact.get("attrs") or {}
        value = attrs.get("value")
        if tag == "Type":
            if cur:
                rows.append(cur)
            cur = {"type": value}
            continue
        if cur is None:
            continue
        if tag == "Catalog":
            cur["catalog"] = value
        elif tag == "Entry":
            cur["entry"] = value
        elif tag == "Field":
            cur["field"] = value
        elif tag == "Value":
            cur["value"] = _num(value)
        elif tag == "StringReplacement":
            cur["string_replacement"] = value
        elif tag == "FieldIsInteger":
            cur["field_is_integer"] = value
    if cur:
        rows.append(cur)
    return rows


def _talent_percent_farther(hero_record, ability_id):
    out = {}
    for talent in hero_record.get("talents") or []:
        if str(talent.get("ability_id") or "") != str(ability_id):
            continue
        text = " ".join(str(talent.get(k) or "") for k in ("short_text", "full_text"))
        text = re.sub(r"<[^>]+>", "", text)
        m = FARTHER_RE.search(text)
        if m:
            out[str(talent.get("talent_id") or "")] = float(m.group(1)) / 100.0
    return out


def _find_cursor_guide_inference(row, hero_record, ability_id, records, by_id, exec_adj, formula_adj):
    """Infer effective projectile range only when multiple independent facts agree.

    Guardrails:
      * direct CAbil Range is a technical sentinel (>=100),
      * CursorEffect is an area guide with a numeric radius,
      * an ability-linked talent says "travels N% farther",
      * the same talent flat-modifies guide radius,
      * and flat-modifies a damage accumulator MaxStepCount used by this ability.
    """
    if row.get("delivery") != "PROJECTILE" or row.get("targeting") != "POINT":
        return None

    starts = _ability_keys(by_id, ability_id)
    if not starts:
        return None

    abil_record = None
    for key in starts:
        r = records.get(key) or {}
        if (_field_number(r, "Range") or 0) >= 100 and _field_values(r, "CursorEffect"):
            abil_record = r
            break
    if not abil_record:
        return None

    sentinel_range = _field_number(abil_record, "Range")
    cursor_ids = _field_values(abil_record, "CursorEffect")
    hero_radius = _num(hero_record.get("radius"))
    if hero_radius is None:
        return None

    farther = _talent_percent_farther(hero_record, ability_id)
    if not farther:
        return None

    closure = _closure(starts, exec_adj, max_depth=10)
    used_accumulators = set()
    for key in closure:
        record = records.get(key) or {}
        if record.get("xml_type") != "CEffectDamage":
            continue
        for edge in formula_adj.get(key, []):
            target = edge.get("target_key")
            if target and str((records.get(target) or {}).get("xml_type") or "").startswith("CAccumulator"):
                used_accumulators.add(target)

    if not used_accumulators:
        return None

    for cursor_id in cursor_ids:
        cursor_candidates = by_id.get(str(cursor_id), [])
        for cursor_key, cursor_record in cursor_candidates:
            if cursor_record.get("xml_type") not in {"CEffectEnumArea", "CEffectSearch"}:
                continue
            guide_radius = _area_radius(cursor_record)
            if guide_radius is None or guide_radius <= hero_radius:
                continue

            base_effective = guide_radius - hero_radius

            for talent_id, pct in farther.items():
                if pct <= 0:
                    continue
                talent_candidates = [x for x in by_id.get(talent_id, []) if x[1].get("xml_type") == "CTalent"]
                for talent_key, talent_record in talent_candidates:
                    mods = _parse_talent_modifications(talent_record)
                    guide_deltas = [
                        m.get("value") for m in mods
                        if m.get("type") == "FlatModification"
                        and str(m.get("entry") or "") == str(cursor_id)
                        and str(m.get("field") or "") == "AreaArray[0].Radius"
                        and isinstance(m.get("value"), (int, float))
                    ]
                    if not guide_deltas:
                        continue

                    for guide_delta in guide_deltas:
                        inferred_range = guide_delta / pct
                        if not math.isclose(inferred_range, base_effective, rel_tol=0.0, abs_tol=0.02):
                            continue

                        accumulator_candidates = []
                        for acc_key in used_accumulators:
                            acc = records.get(acc_key) or {}
                            acc_id = str(acc.get("object_id") or "")
                            step_deltas = [
                                m.get("value") for m in mods
                                if m.get("type") == "FlatModification"
                                and str(m.get("entry") or "") == acc_id
                                and str(m.get("field") or "") == "MaxStepCount"
                                and isinstance(m.get("value"), (int, float))
                                and m.get("value") != 0
                            ]
                            if not step_deltas:
                                continue
                            base_steps = _field_number(acc, "MaxStepCount")
                            if base_steps is None:
                                continue
                            step_delta = step_deltas[0]
                            step_distance = guide_delta / step_delta
                            accumulator_candidates.append({
                                "accumulator_id": acc_id,
                                "base_step_count": base_steps,
                                "talent_step_count_delta": step_delta,
                                "damage_per_step": _field_number(acc, "MultiplierPerStep"),
                                "damage_step_distance": step_distance,
                                "max_damage_distance": base_steps * step_distance,
                            })

                        if not accumulator_candidates:
                            continue

                        # Multiple conditional damage branches may use sibling accumulators.
                        # Spatial distance is accepted only if all candidates agree.
                        first = accumulator_candidates[0]
                        if any(
                            not math.isclose(c["max_damage_distance"], first["max_damage_distance"], rel_tol=0.0, abs_tol=0.02)
                            or not math.isclose(c["damage_step_distance"], first["damage_step_distance"], rel_tol=0.0, abs_tol=0.001)
                            for c in accumulator_candidates[1:]
                        ):
                            continue

                        damage_steps = {c.get("damage_per_step") for c in accumulator_candidates}
                        damage_per_step = next(iter(damage_steps)) if len(damage_steps) == 1 else None
                        return {
                            "rule": "CURSOR_GUIDE_ACCUMULATOR_RANGE",
                            "sentinel_range": sentinel_range,
                            "cursor_effect": cursor_id,
                            "cursor_guide_radius": guide_radius,
                            "caster_radius": hero_radius,
                            "effective_range": base_effective,
                            "talent_id": talent_id,
                            "talent_percent_farther": pct,
                            "talent_guide_radius_delta": guide_delta,
                            "accumulator_ids": sorted(c["accumulator_id"] for c in accumulator_candidates),
                            "base_step_count": first["base_step_count"],
                            "talent_step_count_delta": first["talent_step_count_delta"],
                            "damage_per_step": damage_per_step,
                            "damage_step_distance": first["damage_step_distance"],
                            "max_damage_distance": first["max_damage_distance"],
                            "confidence": "HIGH",
                        }
    return None


def enrich_spatial_models(data, herodata, replay_catalog, typed_graph):
    records = replay_catalog.get("records") or {}
    by_id = _records_by_id(records)
    edges = typed_graph.get("edges") or []
    exec_adj = _execution_adjacency(edges)
    formula_adj = _formula_adjacency(edges)

    metrics = defaultdict(int)
    examples = []

    for hero, rows in data.get("heroes", {}).items():
        hero_record = (herodata.get("heroes") or {}).get(hero) or {}
        for row in rows:
            ability_id = str(row.get("ability_id") or "")
            starts = _ability_keys(by_id, ability_id)

            evidence = []

            # Generic origin rule: an instant/self CAbil originates on the caster.
            if row.get("targeting") == "SELF_OR_INSTANT":
                if row.get("cast_range") is None:
                    row["cast_range"] = 0.0
                    evidence.append({"rule": "SELF_INSTANT_ORIGIN", "cast_range": 0.0, "confidence": "HIGH"})
                    metrics["self_instant_origin"] += 1

            # Generic radial projectile pattern: persistent has >=6 equal-radius offsets,
            # and periodically executes a LaunchMissile. The first direct execution-area
            # search downstream of that launch supplies projectile hit radius.
            if row.get("delivery") == "PROJECTILE" and starts:
                closure = _closure(starts, exec_adj, max_depth=10)
                radial = None
                for key in closure:
                    record = records.get(key) or {}
                    if record.get("xml_type") != "CEffectCreatePersistent":
                        continue
                    offsets = _parse_offsets(record)
                    distance = _uniform_radial_distance(offsets)
                    if distance is None:
                        continue
                    launch_edges = [
                        e for e in exec_adj.get(key, [])
                        if str((records.get(e.get("target_key")) or {}).get("xml_type") or "") == "CEffectLaunchMissile"
                    ]
                    if not launch_edges:
                        continue
                    projectile_radius = None
                    launch_key = launch_edges[0].get("target_key")
                    for e in exec_adj.get(launch_key, []):
                        target = records.get(e.get("target_key")) or {}
                        if target.get("xml_type") in {"CEffectEnumArea", "CEffectSearch"}:
                            projectile_radius = _area_radius(target)
                            if projectile_radius is not None:
                                break
                    radial = {
                        "rule": "RADIAL_PERIODIC_PROJECTILE_GEOMETRY",
                        "persistent_id": record.get("object_id"),
                        "projectile_count": len(offsets),
                        "travel_range": distance,
                        "projectile_radius": projectile_radius,
                        "confidence": "HIGH",
                    }
                    break
                if radial:
                    row["shape"] = "RADIAL_PROJECTILES"
                    row["shape_reason"] = radial["rule"]
                    row["travel_range"] = radial["travel_range"]
                    row["radial_projectile_count"] = radial["projectile_count"]
                    if radial.get("projectile_radius") is not None:
                        row["projectile_radius"] = radial["projectile_radius"]
                    if row.get("targeting") == "SELF_OR_INSTANT":
                        row["cast_range"] = 0.0
                    row["spatial_status"] = "SPATIAL_COMPLETE"
                    row["status_reason"] = "RADIAL_PROJECTILE_GEOMETRY_KNOWN"
                    row["confidence"] = "HIGH"
                    evidence.append(radial)
                    metrics["radial_projectile_pattern"] += 1

            # Conservative cursor-guide range inference. This intentionally requires
            # exact agreement between HeroData percent text, GameData guide mutation,
            # and a damage accumulator step mutation.
            guide = _find_cursor_guide_inference(
                row, hero_record, ability_id, records, by_id, exec_adj, formula_adj
            )
            if guide:
                row["cast_range"] = guide["effective_range"]
                row["travel_range"] = guide["effective_range"]
                row["max_damage_distance"] = guide["max_damage_distance"]
                row["damage_step_distance"] = guide["damage_step_distance"]
                row["damage_step_count"] = guide["base_step_count"]
                if guide.get("damage_per_step") is not None:
                    row["damage_per_step"] = guide["damage_per_step"]
                row["cursor_guide_radius"] = guide["cursor_guide_radius"]
                row["range_model"] = guide["rule"]
                row["damage_distance_model"] = "ACCUMULATOR_DISTANCE_STEPS"
                # CursorEffect is display-only; do not expose its radius as the ability's
                # impact/area radius.
                if row.get("radius") == guide["cursor_guide_radius"]:
                    row["radius"] = None
                row["spatial_status"] = "SPATIAL_COMPLETE"
                row["status_reason"] = "PROJECTILE_RANGE_AND_DISTANCE_DAMAGE_KNOWN"
                row["confidence"] = "HIGH"
                evidence.append(guide)
                metrics["cursor_guide_accumulator_range"] += 1

            if evidence:
                row.setdefault("derived_spatial_evidence", []).extend(evidence)
                examples.append({
                    "hero": hero,
                    "ability_id": ability_id,
                    "name": row.get("name"),
                    "rules": [x.get("rule") for x in evidence],
                })

    return {
        "summary": dict(metrics),
        "examples": examples,
    }
