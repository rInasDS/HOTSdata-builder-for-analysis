from __future__ import annotations
import copy, hashlib, json, re

SCHEMA_VERSION="1.0.0"
MECHANIC_KINDS={"DAMAGE","HEAL","SHIELD","STATUS","RESOURCE","MOVEMENT","SUMMON","PROTECTION","CONTROL","OTHER"}

def _slug(v):
    s=re.sub(r'[^A-Za-z0-9]+','_',str(v or '')).strip('_').upper()
    return s or 'UNKNOWN'

def _stable_id(owner, kind, discriminator, scope=None):
    scope = str(scope or owner or "UNKNOWN")
    raw=f"{scope}|{owner}|{kind}|{discriminator}"
    h=hashlib.sha1(raw.encode('utf-8')).hexdigest()[:10]
    return f"{_slug(scope)}:{_slug(owner)}:{_slug(kind)}:{h}"

def _prov(source_type, source=None, confidence=None):
    return {"source_type":source_type,"source":copy.deepcopy(source),"confidence":confidence or "DERIVED"}

def _mechanic(owner, kind, discriminator, *, scope=None, formula=None, target=None, conditions=None, timing=None, periodicity=None, provenance=None, relation="DIRECT_OR_UNRESOLVED", data=None):
    return {
        "mechanic_id":_stable_id(owner,kind,discriminator,scope), "kind":kind,
        "owner_id":owner, "execution_relation":relation,
        "formula":copy.deepcopy(formula), "target":copy.deepcopy(target),
        "conditions":copy.deepcopy(conditions or []), "timing":copy.deepcopy(timing),
        "periodicity":copy.deepcopy(periodicity), "provenance":copy.deepcopy(provenance or []),
        "data":copy.deepcopy(data or {}),
    }

def _formula(m):
    if not isinstance(m,dict): return None
    return {k:copy.deepcopy(v) for k,v in m.items() if k in {
        "model","amount_model","value","candidate_value","flat","percent","percent_value",
        "damage_type","source","evidence","value_percent_max_health","value_percent","fraction","raw","constant_id","constant_raw","damage_semantics",
        "source_event","source_damage_type","resource","unit","vital","activation_delay_seconds","modifiers"
    }} or copy.deepcopy(m)

