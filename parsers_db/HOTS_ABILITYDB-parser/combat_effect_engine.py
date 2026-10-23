from __future__ import annotations
import json
from json_backend import load as _orjson_load, save as _orjson_save
from pathlib import Path
from xml_semantics import ability_starts,values_for_tag,descendants,const_value
from reverse_provenance_resolver import resolve_reverse_provenance

def load(p): return _orjson_load(p)
def save(p,d): return _orjson_save(p,d)

def dedupe(items):
    seen=set();out=[]
    for x in items:
        k=json.dumps(x,ensure_ascii=False,sort_keys=True)
        if k not in seen:seen.add(k);out.append(x)
    return out



def is_base_shield_candidate(object_id, chain):
    """Reject talent-only/conditional shield branches from base ability semantics."""
    parts=[str(object_id or "")] + [str(x or "") for x in (chain or [])]
    low=" ".join(parts).lower()
    # Talent-scoped XML branches must be represented as talent overlays, not
    # as unconditional base shields on the parent ability.
    if "talent" in low:
        return False
    return True

def _execution_nodes(execution):
    """Nodes present in proven reverse provenance chains only."""
    out={}
    for t in execution.get("terminals") or []:
        chain=t.get("chain") or []
        for dep,oid in enumerate(chain):
            cur=out.get(oid)
            if cur is None or dep < cur.get("depth",999):
                out[oid]={"id":oid,"depth":dep,"chain":chain[:dep+1]}
    return out

def extract(objects,a):
    starts=ability_starts(objects,a)
    execution=resolve_reverse_provenance(objects,a)
    proven=_execution_nodes(execution)
    damage=[];heal=[];shield=[];resource=[];indirect=[]

    # Exact direct ability resource/cost extraction from the actual ability starts.
    for d in starts:
        for node in descendants(d):
            path=node.get("path",[]); attrs=node.get("attrs",{})
            if "Cost" in path and node.get("tag")=="Vital":
                idx=attrs.get("index"); x=const_value(objects,attrs.get("value"))
                if x is not None: resource.append({"kind":"vital_cost","index":idx,"value":x,"file":d.get("file")})
            if "Cost" in path and node.get("tag")=="Cooldown":
                x=const_value(objects,attrs.get("TimeUse"))
                if x is not None: resource.append({"kind":"cooldown_reference","value":x,"file":d.get("file")})
            if "Charge" in path and node.get("tag") in {"CountMax","CountStart","CountUse","TimeUse"}:
                x=const_value(objects,attrs.get("value"))
                if x is not None: resource.append({"kind":"charge","field":node.get("tag"),"value":x,"file":d.get("file")})

    # Damage/heal/modify-unit mechanics come ONLY from typed execution terminals.
    terminal_by_id={}
    for t in execution.get("terminals") or []:
        terminal_by_id.setdefault(t.get("object_id"),[]).append(t)

    for oid,terms in terminal_by_id.items():
        info=proven.get(oid) or {}; dep=info.get("depth",0)
        for d in objects.get(oid,[]):
            typ=str(d.get("xml_type") or "")
            for t in terms:
                chain=t.get("chain") or info.get("chain") or [oid]
                eclass=t.get("execution_class")
                if typ=="CEffectDamage":
                    for x in values_for_tag(objects,d,{"Amount"}):
                        damage.append({"effect_id":oid,"value":x["value"],"graph_depth":dep,"chain":chain,"file":d.get("file"),"execution_class":eclass,"evidence":"REVERSE_PROVENANCE_TERMINAL"})
                elif typ=="CEffectCreateHealer":
                    for x in values_for_tag(objects,d,{"RechargeVitalRate","RechargeVitalFraction"}):
                        heal.append({"effect_id":oid,"field":x["tag"],"value":x["value"],"graph_depth":dep,"chain":chain,"file":d.get("file"),"execution_class":eclass,"evidence":"REVERSE_PROVENANCE_TERMINAL"})
                elif typ=="CEffectModifyUnit":
                    for node in descendants(d):
                        if node.get("tag")!="VitalArray": continue
                        attrs=node.get("attrs",{})
                        if str(attrs.get("index") or "").lower() not in {"life","health"}: continue
                        for key in ("Change","ChangeFraction","value","Value"):
                            x=const_value(objects,attrs.get(key))
                            if x is not None:
                                heal.append({"effect_id":oid,"field":key,"value":x,"graph_depth":dep,"chain":chain,"file":d.get("file"),"execution_class":eclass,"evidence":"REVERSE_PROVENANCE_TERMINAL"})

    # Shield discovery remains conservative, but may inspect only explicitly reachable
    # Source Graph nodes. It no longer receives nodes from legacy traverse().
    for oid,info in proven.items():
        dep=info.get("depth",0); chain=info.get("chain") or [oid]
        if dep>6: continue
        for d in objects.get(oid,[]):
            typ=str(d.get("xml_type") or "")
            if typ in {"CEffectApplyBehavior","CBehaviorBuff"} and "shield" in oid.lower():
                if not is_base_shield_candidate(oid,chain): continue
                vals=[]
                for node in descendants(d):
                    for k,v in node.get("attrs",{}).items():
                        x=const_value(objects,v)
                        if x is not None: vals.append({"field":f"{'/'.join(node.get('path',[]))}@{k}","value":x})
                shield.append({"object_id":oid,"xml_type":typ,"graph_depth":dep,"chain":chain,"values":vals[:100],"file":d.get("file"),"evidence_class":"REVERSE_PROVENANCE_REACHABLE_SHIELD"})

    return {
        "record_key":a.get("record_key"),
        "damage":dedupe(damage),"heal":dedupe(heal),"shield_candidates":dedupe(shield),
        "resource_candidates":dedupe(resource),"indirect_effects":dedupe(indirect),
        "graph_nodes":len(proven),"xml_start_count":len(starts),
        "mana_cost":a.get("mana_cost"),"charges":a.get("charges"),"charge_data":a.get("charge_data"),
        "cooldown_reference":a.get("cooldown"),"execution_evidence":execution
    }

def run_combat(catalog_path,herodata_path,output_path,report_path):
    cat=load(catalog_path);hd=load(herodata_path);objects=cat.get("objects",{})
    heroes={};total=wd=wh=ws=ind=0;review=[]
    for hero,h in hd.get("heroes",{}).items():
        arr=[]
        for a in h.get("abilities",[]):
            if not a.get("id"):continue
            total+=1;x=extract(objects,a);arr.append(x)
            if x["damage"]:wd+=1
            if x["heal"]:wh+=1
            if x["shield_candidates"]:ws+=1
            if x["indirect_effects"]:ind+=1
            if a.get("category") in {"BASIC","HEROIC"} and not x["damage"] and not x["heal"] and not x["shield_candidates"]:
                review.append({"hero":hero,"record_key":a.get("record_key"),"ability_id":a.get("id"),"name":a.get("name"),"reason":"NO_DIRECT_EXECUTABLE_COMBAT_EFFECT"})
        heroes[hero]=arr
    data={"metadata":{"builder":"combat_effect_engine_v2_4_reverse_provenance","note":"cooldown reference only"},"heroes":heroes}
    report={"summary":{"abilities_total":total,"with_damage":wd,"with_heal":wh,"with_shield_candidates":ws,"with_indirect_effects":ind},"review":review}
    save(output_path,data);save(report_path,report);return report
