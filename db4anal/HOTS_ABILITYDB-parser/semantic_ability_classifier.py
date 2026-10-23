from __future__ import annotations

import re
from collections import Counter

from fact_utils import load, save, index_by_record_key, ability_records


MOVEMENT_WORDS = re.compile(
    r"\b(teleport|blink|dash|leap|jump|charge|lunge|vault|roll|rush|dive|burrow)\b",
    re.I,
)
BEAM_WORDS = re.compile(
    r"\b(beam|laser|ray|breath|disintegrate|channel a beam|stream)\b",
    re.I,
)
SUMMON_WORDS = re.compile(r"\b(summon|spawn|create a|create an|locust|turret|totem)\b", re.I)
CHANNEL_WORDS = re.compile(r"\b(channel|channeling|channeled)\b", re.I)
TOGGLE_WORDS = re.compile(r"\btoggle\b", re.I)
TRANSFORM_WORDS = re.compile(
    r"\b(transform|enter .*form|become|change form|dragonqueen|mounted form)\b",
    re.I,
)
AOE_TEXT_WORDS = re.compile(
    r"\b(nearby enemies|around the target|around .* location|in an area|area around)\b",
    re.I,
)
UNIT_TARGET_WORDS = re.compile(
    r"\b(target enemy|target hero|target ally|target allied|an enemy hero|an allied hero)\b",
    re.I,
)


def text_blob(ability):
    return " ".join(
        str(ability.get(k) or "")
        for k in ("name", "short_text", "full_text", "category", "ability_type")
    ).lower()


def first_value(facts, kind):
    for f in facts:
        if f.get("kind") == kind:
            return f.get("value")
    return None


def has_kind(facts, kind, value=None):
    return any(
        f.get("kind") == kind and (value is None or f.get("value") == value)
        for f in facts
    )


def best_numeric(facts, kind, min_conf=None):
    rank = {"LOW": 1, "MEDIUM": 2, "HIGH": 3, None: 0}
    min_rank = rank.get(min_conf, 0)
    rows = [
        f for f in facts
        if f.get("kind") == kind
        and isinstance(f.get("value"), (int, float))
        and rank.get(f.get("confidence"), 0) >= min_rank
    ]
    if not rows:
        return None
    rows.sort(key=lambda f: rank.get(f.get("confidence"), 0), reverse=True)
    return rows[0].get("value")


def strong_area_radius(area_facts):
    for f in area_facts:
        if f.get("kind") != "RADIUS" or f.get("confidence") != "HIGH":
            continue
        src = f.get("source") or {}
        if src.get("xml_type") in {"CEffectEnumArea", "CEffectSearch"}:
            return f.get("value")
    return None


def semantic_evidence(ability, role, targeting_facts, projectile_facts, area_facts, combat):
    text = text_blob(ability)
    targeting = first_value(targeting_facts, "TARGETING")
    projectile = has_kind(projectile_facts, "DELIVERY", "PROJECTILE")
    mover = (
        has_kind(projectile_facts, "MOVER_TYPE")
        or has_kind(area_facts, "REVERSE_MOVER")
        or bool(MOVEMENT_WORDS.search(text))
    )
    radius = strong_area_radius(area_facts)
    width = best_numeric(area_facts, "WIDTH", "MEDIUM")
    arc = best_numeric(area_facts, "ARC", "MEDIUM")
    area_node = has_kind(area_facts, "AREA_NODE")
    beam_text = bool(BEAM_WORDS.search(text))
    channel_text = bool(CHANNEL_WORDS.search(text))
    summon_text = bool(SUMMON_WORDS.search(text))

    return {
        "role": role,
        "targeting": targeting,
        "projectile": projectile,
        "mover": mover,
        "strong_area_radius": radius,
        "width": width,
        "arc": arc,
        "area_node": area_node,
        "beam_text": beam_text,
        "channel_text": channel_text,
        "summon_text": summon_text,
        "toggle_text": bool(TOGGLE_WORDS.search(text)),
        "transform_text": bool(TRANSFORM_WORDS.search(text)),
        "aoe_text": bool(AOE_TEXT_WORDS.search(text)),
        "unit_target_text": bool(UNIT_TARGET_WORDS.search(text)),
        "combat_present": bool(combat),
        "text": text,
    }


