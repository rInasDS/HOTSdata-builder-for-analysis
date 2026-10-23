from __future__ import annotations

import copy
from fact_utils import load, save, index_by_record_key
from combat_formula_model import ability_damage_models, enrich_modifier_damage_semantics
from heal_model import infer_heal_models
from shield_model import infer_shield_models
from replay_schema_v1 import attach_schema_v1, validate_schema_v1


def index_talent_modifiers(data):
    out = {}
    for hero, rows in data.get("heroes", {}).items():
        out[hero] = {
            str(row.get("talent_id")): row
            for row in rows if row.get("talent_id")
        }
    return out




def _validated_direct_shield_instances(node):
    out=[]
    for inst in node.get("instances") or []:
        if not isinstance(inst,dict):
            continue
        evidence=str(inst.get("evidence_class") or "")
        if evidence=="DIRECT_BASE_SHIELD" or evidence.startswith("TYPED_DAMAGE_RESPONSE_") or evidence=="TYPED_CREATE_HEALER_SHIELD":
            out.append(inst); continue
        # Backward-compatible validation for older intermediate facts:
        # talent-scoped shield branches are never unconditional base shields.
        blob=" ".join([str(inst.get("object_id") or "")]+[str(x or "") for x in (inst.get("chain") or [])]).lower()
        if "talent" not in blob and inst.get("xml_type") in {"CEffectApplyBehavior","CBehaviorBuff"}:
            out.append(inst)
    return out


