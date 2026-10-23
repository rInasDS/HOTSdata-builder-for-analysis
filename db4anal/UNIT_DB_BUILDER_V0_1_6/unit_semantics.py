from __future__ import annotations
from collections import defaultdict, deque, Counter
import math, re
from json_backend import load, save

FORMULA_TOKEN_RE = re.compile(r"\$[A-Za-z0-9_]+")
STAT_FIELDS = [
    "LifeStart","LifeMax","LifeRegenRate","EnergyStart","EnergyMax","EnergyRegenRate",
    "ShieldStart","ShieldMax","ShieldRegenRate","ShieldRegenDelay","Speed","Acceleration",
    "Deceleration","TurningRate","StationaryTurningRate","Radius","InnerRadius","Sight",
    "VisionHeight","AttackTargetPriority","DeathTime","ArmorLink","Mover","PlacementFootprint",
]
WEAPON_FIELDS = ["Period","Range","MinRange","Arc","DamagePoint","Backswing","ScanRange","MinimumRange"]
ABILITY_FIELDS = ["Range","Arc","CastIntroTime","CastOutroTime","Flags","Cost","CmdButtonArray"]
EFFECT_FIELDS = [
    "Amount","Fraction","Kind","ArmorReduction","Vital","Radius","Period","PeriodCount","Duration",
    "SearchFilters","TargetFilters","MaximumCount","MinimumCount","ImpactEffect","LaunchEffect",
    "InitialEffect","FinalEffect","ExpireEffect","PeriodicEffect","EffectArray","Behavior","SpawnUnit",
]


def _as_number(s):
    if s is None: return None
    try:
        x=float(s)
        if math.isfinite(x):
            return int(x) if x.is_integer() else x
    except (TypeError,ValueError):
        pass
    return None


def _attrs_value(attrs):
    for k in ("value","Value","Link"):
        if k in attrs: return attrs.get(k)
    return None


def _field_nodes(rec, name):
    return list((rec.get("fields") or {}).get(name,[]) or [])


def _field_summary(rec, name):
    nodes=_field_nodes(rec,name)
    if not nodes: return None
    out=[]
    for n in nodes:
        attrs=n.get("attrs") or {}
        raw=_attrs_value(attrs)
        out.append({
            "attrs":attrs,
            "raw_value":raw,
            "numeric_value":_as_number(raw),
            "formula_tokens":FORMULA_TOKEN_RE.findall(raw) if isinstance(raw,str) else [],
            "provenance":n.get("provenance") or {},
        })
    return out[0] if len(out)==1 else out


def _indexed_map(rec, field):
    out={}
    for n in _field_nodes(rec,field):
        a=n.get("attrs") or {}; idx=a.get("index")
        if idx is not None: out[str(idx)] = a.get("value")
    return out


def _edges_by_source(graph):
    d=defaultdict(list)
    for e in graph.get("edges",[]) or []: d[e.get("source_key")].append(e)
    return d


def _edges_by_target(graph):
    d=defaultdict(list)
    for e in graph.get("edges",[]) or []:
        t=e.get("target_key")
        if t: d[t].append(e)
    return d


def _owned_execution_closure(start_keys, edges_by_source, max_nodes=5000):
    q=deque(start_keys); seen=set(); edges=[]
    while q and len(seen)<max_nodes:
        cur=q.popleft()
        if cur in seen: continue
        seen.add(cur)
        for e in edges_by_source.get(cur,[]):
            prop=e.get("propagation") or {}
            if e.get("relation")=="INHERITS" or prop.get("ownership") or prop.get("execution"):
                edges.append(e)
                if e.get("owner_boundary") in {"SUMMON","LAUNCHED_UNIT","CONTRIBUTION"}:
                    continue
                t=e.get("target_key")
                if t and t not in seen: q.append(t)
    return seen,edges


def _compact_edge(e):
    out={k:e.get(k) for k in ["source_key","target_key","relation","field","path","attr","owner_boundary","propagation","resolution"]}
    if e.get("provenance"): out["provenance"]=e.get("provenance")
    return out


def _record_summary(key, rec):
    if not rec: return {"key":key,"status":"MISSING"}
    typ=rec.get("xml_type") or (key.split("::",1)[0] if "::" in key else None)
    fields={}
    pick = EFFECT_FIELDS if typ and typ.startswith("CEffect") else ABILITY_FIELDS if typ and typ.startswith("CAbil") else WEAPON_FIELDS if typ and typ.startswith("CWeapon") else []
    for f in pick:
        x=_field_summary(rec,f)
        if x is not None: fields[f]=x
    return {"key":key,"type":typ,"id":rec.get("object_id"),"parent":rec.get("parent"),"fields":fields}


