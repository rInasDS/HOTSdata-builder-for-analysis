from __future__ import annotations

from collections import Counter, defaultdict
import re
from fact_utils import load, save, index_by_record_key, ability_records
from shield_model import infer_shield_models
from heal_model import infer_heal_models


SPATIAL_METRICS = {
    "RANGE", "WIDTH", "RADIUS", "AREA", "LENGTH", "ARC", "DURATION",
    "ADDITIONAL_TARGETS", "ADDITIONAL_PROJECTILES", "TARGET_COUNT",
}

COMBAT_METRICS = {
    "DAMAGE", "DAMAGE_BONUS", "DAMAGE_OVER_TIME", "DAMAGE_TAKEN", "ENEMY_DAMAGE_DEALT",
    "BASIC_ATTACK_DAMAGE", "SPELL_POWER",
    "PERCENT_MAX_HEALTH_DAMAGE", "PERCENT_CURRENT_HEALTH_DAMAGE", "PERCENT_MISSING_HEALTH_DAMAGE",
    "HEALING", "DAMAGE_TO_HEAL", "ARMOR", "SPELL_ARMOR", "DAMAGE_REDUCTION",
}

BASE_STATUS_PATTERNS = [
    ("STUN", re.compile(r"\bstun(?:s|ned|ning)?\b", re.I)),
    ("SLOW", re.compile(r"\bslow(?:s|ed|ing)?\b(?!\s+time)", re.I)),
    ("ROOT", re.compile(r"\broot(?:s|ed|ing)?\b", re.I)),
    ("SILENCE", re.compile(r"\bsilenc(?:e|es|ed|ing)\b", re.I)),
    ("BLIND", re.compile(r"\bblind(?:s|ed|ing)?\b", re.I)),
    ("FEAR", re.compile(r"\b(?:fear|fears|feared|terrify|terrifies|terrified)\b", re.I)),
    ("TAUNT", re.compile(r"\btaunt(?:s|ed|ing)?\b|\bforce(?:s|d)?\b[^.;]{0,45}?\battack\b", re.I)),
    ("SLEEP", re.compile(r"\bsleep(?:s|ing)?\b|\bput(?:s)? .{0,30}? to sleep\b", re.I)),
    ("KNOCKBACK", re.compile(r"\bknock(?:s|ed|ing)?(?:\s+\w+){0,5}?\s+(?:back|away)\b|\bknockback\b", re.I)),
    ("KNOCKUP", re.compile(r"\bknock(?:s|ed)?\s+up\b|\bknockup\b", re.I)),
    ("PULL", re.compile(r"\bpull(?:s|ed|ing)?\b", re.I)),
    ("UNSTOPPABLE", re.compile(r"\bunstoppable\b", re.I)),
    ("INVULNERABLE", re.compile(r"\binvulnerable\b", re.I)),
    ("PROTECTED", re.compile(r"\bprotected\b", re.I)),
    ("STASIS", re.compile(r"\bstasis\b", re.I)),
    ("CLEANSE", re.compile(r"\bcleanse\b|\bremove(?:s|d)? all .{0,50}?(?:roots|slows|stuns|silences|crowd control)\b", re.I)),
]

SLOW_AMOUNT_RE = re.compile(r"\bslow(?:s|ed|ing)?[^.%]{0,55}?by\s+(\d+(?:\.\d+)?)%", re.I)
STATUS_DURATION_RE = re.compile(r"\bfor\s+(\d+(?:\.\d+)?)\s+seconds?", re.I)


def strong_radius(area_facts):
    for f in area_facts:
        if f.get("kind") == "RADIUS" and f.get("confidence") == "HIGH":
            src = f.get("source") or {}
            if src.get("xml_type") in {"CEffectEnumArea", "CEffectSearch"}:
                return f.get("value")
    return None


def index_talent_rows(data):
    out = {}
    for hero, rows in data.get("heroes", {}).items():
        m = {}
        for row in rows:
            if row.get("talent_id"):
                m[str(row["talent_id"])] = row
        out[hero] = m
    return out


def ability_target_ids(row):
    return {
        str(t.get("id"))
        for t in row.get("targets", [])
        if t.get("type") == "ABILITY" and t.get("id")
    }