def build_resolver_semantics(subeffects, ability=None, combat=None):
    """Compact replay-resolver view derived from the full subeffect graph."""
    if not isinstance(subeffects,dict):
        return {"base_effects":[],"status_effects":[],"has_damage":False,"has_heal":False,
                "has_shield":False,"shield_model":None,"shield_models":[],
                "interrupt_capable":[],"protection":[],"talent_combat_overlays":[],
                "talent_status_overlays":[],"talent_control_overlays":[],"talent_protection_overlays":[]}
    base_effects=[]; statuses=[]; has_damage=False; has_heal=False; has_shield=False; shield_models=[]
    _ability_text=" ".join(str((ability or {}).get(k) or "") for k in ("short_text","full_text"))
    damage_models=ability_damage_models(ability or {}, combat or {})
    heal_models=infer_heal_models(_ability_text,"ABILITY_TEXT")
    shield_models=infer_shield_models(_ability_text,"ABILITY_TEXT")
    if shield_models:
        has_shield=True
    for n in subeffects.get("base_subeffects",[]) or []:
        kind=str(n.get("kind") or "").upper()
        if kind: base_effects.append(kind)
        if kind=="DAMAGE": has_damage=True
        if kind=="HEAL": has_heal=True
        if kind=="SHIELD":
            node_models=copy.deepcopy(n.get("shield_models") or [])
            direct_instances=_validated_direct_shield_instances(n)
            if node_models or direct_instances:
                has_shield=True
                shield_models.extend(node_models)
        if kind=="AREA_EFFECT":
            ef=n.get("effects") or {}
            has_damage = has_damage or bool(ef.get("damage"))
            has_heal = has_heal or bool(ef.get("heal"))
            # AREA_EFFECT shield flags are advisory only; validated shield nodes
            # or explicit shield models are required to set has_shield.
        if kind=="STATUS_EFFECT" and n.get("value"):
            statuses.append(str(n.get("value")).upper())

    if damage_models:
        has_damage=True
        if "DAMAGE" not in base_effects:
            base_effects.append("DAMAGE")
    if heal_models:
        has_heal=True
        if "HEAL" not in base_effects:
            base_effects.append("HEAL")

    interrupt_set={"STUN","SILENCE","SLEEP","FEAR","TAUNT","KNOCKBACK","KNOCKUP","PULL","DISPLACEMENT"}
    protection_set={"UNSTOPPABLE","INVULNERABLE","PROTECTED","STASIS","CLEANSE"}
    combat_overlays=[]; talent_status_overlays=[]
    for ov in subeffects.get("talent_overlays",[]) or []:
        if ov.get("action")=="ADD_OR_MODIFY_SUBEFFECT":
            for e in ov.get("effects") or []:
                if e.get("kind")=="SHIELD_MODEL" and isinstance(e.get("value"),dict):
                    sm=copy.deepcopy(e["value"]); sm["talent_id"]=ov.get("talent_id"); sm["talent_name"]=ov.get("name")
                    shield_models.append(sm); has_shield=True
                if e.get("kind")=="HEAL_MODEL" and isinstance(e.get("value"),dict):
                    hm=copy.deepcopy(e["value"]); hm["talent_id"]=ov.get("talent_id"); hm["talent_name"]=ov.get("name")
                    heal_models.append(hm); has_heal=True
                if e.get("kind") in {"STATUS_EFFECT","STATUS_REMOVE"} and e.get("value"):
                    talent_status_overlays.append({
                        "talent_id":ov.get("talent_id"),"talent_name":ov.get("name"),
                        "operation":"REMOVE" if e.get("kind")=="STATUS_REMOVE" else "APPLY",
                        "status":str(e.get("value")).upper(),
                        "amount_percent":e.get("amount_percent"),
                        "duration_seconds":e.get("duration_seconds"),
                        "target_filter":copy.deepcopy(e.get("target_filter") or []),
                        "triggers":copy.deepcopy(ov.get("triggers") or []),
                        "conditions":copy.deepcopy(ov.get("conditions") or []),
                        "temporal_rules":copy.deepcopy(ov.get("temporal_rules") or []),
                        "counter_rules":copy.deepcopy(ov.get("counter_rules") or []),
                        "internal_cooldown":copy.deepcopy(ov.get("internal_cooldown") or []),
                        "quest":copy.deepcopy(ov.get("quest") or []),
                    })
        if ov.get("action")!="MODIFY_COMBAT": continue
        _mods=[enrich_modifier_damage_semantics(copy.deepcopy(m), None) for m in (ov.get("modifiers") or [])]
        combat_overlays.append({
            "talent_id":ov.get("talent_id"),
            "name":ov.get("name"),
            "modifiers":_mods,
            "runtime_rules":copy.deepcopy(ov.get("runtime_rules") or {}),
        })
    _sm_seen=set(); _sm_uniq=[]
    for _sm in shield_models:
        _k=(_sm.get("amount_model"),_sm.get("value"),_sm.get("candidate_value"),_sm.get("evidence"),_sm.get("talent_id"))
        if _k not in _sm_seen:
            _sm_seen.add(_k); _sm_uniq.append(_sm)
    shield_models=_sm_uniq
    _hm_seen=set(); _hm_uniq=[]
    for _hm in heal_models:
        _k=(_hm.get("amount_model"),_hm.get("value"),_hm.get("evidence"),_hm.get("talent_id"))
        if _k not in _hm_seen:
            _hm_seen.add(_k); _hm_uniq.append(_hm)
    heal_models=_hm_uniq

    return {
        "base_effects":sorted(set(base_effects)),
        "status_effects":sorted(set(statuses)),
        "has_damage":has_damage,
        "damage_models":damage_models,
        "has_heal":has_heal,
        "heal_model":copy.deepcopy(heal_models[0]) if len(heal_models)==1 else None,
        "heal_models":heal_models,
        "has_shield":has_shield,
        "shield_model":copy.deepcopy(shield_models[0]) if len(shield_models)==1 else None,
        "shield_models":shield_models,
        "interrupt_capable":sorted(set(statuses)&interrupt_set),
        "protection":sorted(set(statuses)&protection_set),
        "talent_combat_overlays":combat_overlays,
        "talent_status_overlays":talent_status_overlays,
        "talent_control_overlays":[x for x in talent_status_overlays if x.get("operation")=="APPLY" and x.get("status") in (interrupt_set|{"ROOT","SLOW","BLIND"})],
        "talent_protection_overlays":[x for x in talent_status_overlays if x.get("status") in protection_set or x.get("operation")=="REMOVE"],
    }