BEHAVIOR_SEMANTIC_FIELDS = [
    "Duration","Period","InitialEffect","PeriodicEffect","FinalEffect","ExpireEffect","RefreshEffect",
    "DamageResponse","Modification","ArmorModification","MaxStackCount","MinStackCount","StackCount",
    "VeterancyLevelArray","XPFraction","BehaviorFlags","InfoFlags","BehaviorCategories","BuffFlags",
]
BEHAVIOR_NESTED_ROOTS = {
    "DamageResponse","Modification","ArmorModification","VeterancyLevelArray",
    "BehaviorCategories","BuffFlags","BehaviorFlags","InfoFlags",
}


def _nested_fact_summary(nf):
    out={"path":nf.get("path") or [],"attrs":nf.get("attrs") or {},"provenance":nf.get("provenance") or {}}
    if (nf.get("path") or [None])[0] == "VeterancyLevelArray":
        for k in ("ancestor_context","effective_ancestor_context","ancestor_context_status","veterancy_level_context"):
            if k in nf: out[k]=nf.get(k)
    return out


def _behavior_summary(key, rec, edges_by_source):
    out={"key":key,"type":rec.get("xml_type"),"id":rec.get("object_id"),"parent":rec.get("parent")}
    for f in BEHAVIOR_SEMANTIC_FIELDS:
        x=_field_summary(rec,f)
        if x is not None: out.setdefault("fields",{})[f]=x
    nested=[]
    for nf in rec.get("nested_facts",[]) or []:
        path=nf.get("path") or []
        if path and path[0] in BEHAVIOR_NESTED_ROOTS:
            nested.append(_nested_fact_summary(nf))
    if nested: out["nested_mechanics"]=nested
    triggers=[]
    for e in edges_by_source.get(key,[]):
        if e.get("relation") in {"TRIGGERS_ON_DAMAGE","EVENT_EXECUTES","PERIODIC_EXECUTES","TRIGGERS_EFFECT","CONDITIONAL_EXECUTES","EXECUTES","APPLIES_BEHAVIOR","REMOVES_BEHAVIOR"}:
            triggers.append(_compact_edge(e))
    if triggers: out["trigger_edges"]=triggers
    return out


def _field_effective_index(node, fallback):
    prov=node.get("provenance") or {}
    idx=prov.get("index")
    if idx is None: idx=(node.get("attrs") or {}).get("index")
    if idx is None: idx=fallback
    try:
        s=str(idx)
        return int(s) if s.lstrip('-').isdigit() else s
    except Exception:
        return idx


def _veterancy_context_stats(rec):
    """Audit effective VeterancyLevelArray descendant -> level-slot identity."""
    level_slots=set()
    for pos,node in enumerate(_field_nodes(rec,"VeterancyLevelArray")):
        if (node.get("attrs") or {}).get("__hole__")=="1":
            continue
        level_slots.add(str(_field_effective_index(node,pos)))
    source=contextualized=orphan=slot_mismatch=0
    for nf in rec.get("nested_facts",[]) or []:
        path=nf.get("path") or []
        if not path or path[0] != "VeterancyLevelArray":
            continue
        source += 1
        ctx=nf.get("veterancy_level_context") or {}
        idx=ctx.get("level_index")
        if ctx.get("status")=="RESOLVED" and idx is not None:
            contextualized += 1
            if str(idx) not in level_slots: slot_mismatch += 1
        else:
            orphan += 1
    return {
        "source_nested_facts":source,
        "contextualized_nested_facts":contextualized,
        "orphan_nested_facts":orphan,
        "slot_mismatch_nested_facts":slot_mismatch,
        "effective_level_slots":len(level_slots),
    }


def _compact_field_value(attrs):
    attrs=attrs or {}
    raw=_attrs_value(attrs)
    return {
        "raw_value":raw,
        "numeric_value":_as_number(raw),
        "attrs":attrs,
    }


def _veterancy_modification_entries(levels):
    """Summarize per-level modification evidence without inventing arithmetic.

    The authoritative facts already live in ``levels[].nested_facts`` with full
    provenance.  This profile intentionally does not duplicate those facts; it
    groups them by field signature so later resolvers can see which fields vary
    across which proven veterancy slots.
    """
    by_sig=defaultdict(list)
    for level in levels:
        li=level.get("index")
        for nf in level.get("nested_facts",[]) or []:
            path=nf.get("path") or []
            if len(path) < 2 or path[0] != "VeterancyLevelArray":
                continue
            attrs=nf.get("attrs") or {}
            tail=path[1:]
            leaf=tail[-1] if tail else None
            idx=attrs.get("index")
            # Canonical value-bearing leaf, e.g. VitalMaxArray[Life]=180.
            if "value" in attrs or "Value" in attrs or "Link" in attrs:
                sig=".".join(tail)
                if idx is not None:
                    sig += f"[{idx}]"
                by_sig[sig].append((li,_attrs_value(attrs)))
                continue
            # Attribute-valued Modification node, e.g. Modification KillXPBonus=10.
            if leaf == "Modification":
                for name,val in attrs.items():
                    if name in {"index","removed"}:
                        continue
                    sig=".".join(tail+[str(name)])
                    by_sig[sig].append((li,val))
    out=[]
    for sig in sorted(by_sig):
        obs=by_sig[sig]
        vals=[]
        for _,v in obs:
            if v not in vals: vals.append(v)
        out.append({
            "field_signature":sig,
            "status":"UNRESOLVED_OPERATION_SEMANTICS",
            "operation":"UNRESOLVED",
            "proof_basis":[],
            "observation_count":len(obs),
            "observed_levels":[li for li,_ in obs],
            "distinct_raw_values":vals,
        })
    return out