def base_text_subeffects(ability, combat):
    text=" ".join(str(ability.get(k) or "") for k in ("short_text","full_text"))
    effects=[]

    if combat.get("damage"):
        effects.append({
            "node_id":"base_damage", "kind":"DAMAGE", "trigger":"ON_RESOLVE",
            "instances":combat.get("damage") or [], "confidence":"HIGH",
            "source":"BASE_XML_EFFECT_GRAPH",
        })
    if combat.get("heal"):
        effects.append({
            "node_id":"base_heal", "kind":"HEAL", "trigger":"ON_RESOLVE",
            "instances":combat.get("heal") or [], "confidence":"HIGH",
            "source":"BASE_XML_EFFECT_GRAPH",
        })
    shield_models=infer_shield_models(text,"ABILITY_TEXT")
    if combat.get("shield_candidates") or shield_models:
        effects.append({
            "node_id":"base_shield", "kind":"SHIELD", "trigger":"ON_RESOLVE",
            "instances":combat.get("shield_candidates") or [],
            "shield_models":shield_models,
            "confidence":"MEDIUM" if shield_models else "MEDIUM",
            "source":"BASE_XML_EFFECT_GRAPH+TEXT_CANDIDATE" if shield_models else "BASE_XML_EFFECT_GRAPH",
        })

    lower=text.lower()
    seen=set()
    for status,pat in BASE_STATUS_PATTERNS:
        if not pat.search(text) or status in seen:
            continue
        node={
            "node_id":"base_status_"+status.lower(),
            "kind":"STATUS_EFFECT", "value":status, "trigger":"ON_RESOLVE",
            "confidence":"HIGH", "source":"HERODATA_TEXT_BASE_SEMANTIC",
        }
        if status=="SLOW":
            m=SLOW_AMOUNT_RE.search(text)
            if m: node["amount_percent"]=float(m.group(1))
            # Slow duration is safe only when tied to the slow phrase; do not borrow
            # an unrelated duration for another status in the same tooltip.
            sm=re.search(r"\bslow(?:s|ed|ing)?[^.]{0,100}?\bfor\s+(\d+(?:\.\d+)?)\s+seconds?", text, re.I)
            if sm: node["duration_seconds"]=float(sm.group(1))
        effects.append(node); seen.add(status)
    return effects


def base_subeffects(spatial, area_facts, combat, ability=None):
    semantic = spatial.get("semantic_type")
    effects = []

    # Primary action is always explicit.
    primary = {
        "node_id": "primary",
        "kind": semantic,
        "trigger": "ON_CAST",
        "shape": spatial.get("shape"),
        "range": spatial.get("cast_range"),
        "width": spatial.get("width"),
        "radius": spatial.get("radius"),
        "length": spatial.get("length"),
        "arc": spatial.get("arc"),
        "confidence": spatial.get("confidence"),
        "source": "BASE_ABILITY",
    }

    if semantic == "PROJECTILE":
        primary["trigger"] = "ON_LAUNCH"
    elif semantic == "MOVEMENT":
        primary["trigger"] = "ON_MOVEMENT_START"

    effects.append(primary)

    radius = strong_radius(area_facts)
    if radius is not None:
        trigger = (
            "ON_IMPACT" if semantic == "PROJECTILE"
            else "ON_ARRIVAL" if semantic == "MOVEMENT"
            else "ON_RESOLVE"
        )
        effects.append({
            "node_id": "base_area",
            "kind": "AREA_EFFECT",
            "trigger": trigger,
            "shape": "CIRCLE",
            "radius": radius,
            "effects": {
                "damage": bool(combat.get("damage")),
                "heal": bool(combat.get("heal")),
                "shield": bool(combat.get("shield_candidates")),
            },
            "confidence": "HIGH",
            "source": "BASE_XML_EFFECT_GRAPH",
        })

    if ability is not None:
        existing={(e.get("kind"),e.get("value")) for e in effects}
        for e in base_text_subeffects(ability,combat):
            key=(e.get("kind"),e.get("value"))
            if key not in existing:
                effects.append(e); existing.add(key)

    return effects