def normalize_ability(hero_id, ability):
    rs=ability.get("resolver_semantics") or {}; owner=str(ability.get("ability_id") or ability.get("id") or ability.get("record_key") or ability.get("name") or "UNKNOWN")
    record_identity=str(ability.get("record_key") or f"{hero_id}|{owner}|{ability.get('slot') or ''}|{ability.get('name') or ''}")
    scope=f"{hero_id}|{record_identity}"
    mechanics=[]
    combat=ability.get("combat") or {}
    for i,m in enumerate(rs.get("damage_models") or []): mechanics.append(_mechanic(owner,"DAMAGE",f"damage:{i}:{m.get('model')}:{m.get('value')}",scope=scope,formula=_formula(m),conditions=m.get("conditions") or [],provenance=[_prov("NORMALIZED_RESOLVER",m,m.get("confidence"))],data={"legacy_index":i}))

    # Typed XML heal/shield instances are authoritative when available. Text-derived
    # models remain fallback evidence for mechanics with no typed runtime instance.
    typed_heal=list(combat.get("heal") or [])
    if typed_heal:
        for i,m in enumerate(typed_heal):
            f=copy.deepcopy(m.get("formula") or {})
            f.setdefault("model", str(m.get("field") or "TYPED_HEAL").upper())
            if m.get("value") is not None: f.setdefault("value",m.get("value"))
            if m.get("source_event") is not None: f.setdefault("source_event",m.get("source_event"))
            if m.get("source_damage_type") is not None: f.setdefault("source_damage_type",m.get("source_damage_type"))
            _typed_heal_timing={"duration":copy.deepcopy(m.get("duration"))} if m.get("duration") is not None else None
            mechanics.append(_mechanic(owner,"HEAL",f"typed_heal:{i}:{m.get('effect_id') or m.get('object_id')}",scope=scope,formula=f,conditions=m.get("conditions") or [],timing=_typed_heal_timing,periodicity=m.get("periodicity"),relation="TYPED_OWNERSHIP",provenance=[_prov("TYPED_COMBAT_GRAPH",m,"HIGH")],data={"object_id":m.get("effect_id") or m.get("object_id"),"field":m.get("field")}))
    else:
        for i,m in enumerate(rs.get("heal_models") or []):
            _per={"kind":"PERIODIC","interval_seconds":m.get("tick_interval_seconds")} if m.get("tick_interval_seconds") is not None else None
            _tim={"duration_seconds":m.get("duration_seconds")} if m.get("duration_seconds") is not None else None
            mechanics.append(_mechanic(owner,"HEAL",f"heal:{i}:{m.get('amount_model')}:{m.get('value')}",scope=scope,formula=_formula(m),target=m.get("target_model"),conditions=m.get("conditions") or [],timing=_tim,periodicity=_per,provenance=[_prov("NORMALIZED_RESOLVER",m,m.get("confidence"))],data={"legacy_index":i,"model_source":m.get("source")}))

    typed_shield=list(combat.get("shield_candidates") or [])
    if typed_shield:
        for i,m in enumerate(typed_shield):
            if m.get("kind")=="DAMAGE_ABSORB_LIMIT":
                f=copy.deepcopy(m.get("formula") or {}); f["model"]="FLAT_ABSORB_LIMIT"; f["value"]=m.get("limit")
            elif m.get("kind")=="DAMAGE_ABSORB_MAX_VITAL_FRACTION":
                f=copy.deepcopy(m.get("formula") or {}); f.setdefault("model","PERCENT_MAX_VITAL"); f.setdefault("vital",m.get("vital")); f.setdefault("value_percent",m.get("value_percent"))
            else:
                f={"model":str(m.get("kind") or "TYPED_SHIELD"),"values":copy.deepcopy(m.get("values") or [])}
            mechanics.append(_mechanic(owner,"SHIELD",f"typed_shield:{i}:{m.get('object_id') or m.get('behavior_id')}",scope=scope,formula=f,conditions=m.get("conditions") or [],timing={"duration":copy.deepcopy(m.get("duration"))} if m.get("duration") is not None else None,periodicity=m.get("periodicity"),relation="TYPED_OWNERSHIP",provenance=[_prov("TYPED_COMBAT_GRAPH",m,"HIGH")],data={"object_id":m.get("object_id"),"evidence_class":m.get("evidence_class")}))
    else:
        for i,m in enumerate(rs.get("shield_models") or []):
            _tim={}
            if m.get("duration_seconds") is not None: _tim["duration_seconds"]=m.get("duration_seconds")
            if m.get("activation_delay_seconds") is not None: _tim["activation_delay_seconds"]=m.get("activation_delay_seconds")
            mechanics.append(_mechanic(owner,"SHIELD",f"shield:{i}:{m.get('amount_model')}:{m.get('value')}",scope=scope,formula=_formula(m),target=m.get("target_model"),conditions=m.get("conditions") or [],timing=_tim or None,provenance=[_prov("NORMALIZED_RESOLVER",m,m.get("confidence"))],data={"legacy_index":i,"effect_class":m.get("effect_class"),"model_source":m.get("source")}))

    # Talent/runtime-gated mechanics stay attached to the owning ability but retain
    # their explicit conditions; they are never collapsed into unconditional base mechanics.
    for kind,key in (("DAMAGE","conditional_damage"),("HEAL","conditional_heal"),("SHIELD","conditional_shield_candidates")):
        for i,m in enumerate(combat.get(key) or []):
            if kind=="SHIELD":
                if m.get("kind")=="DAMAGE_ABSORB_LIMIT":
                    f=copy.deepcopy(m.get("formula") or {}); f["model"]="FLAT_ABSORB_LIMIT"; f["value"]=m.get("limit")
                elif m.get("kind")=="DAMAGE_ABSORB_MAX_VITAL_FRACTION":
                    f=copy.deepcopy(m.get("formula") or {}); f.setdefault("model","PERCENT_MAX_VITAL"); f.setdefault("vital",m.get("vital")); f.setdefault("value_percent",m.get("value_percent"))
                else:
                    f={"model":str(m.get("kind") or "TYPED_SHIELD"),"values":copy.deepcopy(m.get("values") or [])}
            else:
                f=copy.deepcopy(m.get("formula") or {})
                if m.get("value") is not None: f.setdefault("value",m.get("value"))
                if kind=="HEAL": f.setdefault("model",str(m.get("field") or "TYPED_HEAL").upper())
            mechanics.append(_mechanic(owner,kind,f"{key}:{i}:{m.get('effect_id') or m.get('object_id') or m.get('behavior_id')}",scope=scope,formula=f,conditions=m.get("conditions") or [],timing={"duration":copy.deepcopy(m.get("duration"))} if m.get("duration") is not None else None,periodicity=m.get("periodicity"),relation="CONDITIONAL_TYPED_OWNERSHIP",provenance=[_prov("TYPED_COMBAT_GRAPH",m,"HIGH")],data={"object_id":m.get("effect_id") or m.get("object_id"),"talent_gated":bool(any(c.get("talent_ids") for c in (m.get("conditions") or [])))}))
    for status in rs.get("status_effects") or []: mechanics.append(_mechanic(owner,"STATUS",f"status:{status}",scope=scope,data={"status":status},provenance=[_prov("NORMALIZED_RESOLVER")]))
    for status in rs.get("protection") or []: mechanics.append(_mechanic(owner,"PROTECTION",f"protection:{status}",scope=scope,data={"status":status},provenance=[_prov("NORMALIZED_RESOLVER")]))
    for status in rs.get("interrupt_capable") or []: mechanics.append(_mechanic(owner,"CONTROL",f"control:{status}",scope=scope,data={"status":status},provenance=[_prov("NORMALIZED_RESOLVER")]))
    # Preserve semantic flags with no formula as explicit unresolved mechanics instead of silently losing them.
    present={m["kind"] for m in mechanics}
    for kind,key in (("DAMAGE","has_damage"),("HEAL","has_heal"),("SHIELD","has_shield")):
        if rs.get(key) and kind not in present: mechanics.append(_mechanic(owner,kind,f"unresolved:{kind}",scope=scope,provenance=[_prov("RESOLVER_FLAG")],data={"unresolved_formula":True}))
    modifiers=[]
    for group,kind_name in ((combat.get("modifiers") or [],"BEHAVIOR"),(combat.get("conditional_modifiers") or [],"CONDITIONAL_BEHAVIOR")):
        for i,m in enumerate(group):
            talent_ids=sorted({str(tid) for c in (m.get("conditions") or []) for tid in (c.get("talent_ids") or []) if tid})
            modifiers.append({
                "modifier_id":_stable_id(owner,"TYPED_MODIFIER",f"{kind_name}:{i}:{m.get('object_id')}:{m.get('modifier_kind')}",scope),
                "kind":kind_name,"owner_id":owner,"talent_ids":talent_ids,
                "modifier_kind":m.get("modifier_kind"),"value":m.get("value"),"formula":copy.deepcopy(m.get("formula")),
                "conditions":copy.deepcopy(m.get("conditions") or []),"timing":{"duration":copy.deepcopy(m.get("duration"))} if m.get("duration") is not None else None,
                "alignment":m.get("alignment"),"provenance":[_prov("TYPED_COMBAT_GRAPH",m,"HIGH")],
            })
    for i,m in enumerate(combat.get("talent_field_mutations") or []):
        tid=str(m.get("talent_id") or "")
        modifiers.append({
            "modifier_id":_stable_id(owner,"TALENT_FIELD_MUTATION",f"{i}:{tid}:{m.get('target_key')}:{m.get('field')}",scope),
            "kind":"TALENT_FIELD_MUTATION","owner_id":owner,"talent_id":tid or None,
            "talent_ids":[tid] if tid else [],"target_id":m.get("target_id"),"target_key":m.get("target_key"),
            "catalog":m.get("catalog"),"field":m.get("field"),"operation":m.get("operation"),
            "value":m.get("value"),"raw_value":m.get("raw_value"),"formula":copy.deepcopy(m.get("formula")),
            "conditions":[{"relation":"TALENT_SELECTED","talent_ids":[tid]}] if tid else [],
            "provenance":[_prov("TYPED_TALENT_FIELD_MUTATION",m,"HIGH")],
        })
    for i,ov in enumerate(rs.get("talent_combat_overlays") or []):
        modifiers.append({"modifier_id":_stable_id(owner,"TALENT_MODIFIER",f"{i}:{ov.get('talent_id')}",scope),"kind":"TALENT","owner_id":owner,"talent_id":ov.get("talent_id"),"conditions":copy.deepcopy((ov.get("runtime_rules") or {}).get("conditions") or []),"runtime_rules":copy.deepcopy(ov.get("runtime_rules") or {}),"effects":copy.deepcopy(ov.get("modifiers") or []),"provenance":[_prov("TALENT_OVERLAY",ov)]})
    for i,ov in enumerate(rs.get("talent_status_overlays") or []):
        modifiers.append({"modifier_id":_stable_id(owner,"TALENT_STATUS",f"{i}:{ov.get('talent_id')}:{ov.get('status')}",scope),"kind":"TALENT","owner_id":owner,"talent_id":ov.get("talent_id"),"conditions":copy.deepcopy(ov.get("conditions") or []),"runtime_rules":{"triggers":copy.deepcopy(ov.get("triggers") or []),"temporal_rules":copy.deepcopy(ov.get("temporal_rules") or []),"counter_rules":copy.deepcopy(ov.get("counter_rules") or []),"internal_cooldown":copy.deepcopy(ov.get("internal_cooldown") or [])},"effects":[{"operation":ov.get("operation"),"status":ov.get("status"),"duration_seconds":ov.get("duration_seconds"),"amount_percent":ov.get("amount_percent"),"target_filter":copy.deepcopy(ov.get("target_filter") or [])}],"provenance":[_prov("TALENT_STATUS_OVERLAY",ov)]})
    # Deduplicate semantically identical mechanics inside one canonical record before IDs are validated.
    deduped=[]; seen=set()
    for m in mechanics:
        signature=json.dumps({k:m.get(k) for k in ("kind","execution_relation","formula","target","conditions","timing","periodicity","data")},sort_keys=True,ensure_ascii=False,default=str)
        if signature in seen: continue
        seen.add(signature); deduped.append(m)
    mechanics=deduped
    unresolved=[]
    for m in mechanics:
        if m.get("data",{}).get("unresolved_formula"): unresolved.append({"code":f"{m['kind']}_FORMULA_UNRESOLVED","mechanic_id":m["mechanic_id"],"severity":"CRITICAL_FOR_REPLAY"})
    ev=combat.get("execution_evidence") or {}
    classes=ev.get("classes") or ["UNRESOLVED"]
    execution_class=classes[0] if len(classes)==1 else ("COMPOSITE" if classes and classes!=["UNRESOLVED"] else "UNRESOLVED")
    execution={"class":execution_class,"classes":classes,"effect_chain":copy.deepcopy(ev.get("terminals") or []),"source_object_ids":copy.deepcopy(ev.get("starts") or []),"edge_kinds":copy.deepcopy(ev.get("edge_kinds") or [])}
    return {
        "schema_version":SCHEMA_VERSION,
        "identity":{"hero_id":hero_id,"ability_id":owner,"record_key":ability.get("record_key"),"name":ability.get("name"),"slot":ability.get("slot"),"type":ability.get("type"),"aliases":[]},
        "execution":execution,
        "targeting":copy.deepcopy(ability.get("targeting")), "spatial":copy.deepcopy(ability.get("spatial")),
        "timing":{"cooldown":ability.get("cooldown")}, "mechanics":mechanics,"modifiers":modifiers,"conditions":[],
        "provenance":{"normalized_from":"resolver_semantics","legacy_preserved":True},"unresolved":unresolved,
    }