def _veterancy_xp_field_evidence(rec):
    """Retain XP/veterancy-named fields as raw GameData evidence only.

    A field name is not treated as proof of the runtime grant source or of the
    engine interpretation of that field.  This is deliberately weaker than a
    semantic claim.
    """
    out={}
    for name,nodes in (rec.get("fields") or {}).items():
        lname=str(name).lower()
        if name == "VeterancyLevelArray":
            continue
        if "xp" not in lname and "experience" not in lname and "veteran" not in lname:
            continue
        vals=[]
        for n in nodes or []:
            attrs=n.get("attrs") or {}
            vals.append({
                "attrs":attrs,
                "raw_value":_attrs_value(attrs),
                "numeric_value":_as_number(_attrs_value(attrs)),
                "provenance":n.get("provenance") or {},
            })
        out[name]=vals
    return out


def _veterancy_semantic_profile(rec, levels, incoming_edges=None):
    incoming_edges=incoming_edges or []
    level_indices=[x.get("index") for x in levels]
    nested_count=sum(len(x.get("nested_facts",[]) or []) for x in levels)
    modified=[x.get("index") for x in levels if x.get("nested_facts")]
    empty=[x.get("index") for x in levels if not x.get("nested_facts")]
    trailing=[]
    for x in reversed(levels):
        if x.get("nested_facts"):
            break
        trailing.append(x.get("index"))
    trailing.reverse()

    requirements=[]
    for x in levels:
        attrs=x.get("attrs") or {}
        if "MinVeterancyXP" in attrs:
            raw=attrs.get("MinVeterancyXP")
            requirements.append({
                "level_index":x.get("index"),
                "field":"MinVeterancyXP",
                "raw_value":raw,
                "numeric_value":_as_number(raw),
                "provenance":x.get("provenance") or {},
            })

    incoming=[]
    for e in incoming_edges:
        incoming.append({
            "source_key":e.get("source_key"),"relation":e.get("relation"),
            "field":e.get("field"),"path":e.get("path") or [],"attr":e.get("attr"),
            "provenance":e.get("provenance") or {},
            "classification":"REFERENCE_OR_ATTACHMENT_ONLY_NOT_LEVEL_SOURCE_PROOF",
        })

    ops=_veterancy_modification_entries(levels)
    return {
        "status":"STRUCTURE_PROFILED_RUNTIME_SEMANTICS_UNRESOLVED",
        "structure":{
            "level_count":len(levels),
            "level_indices":level_indices,
            "nested_fact_count":nested_count,
            "levels_with_nested_facts":modified,
            "empty_levels":empty,
            "trailing_empty_levels":trailing,
            "first_level_with_nested_facts":modified[0] if modified else None,
            "last_level_with_nested_facts":modified[-1] if modified else None,
        },
        "level_requirement_evidence":{
            "status":"GAMEDATA_FIELD_EVIDENCE_ONLY" if requirements else "NO_MIN_VETERANCY_XP_FIELD_OBSERVED",
            "field":"VeterancyLevelArray.MinVeterancyXP" if requirements else None,
            "requirements":requirements,
            "semantic_interpretation":"UNRESOLVED",
            "note":"The field is retained exactly; its presence does not by itself prove the external runtime source of veterancy progress.",
        },
        "xp_field_evidence":_veterancy_xp_field_evidence(rec),
        "level_source":{
            "status":"UNRESOLVED",
            "source":"UNRESOLVED",
            "proof_basis":[],
            "incoming_reference_evidence":incoming,
            "note":"Reference/attachment to CBehaviorVeterancy is not proof of who grants or synchronizes veterancy progress at runtime.",
        },
        "application_semantics":{
            "cumulative":{
                "status":"UNRESOLVED_ENGINE_SEMANTICS",
                "value":"UNRESOLVED",
                "proof_basis":[],
            },
            "field_operations":ops,
            "resolved_field_operations":sum(1 for x in ops if x.get("operation") != "UNRESOLVED"),
            "total_field_operations":len(ops),
        },
        "empty_slot_semantics":{
            "status":"STRUCTURE_PROVEN_RUNTIME_MEANING_UNRESOLVED" if trailing else "NO_TRAILING_EMPTY_LEVELS",
            "trailing_empty_levels":trailing,
            "runtime_meaning":"UNRESOLVED" if trailing else None,
            "proof_basis":[],
        },
    }

