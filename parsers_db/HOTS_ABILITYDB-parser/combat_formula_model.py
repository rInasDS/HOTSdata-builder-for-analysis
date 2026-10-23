from __future__ import annotations

import re
from copy import deepcopy


def _clean(text):
    s=str(text or "")
    s=re.sub(r"<n\s*/?>", ". ", s, flags=re.I)
    s=re.sub(r"<[^>]+>", "", s)
    s=re.sub(r"~~\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*~~", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _f(v):
    try:
        return float(v)
    except Exception:
        return None


# High-confidence direct damage grammar for an ability's own tooltip.
DIRECT_FLAT = re.compile(r"\b(?:deal|deals|dealing|take|takes|taking)\s+(?:up\s+to\s+)?(\d+(?:\.\d+)?)\s+(?:ability\s+|spell\s+)?damage\b", re.I)
DIRECT_OVER_TIME = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)\s+damage\s+(?:over|every)\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
DIRECT_EVERY_TICK = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)\s+damage\s+every\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
PCT_MAX = re.compile(r"\b(?:deal|deals|dealing|lose|loses)\s+(?:up\s+to\s+)?(\d+(?:\.\d+)?)%\s+of\s+(?:their|the\s+target(?:'s)?|an?\s+enemy\s+hero(?:'s)?)\s+maximum\s+health\s+(?:as\s+damage)?", re.I)
PCT_CURRENT = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their|the\s+target(?:'s)?)\s+current\s+health", re.I)
PCT_MISSING = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their|the\s+target(?:'s)?)\s+missing\s+health", re.I)
BONUS_MAX = re.compile(r"\b(?:bonus|additional)\s+damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their|the\s+target(?:'s)?)\s+maximum\s+health", re.I)
BASIC_ATTACK_PERCENT = re.compile(r"\bbasic\s+attacks?\b[^.;]{0,120}?\b(?:deal|deals)\s+(\d+(?:\.\d+)?)%\s+(?:more|bonus|additional|increased)?\s*damage", re.I)
EVERY_N_ATTACK = re.compile(r"\bevery\s+(\d+)(?:st|nd|rd|th)?\s+basic\s+attack\b", re.I)
SECONDARY_TARGET = re.compile(r"\b(?:nearby|additional|secondary)\s+(?:target|targets|enemy|enemies)\b|\bsplash(?:es)?\b", re.I)
SUMMON_SOURCE = re.compile(r"\b(?:summon|summons|summoned|minion|minions|mercenary|mercenaries|beetle|beetles|zombie|zombies|hydralisk|ultralisk|turret|turrets|locust|locusts|misha|vikings?|mirror images?)\b", re.I)
DOT_SOURCE = re.compile(r"\b(?:damage over time|periodic damage|poison|poisons|burn|burns|bleed|bleeds|every\s+\d+(?:\.\d+)?\s+seconds?)\b", re.I)

# v3.12.8 advanced direct/passive damage grammar.
BONUS_FLAT_DAMAGE = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)\s+bonus(?:\s+spell)?\s+damage\b", re.I)
DAMAGED_FOR_FLAT = re.compile(r"\b(?:enemy|enemies|hero|heroes|target|targets)\s+(?:is|are)\s+damaged\s+for\s+(\d+(?:\.\d+)?)\b", re.I)
DAMAGE_RANGE_FLAT = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)\s+(?:to|[-–])\s+(\d+(?:\.\d+)?)\s+damage\b", re.I)
ADDITIONAL_FLAT_OVER_TIME = re.compile(r"\b(?:take|takes|taking|deal|deals|dealing)\s+(\d+(?:\.\d+)?)\s+additional\s+damage\s+over\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
STRIKE_EVERY_TICK = re.compile(r"\b(?:strike|strikes|striking|hit|hits|hitting)\b[^.;]{0,80}?\b(?:for\s+)?(\d+(?:\.\d+)?)\s+damage\s+every\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
DAMAGE_EQUAL_MISSING = re.compile(r"\b(?:deal|deals|dealing)\s+damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their|the target(?:'s)?)\s+missing\s+health\b", re.I)
DAMAGE_EQUAL_MAX = re.compile(r"\b(?:deal|deals|dealing)\s+(?:bonus\s+)?damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+(?:of\s+)?(?:their|the target(?:'s)?)?\s*maximum\s+health\b", re.I)
PCT_MAX_NO_OF = re.compile(r"\b(?:deal|deals|dealing|impact deals)\s+(\d+(?:\.\d+)?)%\s+maximum\s+health\s+as\s+damage\b", re.I)
PASSIVE_MORE_DAMAGE = re.compile(r"\b(?:deal|deals)\s+(\d+(?:\.\d+)?)%\s+more\s+damage\s+to\s+(.+?)(?:[.;]|$)", re.I)
DAMAGE_PER_MARK = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)%\s+bonus\s+damage\s+per\s+(?:mark|stack|charge)\b", re.I)
COMBO_POINT_VALUE = re.compile(r"\b(\d+)\s+points?\s*[:=-]\s*(\d+(?:\.\d+)?)\b", re.I)

DEATH_SOURCE = re.compile(r"\b(?:upon dying|when .* dies|on death|after dying)\b", re.I)
FORM_SOURCE = re.compile(r"\b(?:while in|during|form|mode|odin|siege mode|molten core|dragonqueen|archon)\b", re.I)


def ability_damage_models(ability, combat=None):
    """High-confidence base damage models. Never infers from generic 'increase damage'."""
    text=_clean(" ".join(str(ability.get(k) or "") for k in ("short_text","full_text")))
    models=[]

    # Exact XML instances are authoritative and may include level scaling.
    for inst in (combat or {}).get("damage") or []:
        if not isinstance(inst,dict):
            continue
        f=inst.get("formula") or {}
        if f.get("model")=="PERCENT_MAX_HEALTH":
            models.append({
                "source_type":"XML_DAMAGE_EFFECT",
                "formula_model":"PERCENT_MAX_HEALTH",
                "value_percent":f.get("value_percent"),
                "object_id":inst.get("object_id") or inst.get("effect_id"),
                "graph_depth":inst.get("graph_depth"),
                "conditions":deepcopy(inst.get("conditions") or []),
                "modifiers":deepcopy(inst.get("formula_modifiers") or f.get("modifiers") or []),
                "confidence":"HIGH",
                "provenance":{"source":"combat.damage","formula":deepcopy(f)},
            })
        else:
            models.append({
                "source_type":"XML_DAMAGE_EFFECT",
                "formula_model":"XML_EXECUTABLE_DAMAGE",
                "value":inst.get("value"),
                "scale":inst.get("scale"),
                "object_id":inst.get("object_id") or inst.get("effect_id"),
                "graph_depth":inst.get("graph_depth"),
                "conditions":deepcopy(inst.get("conditions") or []),
                "modifiers":deepcopy(inst.get("formula_modifiers") or f.get("modifiers") or []),
                "confidence":"HIGH",
                "provenance":{"source":"combat.damage","formula":deepcopy(f)},
            })

    # Tooltip formula is fallback/parallel evidence when XML traversal misses indirect damage.
    for m in DIRECT_EVERY_TICK.finditer(text):
        models.append({"source_type":"ABILITY","formula_model":"FLAT_PER_TICK","value":_f(m.group(1)),"tick_interval_seconds":_f(m.group(2)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in DIRECT_OVER_TIME.finditer(text):
        models.append({"source_type":"ABILITY","formula_model":"FLAT_OVER_TIME","value":_f(m.group(1)),"duration_seconds":_f(m.group(2)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in DIRECT_FLAT.finditer(text):
        models.append({"source_type":"ABILITY","formula_model":"FLAT","value":_f(m.group(1)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for rx, model in ((PCT_MAX,"PERCENT_MAX_HEALTH"),(PCT_CURRENT,"PERCENT_CURRENT_HEALTH"),(PCT_MISSING,"PERCENT_MISSING_HEALTH"),(BONUS_MAX,"PERCENT_MAX_HEALTH_BONUS")):
        for m in rx.finditer(text):
            models.append({"source_type":"ABILITY","formula_model":model,"value_percent":_f(m.group(1)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in BASIC_ATTACK_PERCENT.finditer(text):
        models.append({"source_type":"BASIC_ATTACK","formula_model":"BASIC_ATTACK_PERCENT","value_percent":_f(m.group(1)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})

    # v3.12.8 formulas frequently used by traits, transformed abilities and actives.
    for m in BONUS_FLAT_DAMAGE.finditer(text):
        src_type="BASIC_ATTACK" if "basic attack" in text.lower() or "next attack" in text.lower() else "ABILITY"
        models.append({"source_type":src_type,"formula_model":"FLAT_BONUS","value":_f(m.group(1)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in DAMAGED_FOR_FLAT.finditer(text):
        models.append({"source_type":"ABILITY","formula_model":"FLAT","value":_f(m.group(1)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in DAMAGE_RANGE_FLAT.finditer(text):
        models.append({"source_type":"ABILITY","formula_model":"FLAT_RANGE","min_value":_f(m.group(1)),"max_value":_f(m.group(2)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in ADDITIONAL_FLAT_OVER_TIME.finditer(text):
        models.append({"source_type":"ABILITY_OR_TRIGGERED_EFFECT","formula_model":"FLAT_OVER_TIME","value":_f(m.group(1)),"duration_seconds":_f(m.group(2)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in STRIKE_EVERY_TICK.finditer(text):
        models.append({"source_type":"ABILITY","formula_model":"FLAT_PER_TICK","value":_f(m.group(1)),"tick_interval_seconds":_f(m.group(2)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for rx,model in ((DAMAGE_EQUAL_MISSING,"PERCENT_MISSING_HEALTH"),(DAMAGE_EQUAL_MAX,"PERCENT_MAX_HEALTH"),(PCT_MAX_NO_OF,"PERCENT_MAX_HEALTH")):
        for m in rx.finditer(text):
            models.append({"source_type":"ABILITY_OR_TRIGGERED_EFFECT","formula_model":model,"value_percent":_f(m.group(1)),"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    for m in DAMAGE_PER_MARK.finditer(text):
        models.append({"source_type":"BASIC_ATTACK_OR_TRIGGERED_EFFECT","formula_model":"PERCENT_BONUS_PER_COUNTER","value_percent_per_counter":_f(m.group(1)),"counter":"MARK_OR_STACK_OR_CHARGE","confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":m.group(0)}})
    combo=[(int(m.group(1)),_f(m.group(2))) for m in COMBO_POINT_VALUE.finditer(text)]
    if len(combo)>=2 and "combo point" in text.lower():
        models.append({"source_type":"ABILITY","formula_model":"COMBO_POINT_TABLE","values":[{"points":n,"value":v} for n,v in combo],"confidence":"HIGH","provenance":{"source":"ABILITY_TEXT","phrase":"combo point table"}})

    # Add execution/source annotations without changing numeric formula.
    src=[]
    if SUMMON_SOURCE.search(text): src.append("SUMMON_OR_MINION")
    if DOT_SOURCE.search(text): src.append("DOT_OR_PERIODIC")
    if SECONDARY_TARGET.search(text): src.append("SECONDARY_OR_SPLASH_TARGET")
    if DEATH_SOURCE.search(text): src.append("DEATH_EFFECT")
    if FORM_SOURCE.search(text): src.append("FORM_OR_MODE")
    if EVERY_N_ATTACK.search(text): src.append("PERIODIC_BASIC_ATTACK")
    if src:
        for model in models:
            model["source_tags"]=sorted(set(src))

    # Deduplicate stable semantic values.
    seen=set(); out=[]
    for x in models:
        key=(x.get("source_type"),x.get("formula_model"),x.get("value"),x.get("value_percent"),x.get("tick_interval_seconds"),x.get("duration_seconds"),x.get("min_value"),x.get("max_value"),str(x.get("values")),x.get("value_percent_per_counter"),x.get("object_id"))
        if key not in seen:
            seen.add(key);out.append(x)
    return out


def damage_source_from_phrase(phrase, metric=None, targets=None):
    p=_clean(phrase).lower()
    metric=str(metric or "").upper()
    ttypes={str((x or {}).get("type") or "").upper() for x in (targets or []) if isinstance(x,dict)}
    tags=[]

    if metric=="BASIC_ATTACK_DAMAGE" or "basic attack" in p or "attack damage" in p:
        primary="BASIC_ATTACK"
    elif metric in {"PERCENT_MAX_HEALTH_DAMAGE","PERCENT_CURRENT_HEALTH_DAMAGE","PERCENT_MISSING_HEALTH_DAMAGE"}:
        primary="ABILITY_OR_TRIGGERED_EFFECT"
    elif "SUMMONED_UNIT" in ttypes or SUMMON_SOURCE.search(p):
        primary="SUMMON_OR_MINION"
    elif "BASIC_ATTACK" in ttypes:
        primary="BASIC_ATTACK"
    else:
        primary="ABILITY_OR_TRIGGERED_EFFECT"

    if DOT_SOURCE.search(p) or metric=="DAMAGE_OVER_TIME": tags.append("DOT_OR_PERIODIC")
    if SECONDARY_TARGET.search(p): tags.append("SECONDARY_OR_SPLASH_TARGET")
    if SUMMON_SOURCE.search(p): tags.append("SUMMON_OR_MINION")
    if DEATH_SOURCE.search(p): tags.append("DEATH_EFFECT")
    if FORM_SOURCE.search(p): tags.append("FORM_OR_MODE")
    if EVERY_N_ATTACK.search(p): tags.append("PERIODIC_BASIC_ATTACK")
    if "hero" in p: tags.append("HERO_TARGET_CONTEXT")
    if "non-hero" in p or "minion" in p or "mercenar" in p: tags.append("NON_HERO_TARGET_CONTEXT")

    return primary, sorted(set(tags))


def enrich_modifier_damage_semantics(modifier, targets=None):
    """Attach replay-resolver damage source/formula metadata to an existing MODIFIER."""
    if not isinstance(modifier,dict) or modifier.get("kind")!="MODIFIER":
        return modifier
    v=modifier.get("value")
    if not isinstance(v,dict): return modifier
    metric=str(v.get("metric") or "").upper()
    damage_metrics={
        "DAMAGE","DAMAGE_BONUS","DAMAGE_OVER_TIME","DAMAGE_TAKEN","ENEMY_DAMAGE_DEALT",
        "BASIC_ATTACK_DAMAGE","SPELL_POWER","PERCENT_MAX_HEALTH_DAMAGE","PERCENT_CURRENT_HEALTH_DAMAGE",
        "PERCENT_MISSING_HEALTH_DAMAGE","PERIODIC_DAMAGE","HERO_DAMAGE",
    }
    if metric not in damage_metrics: return modifier
    phrase=str((modifier.get("provenance") or {}).get("phrase") or "")
    primary,tags=damage_source_from_phrase(phrase,metric,targets)
    formula={
        "metric":metric,
        "operation":v.get("operation"),
        "value":v.get("value"),
        "from":v.get("from"),
        "to":v.get("to"),
    }
    if metric=="PERCENT_MAX_HEALTH_DAMAGE": formula["amount_model"]="PERCENT_MAX_HEALTH"
    elif metric=="PERCENT_CURRENT_HEALTH_DAMAGE": formula["amount_model"]="PERCENT_CURRENT_HEALTH"
    elif metric=="PERCENT_MISSING_HEALTH_DAMAGE": formula["amount_model"]="PERCENT_MISSING_HEALTH"
    elif metric=="DAMAGE_OVER_TIME": formula["amount_model"]="DOT_MODIFIER"
    elif metric=="BASIC_ATTACK_DAMAGE": formula["amount_model"]="BASIC_ATTACK_MODIFIER"
    elif metric=="SPELL_POWER": formula["amount_model"]="SPELL_POWER_MODIFIER"
    elif metric in {"DAMAGE_TAKEN","ENEMY_DAMAGE_DEALT"}: formula["amount_model"]="TARGET_DAMAGE_MULTIPLIER"
    elif v.get("operation")=="SET_PERCENT_OF_SOURCE": formula["amount_model"]="SOURCE_DAMAGE_RATIO"
    elif v.get("operation")=="ADD_PERCENT_PER_COUNTER": formula["amount_model"]="PERCENT_PER_COUNTER"
    elif v.get("operation")=="ADD_PERCENT_PER_EVENT": formula["amount_model"]="PERCENT_PER_EVENT"
    elif v.get("operation")=="DEAL_FLAT_TOTAL": formula["amount_model"]="FLAT_TOTAL_OVER_TIME"
    elif v.get("operation")=="ADD_PERCENT_DISTANCE_SCALING": formula["amount_model"]="DISTANCE_SCALING_PERCENT"
    elif v.get("operation")=="SET_PERCENT_OF_BASE": formula["amount_model"]="PERCENT_OF_BASE_DAMAGE"
    else: formula["amount_model"]="DAMAGE_MODIFIER"

    modifier=deepcopy(modifier)
    modifier["damage_semantics"]={
        "source":primary,
        "source_tags":tags,
        "formula":formula,
        "target_filter":deepcopy(modifier.get("target_filter") or []),
        "confidence":modifier.get("confidence") or "MEDIUM",
    }
    return modifier