def normalize_talent(hero_id,talent):
    rs=talent.get("resolver_semantics") or {}; tid=str(talent.get("talent_id") or talent.get("id") or talent.get("name") or "UNKNOWN")
    return {"schema_version":SCHEMA_VERSION,"identity":{"hero_id":hero_id,"talent_id":tid,"name":talent.get("name"),"level":talent.get("level")},"targets":copy.deepcopy(rs.get("targets") or []),"runtime_rules":copy.deepcopy(rs.get("runtime_rules") or {}),"combat_modifiers":copy.deepcopy(rs.get("combat_modifiers") or []),"damage_formulas":copy.deepcopy(rs.get("damage_formulas") or []),"heal_models":copy.deepcopy(rs.get("heal_models") or []),"shield_models":copy.deepcopy(rs.get("shield_models") or []),"status_overlays":copy.deepcopy(rs.get("status_overlays") or []),"provenance":{"normalized_from":"talent.resolver_semantics","legacy_preserved":True}}

def attach_schema_v1(final):
    out=copy.deepcopy(final); out.setdefault("metadata",{})["replay_schema_version"]=SCHEMA_VERSION; out["metadata"]["replay_schema_contract"]="mechanics_v1"
    for hero_id,h in out.get("heroes",{}).items():
        for ab in h.get("abilities",[]) or []: ab["replay_v1"]=normalize_ability(hero_id,ab)
        for t in h.get("talents",[]) or []: t["replay_v1"]=normalize_talent(hero_id,t)
    return out

def validate_schema_v1(final):
    errors=[]; ids=set(); abilities=mechanics=0
    for hero,h in final.get("heroes",{}).items():
        for ab in h.get("abilities",[]) or []:
            abilities+=1; v=ab.get("replay_v1")
            if not isinstance(v,dict): errors.append(f"{hero}/{ab.get('name')}: missing replay_v1"); continue
            for m in v.get("mechanics",[]) or []:
                mechanics+=1; mid=m.get("mechanic_id")
                if not mid: errors.append(f"{hero}/{ab.get('name')}: mechanic without id")
                elif mid in ids: errors.append(f"duplicate mechanic_id {mid}")
                ids.add(mid)
                if m.get("kind") not in MECHANIC_KINDS: errors.append(f"{mid}: bad kind")
                if not m.get("owner_id"): errors.append(f"{mid}: missing owner")
    return {"schema_version":SCHEMA_VERSION,"abilities":abilities,"mechanics":mechanics,"unique_mechanic_ids":len(ids),"errors":errors,"passed":not errors}