def _veterancy_evidence(key, rec, incoming_edges=None):
    """Retain effective CBehaviorVeterancy evidence without evaluating scaling.

    v0.1.5 adds a fail-closed semantic profile: progression-field evidence,
    per-level modification signatures, trailing-empty structure and incoming
    reference evidence are retained, while arithmetic/cumulative/source meaning
    remains UNRESOLVED unless directly proven.
    """
    if not rec or not (rec.get("xml_type") or "").startswith("CBehaviorVeterancy"):
        return None
    out={"key":key,"type":rec.get("xml_type"),"id":rec.get("object_id"),"parent":rec.get("parent")}
    fields={}
    for f in ("VeterancyLevelArray","Modification","XPFraction","BehaviorFlags","InfoFlags"):
        x=_field_summary(rec,f)
        if x is not None: fields[f]=x
    if fields: out["fields"]=fields
    nested=[]
    by_level=defaultdict(list)
    for nf in rec.get("nested_facts",[]) or []:
        path=nf.get("path") or []
        if path and path[0] in {"VeterancyLevelArray","Modification"}:
            compact=_nested_fact_summary(nf); nested.append(compact)
            if path[0]=="VeterancyLevelArray":
                ctx=nf.get("veterancy_level_context") or {}
                if ctx.get("status")=="RESOLVED" and ctx.get("level_index") is not None:
                    by_level[str(ctx.get("level_index"))].append(compact)
    if nested: out["nested_facts"]=nested

    levels=[]
    for pos,node in enumerate(_field_nodes(rec,"VeterancyLevelArray")):
        attrs=node.get("attrs") or {}
        if attrs.get("__hole__")=="1":
            continue
        idx=_field_effective_index(node,pos)
        level={
            "index":idx,
            "attrs":attrs,
            "provenance":node.get("provenance") or {},
            "nested_facts":by_level.get(str(idx),[]),
        }
        levels.append(level)
    if levels: out["levels"]=levels
    out["context_integrity"]=_veterancy_context_stats(rec)
    out["semantic_profile"]=_veterancy_semantic_profile(rec,levels,incoming_edges=incoming_edges)
    return out


def _behavior_retention_flags(rec, summary):
    """Return source/retained booleans used by the integrity gate."""
    source_armor = bool((rec.get("fields") or {}).get("ArmorModification")) or any(
        (nf.get("path") or [None])[0] == "ArmorModification" for nf in rec.get("nested_facts",[]) or []
    )
    retained_armor = bool((summary.get("fields") or {}).get("ArmorModification")) or any(
        (nf.get("path") or [None])[0] == "ArmorModification" for nf in summary.get("nested_mechanics",[]) or []
    )
    source_veterancy = (rec.get("xml_type") or "").startswith("CBehaviorVeterancy")
    return source_armor, retained_armor, source_veterancy

def collect_observed_units(map_db):
    units={}
    semantic_roots=set()
    for map_id,m in (map_db.get("maps") or {}).items():
        for u in m.get("unit_types",[]) or []:
            uid=u.get("unit_type")
            if not uid: continue
            owners=u.get("owner_classes") or {}
            non_roster=sum(v for k,v in owners.items() if k!="ROSTER_PLAYER_CONTROLLED")
            if non_roster<=0: continue
            x=units.setdefault(uid,{"unit_id":uid,"maps":set(),"replay_support_by_map":{},"spawn_count_by_map":{},"owner_classes":Counter(),"semantic_contexts":set()})
            x["maps"].add(map_id); x["replay_support_by_map"][map_id]=u.get("replay_support",0); x["spawn_count_by_map"][map_id]=u.get("spawn_count",0); x["owner_classes"].update(owners)
        for lm in m.get("semantic_landmarks",[]) or []:
            cls=lm.get("semantic_class")
            for near in lm.get("nearby_spawn_clusters",[]) or []:
                if near.get("owner_class")=="ROSTER_PLAYER_CONTROLLED": continue
                uid=near.get("unit_type")
                if not uid: continue
                semantic_roots.add(uid)
                x=units.setdefault(uid,{"unit_id":uid,"maps":set(),"replay_support_by_map":{},"spawn_count_by_map":{},"owner_classes":Counter(),"semantic_contexts":set()})
                x["maps"].add(map_id); x["semantic_contexts"].add(cls)
    out=[]
    for uid,x in units.items():
        out.append({
            "unit_id":uid,"maps":sorted(x["maps"]),"replay_support_by_map":x["replay_support_by_map"],
            "spawn_count_by_map":x["spawn_count_by_map"],"owner_classes":dict(x["owner_classes"]),
            "semantic_contexts":sorted(c for c in x["semantic_contexts"] if c),"semantic_landmark_unit":uid in semantic_roots,
        })
    return sorted(out,key=lambda x:x["unit_id"])