def talent_overlays_for_ability(hero, ability_id, talent_rows):
    overlays = []

    for talent in talent_rows:
        targets = ability_target_ids(talent)
        if ability_id not in targets:
            continue

        spatial_mods = []
        combat_mods = []
        runtime_effects = []
        raw=talent.get("raw_text") or {}
        talent_text=(raw.get("normalized") if isinstance(raw,dict) else "") or talent.get("full_text") or talent.get("short_text") or ""
        talent_shields=infer_shield_models(talent_text,"TALENT_TEXT")
        talent_heals=infer_heal_models(talent_text,"TALENT_TEXT")
        for sm in talent_shields:
            runtime_effects.append({
                "kind":"SHIELD_MODEL",
                "confidence":"MEDIUM",
                "value":sm,
                "provenance":{"source_type":"TALENT_TEXT_SHIELD_CANDIDATE","numeric_authoritative":False},
            })

        for e in talent.get("effects", []) or []:
            value = e.get("value")
            if e.get("kind") == "MODIFIER" and isinstance(value, dict):
                metric=value.get("metric")
                if metric in SPATIAL_METRICS:
                    spatial_mods.append(e)
                if metric in COMBAT_METRICS:
                    combat_mods.append(e)
            elif e.get("kind") in {
                "STATUS_EFFECT", "STATUS_REMOVE", "SUMMON_RULE", "COUNT_MODIFIER", "MECHANIC",
                "STACK_RULE"
            }:
                runtime_effects.append(e)

        if spatial_mods:
            overlays.append({
                "talent_id": talent.get("talent_id"),
                "name": talent.get("name"),
                "action": "MODIFY_SPATIAL",
                "modifiers": spatial_mods,
                "runtime_rules": {
                    "triggers": talent.get("triggers") or [],
                    "conditions": talent.get("conditions") or [],
                    "temporal_rules": talent.get("temporal_rules") or [],
                    "counter_rules": talent.get("counter_rules") or [],
                },
                "source": "TALENT_DECODER",
            })

        if combat_mods:
            overlays.append({
                "talent_id":talent.get("talent_id"),
                "name":talent.get("name"),
                "action":"MODIFY_COMBAT",
                "modifiers":combat_mods,
                "runtime_rules":{
                    "triggers":talent.get("triggers") or [],
                    "conditions":talent.get("conditions") or [],
                    "temporal_rules":talent.get("temporal_rules") or [],
                    "counter_rules":talent.get("counter_rules") or [],
                    "internal_cooldown":talent.get("internal_cooldown") or [],
                    "quest":talent.get("quest") or [],
                },
                "source":"TALENT_DECODER",
            })

        if runtime_effects or talent.get("internal_cooldown") or talent.get("quest"):
            overlays.append({
                "talent_id": talent.get("talent_id"),
                "name": talent.get("name"),
                "action": "ADD_OR_MODIFY_SUBEFFECT",
                "effects": runtime_effects,
                "triggers": talent.get("triggers") or [],
                "conditions": talent.get("conditions") or [],
                "temporal_rules": talent.get("temporal_rules") or [],
                "counter_rules": talent.get("counter_rules") or [],
                "internal_cooldown": talent.get("internal_cooldown") or [],
                "quest": talent.get("quest") or [],
                "source": "TALENT_DECODER",
            })

    return overlays


def run_subeffect_graph(
    herodata_path,
    spatial_path,
    area_path,
    combat_path,
    talent_modifier_path,
    output_path,
    report_path,
):
    hd = load(herodata_path)
    spatial_idx = index_by_record_key(load(spatial_path))
    area_idx = index_by_record_key(load(area_path))
    combat_idx = index_by_record_key(load(combat_path))
    talent_data = load(talent_modifier_path)

    talent_rows = talent_data.get("heroes", {})
    heroes = {}
    counts = Counter()

    for hero, ability in ability_records(hd):
        rk = ability.get("record_key")
        aid = str(ability.get("id") or "")
        spatial = spatial_idx.get(hero, {}).get(rk, {})
        area_facts = area_idx.get(hero, {}).get(rk, {}).get("facts", [])
        combat = combat_idx.get(hero, {}).get(rk, {})

        base = base_subeffects(spatial, area_facts, combat, ability)
        overlays = talent_overlays_for_ability(
            hero, aid, talent_rows.get(hero, [])
        )

        counts["abilities_total"] += 1
        counts["base_subeffects"] += len(base)
        if len(base) > 1:
            counts["abilities_with_nested_base_subeffects"] += 1
        if overlays:
            counts["abilities_with_talent_overlays"] += 1
            counts["talent_overlays"] += len(overlays)

        heroes.setdefault(hero, []).append({
            "record_key": rk,
            "ability_id": aid,
            "name": ability.get("name"),
            "primary_semantic": spatial.get("semantic_type"),
            "base_subeffects": base,
            "talent_overlays": overlays,
        })

    data = {
        "metadata": {
            "builder": "ability_subeffect_graph_v1_2_talent_status",
            "schema_version": "3.12.7",
            "principle": (
                "Primary action and nested/talent-added effects are represented separately."
            ),
        },
        "heroes": heroes,
    }
    report = {"summary": dict(counts)}

    save(output_path, data)
    save(report_path, report)
    return report