def classify_semantic(ability, evidence, combat):
    role = evidence["role"]
    text = evidence["text"]
    targeting = evidence["targeting"]

    if role == "PASSIVE":
        return "PASSIVE", "ROLE_PASSIVE", "HIGH"
    if role == "STATE":
        return "STATE", "ROLE_STATE", "HIGH"
    if role == "CONTROL":
        return "CONTROL", "ROLE_CONTROL", "HIGH"

    if role == "RECAST":
        name = str(ability.get("name") or "").lower()
        if any(x in name for x in ("cancel", "stop", "end")):
            return "CANCEL", "RECAST_CANCEL_NAME", "HIGH"
        if any(x in name for x in ("detonate", "explode", "release", "reactivate")):
            return "RECAST_TRIGGER", "RECAST_TRIGGER_NAME", "MEDIUM"

    # Delivery/movement evidence wins before internal area searches.
    if evidence["mover"]:
        return "MOVEMENT", "MOVER_OR_MOVEMENT_EVIDENCE", "HIGH"

    if evidence["projectile"]:
        return "PROJECTILE", "PROJECTILE_FACT", "HIGH"

    # Beam/line/cone are allowed when geometry supports them.
    if evidence["beam_text"] and evidence["width"] is not None:
        return "BEAM", "BEAM_TEXT_PLUS_WIDTH", "MEDIUM"

    if evidence["arc"] is not None and evidence["width"] is not None:
        if evidence["arc"] < 360:
            return "CONE", "ARC_PLUS_WIDTH", "MEDIUM"

    if evidence["width"] is not None and evidence["area_node"]:
        return "LINE", "WIDTH_PLUS_AREA_PATH", "MEDIUM"

    # AOE requires actual strong area geometry. AREA_NODE alone is never enough.
    if evidence["strong_area_radius"] is not None:
        return "AOE", "STRONG_EFFECT_AREA_RADIUS", "HIGH"

    if evidence["summon_text"]:
        return "SUMMON", "TEXT_SUMMON", "MEDIUM"

    if evidence["channel_text"]:
        return "CHANNEL", "TEXT_CHANNEL", "MEDIUM"

    if evidence["toggle_text"]:
        return "TOGGLE", "TEXT_TOGGLE", "MEDIUM"

    if evidence["transform_text"]:
        return "TRANSFORM", "TEXT_TRANSFORM", "MEDIUM"

    if targeting == "SELF_OR_INSTANT":
        if any(x in text for x in (
            "increase", "gain ", "grant ", "armor", "spell power",
            "attack speed", "movement speed", "shield",
        )):
            return "SELF_BUFF", "SELF_TARGETING_BUFF_TEXT", "MEDIUM"

        if combat and (combat.get("heal") or combat.get("shield_candidates")):
            return "SELF_EFFECT", "SELF_TARGETING_COMBAT", "MEDIUM"

        return "SELF", "SELF_TARGETING", "MEDIUM"

    if targeting == "POINT":
        if combat and combat.get("heal"):
            return "TARGETED_HEAL", "POINT_TARGET_HEAL", "MEDIUM"

        if evidence["unit_target_text"] or "shield" in text or "ally" in text:
            return "UNIT_TARGET", "POINT_TARGET_UNIT_TEXT", "MEDIUM"

        return "POINT_TARGET", "POINT_TARGET_DEFAULT", "MEDIUM"

    # Text AOE is only a weak candidate without strong geometry; do not force AOE.
    if evidence["aoe_text"]:
        return "OTHER", "TEXT_AOE_WITHOUT_GEOMETRY", "LOW"

    if evidence["unit_target_text"]:
        return "UNIT_TARGET", "TEXT_UNIT_TARGET", "LOW"

    return "OTHER", "NO_STRONG_SEMANTIC_RULE", "LOW"


def run_semantic_classifier(
    herodata_path,
    catalog_facts_path,
    targeting_path,
    projectile_path,
    area_path,
    combat_path,
    output_path,
    report_path,
):
    hd = load(herodata_path)
    roles = index_by_record_key(load(catalog_facts_path))
    t_idx = index_by_record_key(load(targeting_path))
    p_idx = index_by_record_key(load(projectile_path))
    a_idx = index_by_record_key(load(area_path))
    c_idx = index_by_record_key(load(combat_path))

    heroes = {}
    counts = Counter()
    reasons = Counter()
    confidences = Counter()
    review = []
    total = 0

    for hero, ability in ability_records(hd):
        total += 1
        rk = ability.get("record_key")

        role = roles.get(hero, {}).get(rk, {}).get("ability_role", "UNRESOLVED")
        tf = t_idx.get(hero, {}).get(rk, {}).get("facts", [])
        pf = p_idx.get(hero, {}).get(rk, {}).get("facts", [])
        af = a_idx.get(hero, {}).get(rk, {}).get("facts", [])
        combat = c_idx.get(hero, {}).get(rk, {})

        evidence = semantic_evidence(ability, role, tf, pf, af, combat)
        semantic, reason, confidence = classify_semantic(ability, evidence, combat)

        counts[semantic] += 1
        reasons[reason] += 1
        confidences[confidence] += 1

        row = {
            "record_key": rk,
            "ability_id": ability.get("id"),
            "name": ability.get("name"),
            "slot": ability.get("slot"),
            "ability_role": role,
            "semantic_type": semantic,
            "semantic_reason": reason,
            "semantic_confidence": confidence,
            "evidence": {
                k: v for k, v in evidence.items()
                if k != "text"
            },
        }
        heroes.setdefault(hero, []).append(row)

        if semantic == "OTHER":
            review.append({
                "hero": hero,
                "record_key": rk,
                "ability_id": ability.get("id"),
                "name": ability.get("name"),
                "reason": reason,
                "evidence": row["evidence"],
            })

    data = {
        "metadata": {
            "builder": "spatial_semantic_resolver_v2",
            "policy": "AREA_NODE is evidence only; strong radius is required for AOE.",
        },
        "heroes": heroes,
    }

    report = {
        "summary": {
            "abilities_total": total,
            "semantic_other": counts["OTHER"],
        },
        "semantic_types": dict(counts),
        "semantic_reasons": dict(reasons),
        "semantic_confidence": dict(confidences),
        "review": review,
    }

    save(output_path, data)
    save(report_path, report)
    return report