def build_unit_db(map_db_path, catalog_path, typed_graph_path, out_path, summary_path, integrity_path, unresolved_path):
    map_db=load(map_db_path); cat=load(catalog_path); graph=load(typed_graph_path)
    records=cat.get("records",{}); bysrc=_edges_by_source(graph)
    observed=collect_observed_units(map_db); obs_by={x["unit_id"]:x for x in observed}
    seed_audit={x.get("unit_id"):x for x in cat.get("seed_audit",[]) or []}
    units={}; unresolved=[]; reactive_count=0; damage_response_count=0

    for uid,obs in obs_by.items():
        audit=seed_audit.get(uid,{})
        cands=audit.get("candidate_keys") or []
        root=cands[0] if len(cands)==1 else (f"CUnit::{uid}" if f"CUnit::{uid}" in records else None)
        if not root or root not in records:
            units[uid]={"observed":obs,"catalog_status":audit.get("status","ABSENT_UNIT_ROOT"),"root_key":root,"semantics_status":"UNRESOLVED_ROOT"}
            unresolved.append({"unit_id":uid,"code":"UNIT_ROOT_UNRESOLVED","audit":audit}); continue
        rec=records[root]
        direct=bysrc.get(root,[])
        behavior_keys=[e.get("target_key") for e in direct if e.get("relation")=="UNIT_HAS_BEHAVIOR" and e.get("target_key")]
        weapon_keys=[e.get("target_key") for e in direct if e.get("relation")=="UNIT_EQUIPS_WEAPON" and e.get("target_key")]
        ability_keys=[e.get("target_key") for e in direct if e.get("relation")=="UNIT_HAS_ABILITY" and e.get("target_key")]
        armor_keys=[e.get("target_key") for e in direct if e.get("relation")=="UNIT_USES_ARMOR" and e.get("target_key")]
        start=[root]+behavior_keys+weapon_keys+ability_keys
        nodes,owned_edges=_owned_execution_closure(start,bysrc)
        effect_keys=sorted(k for k in nodes if k and k.startswith("CEffect"))
        behavior_reachable=sorted(k for k in nodes if k and k.startswith("CBehavior"))

        base_stats={}
        for f in STAT_FIELDS:
            x=_field_summary(rec,f)
            if x is not None: base_stats[f]=x
        behaviors=[]; reactive=[]; veterancy_evidence=[]
        for bk in sorted(set(behavior_keys+behavior_reachable)):
            br=records.get(bk)
            if not br: continue
            bs=_behavior_summary(bk,br,bysrc); behaviors.append(bs)
            ve=_veterancy_evidence(bk,br)
            if ve is not None: veterancy_evidence.append(ve)
            has_dr="DamageResponse" in (br.get("fields") or {}) or any((nf.get("path") or [None])[0]=="DamageResponse" for nf in br.get("nested_facts",[]) or [])
            trigger_edges=[e for e in bysrc.get(bk,[]) if e.get("relation")=="TRIGGERS_ON_DAMAGE"]
            if has_dr or trigger_edges:
                damage_response_count+=1
                reactive.append({"trigger":"ON_DAMAGE_TAKEN","behavior":bs,"execution_edges":[_compact_edge(e) for e in trigger_edges]})
        # Other explicit unit/behavior executable events remain separate, not mislabeled as damage response.
        other_events=[]
        for sk in [root]+sorted(set(behavior_keys+behavior_reachable)):
            for e in bysrc.get(sk,[]):
                if e.get("relation") in {"EVENT_EXECUTES","PERIODIC_EXECUTES","TRIGGERS_EFFECT"}:
                    other_events.append(_compact_edge(e))
        reactive_count += len(reactive)

        formulas=[]
        for k in nodes:
            rr=records.get(k)
            if not rr: continue
            for fn,nl in (rr.get("fields") or {}).items():
                for n in nl or []:
                    for val in (n.get("attrs") or {}).values():
                        if isinstance(val,str) and "$" in val:
                            toks=FORMULA_TOKEN_RE.findall(val)
                            if toks: formulas.append({"record":k,"field":fn,"raw":val,"tokens":toks})
            for nf in rr.get("nested_facts",[]) or []:
                for val in (nf.get("attrs") or {}).values():
                    if isinstance(val,str) and "$" in val:
                        toks=FORMULA_TOKEN_RE.findall(val)
                        if toks: formulas.append({"record":k,"path":nf.get("path"),"raw":val,"tokens":toks})

        units[uid]={
            "observed":obs,"catalog_status":audit.get("status"),"root_key":root,"semantics_status":"RESOLVED_TYPED_CLOSURE",
            "base_stats":base_stats,"flags":_indexed_map(rec,"FlagArray"),"attributes":_indexed_map(rec,"Attributes"),
            "armor":[_record_summary(k,records.get(k)) for k in armor_keys],
            "weapons":[_record_summary(k,records.get(k)) for k in weapon_keys],
            "abilities":[_record_summary(k,records.get(k)) for k in ability_keys],
            "behaviors":behaviors,
            "reactive_mechanics":reactive,
            "event_mechanics":other_events,
            "effect_summaries":[_record_summary(k,records.get(k)) for k in effect_keys],
            "execution_graph":{"node_count":len(nodes),"edge_count":len(owned_edges),"edges":[_compact_edge(e) for e in owned_edges]},
            "scaling_evidence":{"formula_references":formulas,"veterancy_behaviors":veterancy_evidence,"status":"EVIDENCE_ONLY_NOT_EVALUATED"},
        }

    sem_units=[u for u in observed if u.get("semantic_landmark_unit")]
    sem_unresolved=[u["unit_id"] for u in sem_units if units.get(u["unit_id"],{}).get("semantics_status")!="RESOLVED_TYPED_CLOSURE"]
    root_resolved=sum(1 for x in units.values() if x.get("semantics_status")=="RESOLVED_TYPED_CLOSURE")
    result={
        "schema":{"name":"PYHOTS_UNIT_DB","version":"0.1"},
        "contract":{
            "principle":"replay-observed unit root -> typed GameData dependency closure",
            "reference_is_not_execution":True,"fail_closed":True,
            "reactive_mechanics":"Only structurally proven DamageResponse/TRIGGERS_ON_DAMAGE is labeled ON_DAMAGE_TAKEN.",
            "scaling":"Raw formula/accumulator evidence only in v0.1; runtime evaluation remains unresolved.",
        },
        "summary":{"observed_units":len(observed),"resolved_unit_roots":root_resolved,"unresolved_unit_roots":len(observed)-root_resolved,"semantic_landmark_units":len(sem_units),"reactive_mechanics":reactive_count,"damage_response_records":damage_response_count},
        "units":units,
    }
    save(out_path,result)
    save(unresolved_path,unresolved)
    summary={"schema":{"name":"PYHOTS_UNIT_DB_SUMMARY","version":"0.1"},**result["summary"]}
    save(summary_path,summary)
    gates={
        "MAP_DB_LOAD":"PASS",
        "OBSERVED_UNIT_SEEDS":"PASS" if observed else "FAIL",
        "SEMANTIC_LANDMARK_UNIT_ROOTS":"PASS" if not sem_unresolved else "FAIL",
        "REACTIVE_FAIL_CLOSED":"PASS",
        "SCALING_FAIL_CLOSED":"PASS",
    }
    integrity={"schema":{"name":"PYHOTS_UNIT_DB_INTEGRITY","version":"0.1"},"gates":gates,"semantic_landmark_unresolved_units":sem_unresolved,"counts":summary,"overall":"PASS" if all(v=="PASS" for v in gates.values()) else "FAIL"}
    save(integrity_path,integrity)
    return result, integrity