def build_talent_resolver_semantics(modifier_row):
    """Hero-level talent semantics for replay resolver, independent of ability binding."""
    if not isinstance(modifier_row,dict):
        return {"combat_modifiers":[],"status_overlays":[],"control_overlays":[],"protection_overlays":[],"targets":[],"runtime_rules":{}}
    combat_metrics={
        "DAMAGE","DAMAGE_BONUS","DAMAGE_OVER_TIME","DAMAGE_TAKEN","ENEMY_DAMAGE_DEALT",
        "BASIC_ATTACK_DAMAGE","SPELL_POWER","PERCENT_MAX_HEALTH_DAMAGE",
        "PERCENT_CURRENT_HEALTH_DAMAGE","PERCENT_MISSING_HEALTH_DAMAGE",
        "HEALING","DAMAGE_TO_HEAL","ARMOR","SPELL_ARMOR","DAMAGE_REDUCTION",
    }
    control_set={"STUN","SILENCE","SLEEP","FEAR","TAUNT","KNOCKBACK","KNOCKUP","PULL","DISPLACEMENT","ROOT","SLOW","BLIND"}
    protection_set={"UNSTOPPABLE","INVULNERABLE","PROTECTED","STASIS","CLEANSE"}
    mods=[]; statuses=[]
    for e in modifier_row.get("effects") or []:
        if not isinstance(e,dict): continue
        v=e.get("value")
        if e.get("kind")=="MODIFIER" and isinstance(v,dict) and str(v.get("metric") or "").upper() in combat_metrics:
            mods.append(enrich_modifier_damage_semantics(copy.deepcopy(e), modifier_row.get("targets") or []))
        if e.get("kind") in {"STATUS_EFFECT","STATUS_REMOVE"} and v:
            statuses.append({
                "operation":"REMOVE" if e.get("kind")=="STATUS_REMOVE" else "APPLY",
                "status":str(v).upper(),
                "amount_percent":e.get("amount_percent"),
                "duration_seconds":e.get("duration_seconds"),
                "target_filter":copy.deepcopy(e.get("target_filter") or []),
                "confidence":e.get("confidence"),
                "provenance":copy.deepcopy(e.get("provenance") or {}),
            })
    runtime={
        "triggers":copy.deepcopy(modifier_row.get("triggers") or []),
        "conditions":copy.deepcopy(modifier_row.get("conditions") or []),
        "temporal_rules":copy.deepcopy(modifier_row.get("temporal_rules") or []),
        "counter_rules":copy.deepcopy(modifier_row.get("counter_rules") or []),
        "internal_cooldown":copy.deepcopy(modifier_row.get("internal_cooldown") or []),
        "quest":copy.deepcopy(modifier_row.get("quest") or []),
        "quest_v2":copy.deepcopy(modifier_row.get("quest_v2")),
        "stacking":copy.deepcopy(modifier_row.get("stacking") or []),
    }
    damage_formulas=[copy.deepcopy(m.get("damage_semantics")) for m in mods if isinstance(m.get("damage_semantics"),dict)]
    _raw=modifier_row.get("raw_text") or {}
    _talent_text=" ".join(str(_raw.get(k) or "") for k in ("short_text","full_text","normalized"))
    heal_models=infer_heal_models(_talent_text,"TALENT_TEXT")
    shield_models=infer_shield_models(_talent_text,"TALENT_TEXT")
    return {
        "combat_modifiers":mods,
        "damage_formulas":damage_formulas,
        "heal_models":heal_models,
        "shield_models":shield_models,
        "status_overlays":statuses,
        "control_overlays":[x for x in statuses if x.get("operation")=="APPLY" and x.get("status") in control_set],
        "protection_overlays":[x for x in statuses if x.get("status") in protection_set or x.get("operation")=="REMOVE"],
        "targets":copy.deepcopy(modifier_row.get("targets") or []),
        "runtime_rules":runtime,
    }