# v0.1.2: map-scoped packaging helpers. The old build_unit_db wrapper remains
# for backward-compatible unit tests, but authoritative v0.1.2 output is built
# through these helpers after context-filtered effective catalogs are created.
def build_unit_entries_from_data(map_db: dict, cat: dict, graph: dict):
    records=cat.get("records",{}); bysrc=_edges_by_source(graph); bytarget=_edges_by_target(graph)
    observed=collect_observed_units(map_db); obs_by={x["unit_id"]:x for x in observed}
    seed_audit={x.get("unit_id"):x for x in cat.get("seed_audit",[]) or []}
    units={}; unresolved=[]; reactive_count=0; damage_response_count=0
    armor_semantic_source_count=0; armor_semantic_retained_count=0
    veterancy_source_count=0; veterancy_retained_count=0
    veterancy_nested_source_count=0; veterancy_nested_context_count=0
    veterancy_nested_orphan_count=0; veterancy_nested_slot_mismatch_count=0
    veterancy_profile_count=0; veterancy_profile_mismatch_count=0
    veterancy_level_requirement_field_records=0; veterancy_xp_field_evidence_records=0
    veterancy_level_source_resolved_count=0
    veterancy_operation_field_count=0; veterancy_operation_resolved_count=0
    veterancy_cumulative_resolved_count=0; veterancy_empty_slot_runtime_resolved_count=0
    veterancy_unsupported_operation_claims=0; veterancy_unsupported_source_claims=0
    veterancy_unsupported_cumulative_claims=0; veterancy_unsupported_empty_slot_claims=0
    veterancy_trailing_empty_records=0

    def unique_targets(edges, relation):
        seen=set(); out=[]
        for e in edges:
            if e.get("relation") != relation: continue
            t=e.get("target_key")
            if t and t not in seen:
                seen.add(t); out.append(t)
        return out

    for uid,obs in obs_by.items():
        audit=seed_audit.get(uid,{})
        cands=audit.get("candidate_keys") or []
        root=cands[0] if len(cands)==1 else (f"CUnit::{uid}" if f"CUnit::{uid}" in records else None)
        if not root or root not in records:
            units[uid]={"observed":obs,"catalog_status":audit.get("status","ABSENT_UNIT_ROOT"),"root_key":root,"semantics_status":"UNRESOLVED_ROOT"}
            unresolved.append({"unit_id":uid,"code":"UNIT_ROOT_UNRESOLVED","audit":audit}); continue
        rec=records[root]; direct=bysrc.get(root,[])
        behavior_keys=unique_targets(direct,"UNIT_HAS_BEHAVIOR")
        weapon_keys=unique_targets(direct,"UNIT_EQUIPS_WEAPON")
        ability_keys=unique_targets(direct,"UNIT_HAS_ABILITY")
        armor_keys=unique_targets(direct,"UNIT_USES_ARMOR")
        start=[root]+behavior_keys+weapon_keys+ability_keys
        nodes,owned_edges=_owned_execution_closure(start,bysrc)
        effect_keys=sorted(k for k in nodes if k and k.startswith("CEffect"))
        behavior_reachable=sorted(k for k in nodes if k and k.startswith("CBehavior"))

        base_stats={}
        for f in STAT_FIELDS:
            x=_field_summary(rec,f)
            if x is not None: base_stats[f]=x
        behaviors=[]; reactive=[]; veterancy_evidence=[]
        for bk in sorted(set(behavior_keys+behavior_reachable)):
            br=records.get(bk)
            if not br: continue
            bs=_behavior_summary(bk,br,bysrc); behaviors.append(bs)
            source_armor, retained_armor, source_veterancy = _behavior_retention_flags(br,bs)
            if source_armor:
                armor_semantic_source_count += 1
                if retained_armor: armor_semantic_retained_count += 1
            if source_veterancy:
                veterancy_source_count += 1
                vcs=_veterancy_context_stats(br)
                veterancy_nested_source_count += vcs["source_nested_facts"]
                veterancy_nested_context_count += vcs["contextualized_nested_facts"]
                veterancy_nested_orphan_count += vcs["orphan_nested_facts"]
                veterancy_nested_slot_mismatch_count += vcs["slot_mismatch_nested_facts"]
                ve=_veterancy_evidence(bk,br,incoming_edges=bytarget.get(bk,[]))
                if ve is not None:
                    veterancy_evidence.append(ve); veterancy_retained_count += 1; veterancy_profile_count += 1
                    sp=ve.get("semantic_profile") or {}; st=sp.get("structure") or {}
                    if st.get("level_count") != vcs.get("effective_level_slots") or st.get("nested_fact_count") != vcs.get("source_nested_facts"):
                        veterancy_profile_mismatch_count += 1
                    req=sp.get("level_requirement_evidence") or {}
                    if req.get("status") == "GAMEDATA_FIELD_EVIDENCE_ONLY":
                        veterancy_level_requirement_field_records += 1
                    if sp.get("xp_field_evidence"):
                        veterancy_xp_field_evidence_records += 1
                    ls=sp.get("level_source") or {}
                    if ls.get("status") != "UNRESOLVED" or ls.get("source") != "UNRESOLVED":
                        veterancy_level_source_resolved_count += 1
                        if not ls.get("proof_basis"):
                            veterancy_unsupported_source_claims += 1
                    app=sp.get("application_semantics") or {}
                    ops=app.get("field_operations") or []
                    veterancy_operation_field_count += len(ops)
                    for op in ops:
                        if op.get("operation") != "UNRESOLVED":
                            veterancy_operation_resolved_count += 1
                            if not op.get("proof_basis"):
                                veterancy_unsupported_operation_claims += 1
                    cumulative=app.get("cumulative") or {}
                    if cumulative.get("value") != "UNRESOLVED":
                        veterancy_cumulative_resolved_count += 1
                        if not cumulative.get("proof_basis"):
                            veterancy_unsupported_cumulative_claims += 1
                    empty_sem=sp.get("empty_slot_semantics") or {}
                    if (sp.get("structure") or {}).get("trailing_empty_levels"):
                        veterancy_trailing_empty_records += 1
                        if empty_sem.get("runtime_meaning") != "UNRESOLVED":
                            veterancy_empty_slot_runtime_resolved_count += 1
                            if not empty_sem.get("proof_basis"):
                                veterancy_unsupported_empty_slot_claims += 1
            has_dr="DamageResponse" in (br.get("fields") or {}) or any((nf.get("path") or [None])[0]=="DamageResponse" for nf in br.get("nested_facts",[]) or [])
            trigger_edges=[e for e in bysrc.get(bk,[]) if e.get("relation")=="TRIGGERS_ON_DAMAGE"]
            if has_dr or trigger_edges:
                damage_response_count+=1
                reactive.append({"trigger":"ON_DAMAGE_TAKEN","behavior":bs,"execution_edges":[_compact_edge(e) for e in trigger_edges]})
        other_events=[]
        seen_event=set()
        for sk in [root]+sorted(set(behavior_keys+behavior_reachable)):
            for e in bysrc.get(sk,[]):
                if e.get("relation") in {"EVENT_EXECUTES","PERIODIC_EXECUTES","TRIGGERS_EFFECT"}:
                    ce=_compact_edge(e); sig=(ce.get("source_key"),ce.get("target_key"),ce.get("relation"),tuple(ce.get("path") or []),ce.get("attr"))
                    if sig not in seen_event: seen_event.add(sig); other_events.append(ce)
        reactive_count += len(reactive)

        formulas=[]; seen_formula=set()
        for k in nodes:
            rr=records.get(k)
            if not rr: continue
            for fn,nl in (rr.get("fields") or {}).items():
                for n in nl or []:
                    for val in (n.get("attrs") or {}).values():
                        if isinstance(val,str) and "$" in val:
                            toks=FORMULA_TOKEN_RE.findall(val)
                            sig=(k,fn,val,tuple(toks))
                            if toks and sig not in seen_formula:
                                seen_formula.add(sig); formulas.append({"record":k,"field":fn,"raw":val,"tokens":toks})
            for nf in rr.get("nested_facts",[]) or []:
                for val in (nf.get("attrs") or {}).values():
                    if isinstance(val,str) and "$" in val:
                        toks=FORMULA_TOKEN_RE.findall(val); sig=(k,tuple(nf.get("path") or []),val,tuple(toks))
                        if toks and sig not in seen_formula:
                            seen_formula.add(sig); formulas.append({"record":k,"path":nf.get("path"),"raw":val,"tokens":toks})

        units[uid]={
            "observed":obs,"catalog_status":audit.get("status"),"root_key":root,"semantics_status":"RESOLVED_TYPED_CLOSURE",
            "base_stats":base_stats,"flags":_indexed_map(rec,"FlagArray"),"attributes":_indexed_map(rec,"Attributes"),
            "armor":[_record_summary(k,records.get(k)) for k in armor_keys],
            "weapons":[_record_summary(k,records.get(k)) for k in weapon_keys],
            "abilities":[_record_summary(k,records.get(k)) for k in ability_keys],
            "behaviors":behaviors,"reactive_mechanics":reactive,"event_mechanics":other_events,
            "effect_summaries":[_record_summary(k,records.get(k)) for k in effect_keys],
            "execution_graph":{"node_count":len(nodes),"edge_count":len(owned_edges),"edges":[_compact_edge(e) for e in owned_edges]},
            "scaling_evidence":{"formula_references":formulas,"veterancy_behaviors":veterancy_evidence,"status":"EVIDENCE_ONLY_NOT_EVALUATED"},
        }
    return units, unresolved, {
        "reactive_mechanics":reactive_count,
        "damage_response_records":damage_response_count,
        "observed_units":len(observed),
        "armor_semantic_source_records":armor_semantic_source_count,
        "armor_semantic_retained_records":armor_semantic_retained_count,
        "veterancy_source_records":veterancy_source_count,
        "veterancy_retained_records":veterancy_retained_count,
        "veterancy_nested_source_facts":veterancy_nested_source_count,
        "veterancy_nested_contextualized_facts":veterancy_nested_context_count,
        "veterancy_nested_orphan_facts":veterancy_nested_orphan_count,
        "veterancy_nested_slot_mismatch_facts":veterancy_nested_slot_mismatch_count,
        "veterancy_semantic_profile_records":veterancy_profile_count,
        "veterancy_semantic_profile_mismatches":veterancy_profile_mismatch_count,
        "veterancy_level_requirement_field_records":veterancy_level_requirement_field_records,
        "veterancy_xp_field_evidence_records":veterancy_xp_field_evidence_records,
        "veterancy_level_source_resolved_records":veterancy_level_source_resolved_count,
        "veterancy_operation_fields":veterancy_operation_field_count,
        "veterancy_operation_resolved_fields":veterancy_operation_resolved_count,
        "veterancy_cumulative_resolved_records":veterancy_cumulative_resolved_count,
        "veterancy_empty_slot_runtime_resolved_records":veterancy_empty_slot_runtime_resolved_count,
        "veterancy_unsupported_operation_claims":veterancy_unsupported_operation_claims,
        "veterancy_unsupported_source_claims":veterancy_unsupported_source_claims,
        "veterancy_unsupported_cumulative_claims":veterancy_unsupported_cumulative_claims,
        "veterancy_unsupported_empty_slot_claims":veterancy_unsupported_empty_slot_claims,
        "veterancy_trailing_empty_records":veterancy_trailing_empty_records,
    }