def run_merge(
    herodata_path,
    talent_path,
    talent_modifier_path,
    overrides_path,
    subeffect_path,
    final_path,
    report_path,
):
    hd = load(herodata_path)
    tal = load(talent_path)
    tal_mod = load(talent_modifier_path)
    ov = load(overrides_path)
    subeffects = load(subeffect_path)

    spatial = index_by_record_key(ov.get("spatial", {}))
    combat = index_by_record_key(ov.get("combat", {}))
    sub_idx = index_by_record_key(subeffects)
    modifier_idx = index_talent_modifiers(tal_mod)

    heroes = {}
    total = 0
    talents_total = 0
    with_subeffects = 0

    for hero, h in hd.get("heroes", {}).items():
        abilities = []
        for ab in h.get("abilities", []):
            x = copy.deepcopy(ab)
            rk = x.get("record_key")
            x["spatial"] = copy.deepcopy(spatial.get(hero, {}).get(rk))
            x["combat"] = copy.deepcopy(combat.get(hero, {}).get(rk))
            x["subeffects"] = copy.deepcopy(sub_idx.get(hero, {}).get(rk))
            x["resolver_semantics"] = build_resolver_semantics(x["subeffects"], x, x["combat"])
            if x["subeffects"]:
                with_subeffects += 1
            abilities.append(x)
            total += 1

        talent_rows = []
        for t in tal.get("heroes", {}).get(hero, []):
            tx = copy.deepcopy(t)
            tid = tx.get("talent_id")
            tx["modifier_facts"] = copy.deepcopy(
                modifier_idx.get(hero, {}).get(str(tid))
            )
            tx["resolver_semantics"] = build_talent_resolver_semantics(tx.get("modifier_facts"))
            talent_rows.append(tx)
            talents_total += 1

        hr = copy.deepcopy(h)
        hr["abilities"] = abilities
        hr["talents"] = copy.deepcopy(talent_rows)
        hr["talent_facts"] = talent_rows
        heroes[hero] = hr

    final = {
        "metadata": {
            "builder": "ability_builder_v3_12_10",
            "schema_version": 3.20,
            "architecture": "universal_reproducible_builder",
            "manual_overrides": 0,
        },
        "heroes": heroes,
    }
    shield_contradictions=[]
    for hero,h in heroes.items():
        for ab in h.get("abilities",[]) or []:
            rs=ab.get("resolver_semantics") or {}
            if rs.get("has_shield") and not (rs.get("shield_models") or []):
                # Valid direct XML shield effects may have no numeric model yet.
                base=ab.get("subeffects",{}).get("base_subeffects",[]) if isinstance(ab.get("subeffects"),dict) else []
                direct=any(
                    n.get("kind")=="SHIELD" and _validated_direct_shield_instances(n)
                    for n in base if isinstance(n,dict)
                )
                if not direct:
                    shield_contradictions.append({
                        "hero":hero,"ability_id":ab.get("id"),"name":ab.get("name"),
                        "reason":"HAS_SHIELD_WITHOUT_MODEL_OR_DIRECT_XML"
                    })

    report = {
        "summary": {
            "heroes": len(heroes),
            "abilities_total": total,
            "talents_total": talents_total,
            "abilities_with_subeffect_graph": with_subeffects,
            "overrides_applied": len(ov.get("applied", [])),
            "override_missing": len(ov.get("missing", [])),
            "manual_overrides_applied": 0,
            "shield_semantic_contradictions": len(shield_contradictions),
        },
        "shield_semantic_audit": shield_contradictions[:1000],
    }
    final = attach_schema_v1(final)
    schema_report = validate_schema_v1(final)
    report["replay_schema_v1"] = schema_report
    report["summary"]["replay_schema_v1_passed"] = schema_report["passed"]
    report["summary"]["replay_mechanics_total"] = schema_report["mechanics"]
    if not schema_report["passed"]:
        raise RuntimeError("Replay schema v1 validation failed: "+str(schema_report["errors"][:10]))
    save(final_path, final)
    save(report_path, report)
    return report
