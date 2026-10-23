from __future__ import annotations
from collections import defaultdict, Counter, deque
from pathlib import Path
from typing import Any

from json_backend import load, save


def _type(key: str | None) -> str:
    return key.split('::',1)[0] if key and '::' in key else ''

def _oid(key: str | None) -> str | None:
    return key.split('::',1)[1] if key and '::' in key else None

def _to_number(v):
    try:
        x=float(v)
        return int(x) if x.is_integer() else x
    except Exception:
        return None

def _resolve_scalar(records: dict, raw: Any) -> dict:
    if raw is None:
        return {"raw": None, "value": None, "model": "MISSING"}
    s=str(raw)
    n=_to_number(s)
    if n is not None:
        return {"raw": s, "value": n, "model": "LITERAL"}
    if s.startswith('$'):
        rec=records.get(f"const::{s}")
        if rec:
            cv=(rec.get('effective_attrs') or {}).get('value')
            cn=_to_number(cv)
            return {"raw": s, "value": cn if cn is not None else cv,
                    "model": "CONSTANT" if cn is not None else "CONSTANT_EXPRESSION",
                    "constant_id": s, "constant_raw": cv}
        return {"raw": s, "value": None, "model": "UNRESOLVED_CONSTANT", "constant_id": s}
    return {"raw": s, "value": None, "model": "EXPRESSION_OR_ENUM"}

def _field_nodes(rec: dict, field: str) -> list[dict]:
    return [x for x in (rec.get('fields') or {}).get(field,[]) if (x.get('attrs') or {}).get('__hole__') != '1']

def _field_value(rec: dict, field: str, attr_names=("value","Value")):
    nodes=_field_nodes(rec,field)
    if not nodes: return None, None
    node=nodes[-1]
    attrs=node.get('attrs') or {}
    for a in attr_names:
        if a in attrs: return attrs[a], node.get('provenance') or {}
    return None, node.get('provenance') or {}

def _nested_facts(rec: dict, prefix: str | None=None) -> list[dict]:
    facts=rec.get('nested_facts') or []
    if prefix is None: return facts
    return [x for x in facts if (x.get('path') or []) and (x.get('path') or [None])[0] == prefix]

def _alignment(rec: dict) -> str | None:
    v,_=_field_value(rec,'Alignment')
    return v

def _duration(records, rec):
    v,_=_field_value(rec,'Duration')
    return _resolve_scalar(records,v) if v is not None else None

def _build_graph(typed: dict):
    outgoing=defaultdict(list)
    for e in typed.get('edges',[]) or []:
        outgoing[e.get('source_key')].append(e)
    return outgoing

def _records_by_id(records: dict) -> dict[str,list[str]]:
    out=defaultdict(list)
    for k,r in records.items():
        oid=r.get('object_id')
        if oid: out[str(oid)].append(k)
    return out

def _active_talent_entry_map(records: dict, herodata: dict) -> dict[str,set[str]]:
    """Map catalog Entry ids touched by talents that actually exist in current HeroData.

    This is intentionally structural.  It lets the combat layer distinguish an
    effect whose Chance=0 is enabled by a current talent from an orphaned/dead
    branch left in GameData after a talent was removed.
    """
    active=set()
    for h in (herodata.get('heroes') or {}).values():
        for t in h.get('talents',[]) or []:
            tid=t.get('talent_id') or t.get('id')
            if tid: active.add(str(tid))
    out=defaultdict(set)
    for key,rec in records.items():
        if not key.startswith('CTalent::'): continue
        tid=key.split('::',1)[1]
        if tid not in active: continue
        for fact in rec.get('nested_facts') or []:
            path=fact.get('path') or []
            if not path or path[0] != 'AbilityModificationArray' or fact.get('tag') != 'Entry':
                continue
            entry=(fact.get('attrs') or {}).get('value')
            if entry: out[str(entry)].add(tid)
    return out

def _parse_talent_modification_groups(rec: dict) -> list[dict]:
    """Parse CTalent/AbilityModificationArray/Modifications into ordered groups.

    Effective Catalog intentionally stores nested scalar facts without inventing
    synthetic XML records.  HOTS emits one ``Type`` scalar at the beginning of
    almost every Modifications entry, which gives us a stable structural group
    boundary while preserving the original provenance of every scalar.
    """
    facts=[x for x in (rec.get('nested_facts') or [])
           if (x.get('path') or [])[:2] == ['AbilityModificationArray','Modifications']]
    groups=[]; cur={}; prov={}
    for fact in facts:
        tag=str(fact.get('tag') or '')
        if tag=='Type' and cur:
            cur['_provenance']=prov; groups.append(cur); cur={}; prov={}
        attrs=fact.get('attrs') or {}
        if tag:
            cur[tag]=attrs.get('value',attrs.get('Value'))
            if not prov: prov=fact.get('provenance') or {}
    if cur:
        cur['_provenance']=prov; groups.append(cur)
    return groups

def _catalog_matches_key(catalog: str | None, key: str) -> bool:
    if not catalog: return True
    typ=_type(key); c=str(catalog).lower()
    if c in {'effect','ceffect'}: return typ.startswith('CEffect')
    if c in {'behavior','cbehavior'}: return typ.startswith('CBehavior')
    if c in {'abil','ability','cabil'}: return typ.startswith('CAbil')
    if c in {'validator','cvalidator'}: return typ.startswith('CValidator')
    if c in {'accumulator','caccumulator'}: return typ.startswith('CAccumulator')
    if c in {'unit','cunit'}: return typ.startswith('CUnit')
    if c in {'weapon','cweapon'}: return typ.startswith('CWeapon')
    return False

def _active_talent_field_mutations(records: dict, herodata: dict, by_id: dict[str,list[str]]) -> dict[str,list[dict]]:
    """Index current HeroData CTalent field mutations by exact catalog target key.

    This is a semantic overlay, not an Effective Catalog mutation: the base
    catalog stays variant-independent and each mutation retains its talent gate.
    """
    active=set()
    for h in (herodata.get('heroes') or {}).values():
        for t in h.get('talents',[]) or []:
            tid=t.get('talent_id') or t.get('id')
            if tid: active.add(str(tid))
    out=defaultdict(list)
    payload_tags=('Value','StringReplacement','CatalogReplacement')
    for key,rec in records.items():
        if not key.startswith('CTalent::'): continue
        tid=key.split('::',1)[1]
        if tid not in active: continue
        for g in _parse_talent_modification_groups(rec):
            entry=g.get('Entry'); field=g.get('Field'); catalog=g.get('Catalog')
            if not entry or not field: continue
            payload_tag=next((x for x in payload_tags if g.get(x) is not None),None)
            raw=g.get(payload_tag) if payload_tag else None
            scalar=_resolve_scalar(records,raw) if raw is not None else {"raw":None,"value":None,"model":"MISSING"}
            for target_key in by_id.get(str(entry),[]) or []:
                if not _catalog_matches_key(catalog,target_key): continue
                out[target_key].append({
                    "talent_id":tid,"talent_key":key,"target_id":str(entry),"target_key":target_key,
                    "catalog":catalog,"field":str(field),"operation":g.get('Type') or 'UNSPECIFIED',
                    "field_is_integer":g.get('FieldIsInteger'),"payload_tag":payload_tag,
                    "raw_value":raw,"value":scalar.get('value') if scalar.get('value') is not None else raw,
                    "formula":scalar,"provenance":g.get('_provenance') or {},
                })
    return out

def _chance_zero(rec: dict) -> bool:
    for n in _field_nodes(rec,'Chance'):
        v=(n.get('attrs') or {}).get('value')
        try:
            if float(v) == 0.0: return True
        except Exception:
            pass
    return False

def _nonzero_number(v) -> bool:
    try: return float(v) != 0.0
    except Exception: return False

def _talent_ids_in_conditions(item: dict) -> set[str]:
    return {str(tid) for c in (item.get('conditions') or []) for tid in (c.get('talent_ids') or []) if tid}

def _apply_zero_chance_gate(item: dict, records: dict, talent_mutations: dict[str,list[dict]]) -> bool:
    """Fail closed for any combat mechanic behind a disabled Chance=0 effect.

    A CTalent AbilityModificationArray that changes that exact effect's Chance to
    a non-zero value is materialized as an explicit talent condition.  Otherwise
    the branch is dormant.  Reachability alone is never evidence of activation.
    """
    zero=[]
    for key in item.get('typed_chain') or []:
        rec=records.get(key) or {}
        if str(rec.get('xml_type') or '').startswith('CEffect') and _chance_zero(rec):
            zero.append((key,str(rec.get('object_id') or _oid(key) or '')))
    if not zero: return True
    synthetic=[]; existing=_talent_ids_in_conditions(item)
    for key,oid in zero:
        muts=[m for m in (talent_mutations.get(key) or [])
              if str(m.get('field') or '')=='Chance' and _nonzero_number(m.get('value'))]
        tids=sorted({str(m.get('talent_id')) for m in muts if m.get('talent_id')})
        if not tids:
            item['dormant_zero_chance']={"blocked_object_key":key,"reason":"NO_CURRENT_EXPLICIT_TALENT_FIELD_ENABLER"}
            return False
        # Existing validator talent gates do not replace the Chance mutation: the
        # two facts are separate evidence.  Avoid only exact duplicate talent sets.
        if set(tids).issubset(existing):
            continue
        synthetic.append({
            "source_key":key,"relation":"ENABLED_BY_TALENT_MODIFICATION",
            "field":"Chance","target_key":key,"target_id":oid,"talent_ids":tids,
            "mutations":[{k:m.get(k) for k in ('talent_id','operation','raw_value','value','catalog','field')} for m in muts],
        })
    item.setdefault('conditions',[]).extend(synthetic)
    return True

def _start_keys(by_id: dict[str,list[str]], ability: dict) -> list[str]:
    aid=ability.get('ability_id') or ability.get('id')
    button=ability.get('button_id')
    starts=[]
    if aid and aid != ':PASSIVE:':
        starts=[k for k in by_id.get(str(aid),[]) if _type(k).startswith('CAbil')]
        if not starts:
            c=[k for k in by_id.get(str(aid),[]) if _type(k).startswith(('CBehavior','CEffect'))]
            if len(c)==1: starts=c
    if not starts and button:
        c=[k for k in by_id.get(str(button),[]) if _type(k).startswith(('CAbil','CBehavior','CEffect'))]
        if len(c)==1: starts=c
    return sorted(set(starts))

def _owned_closure(starts: list[str], outgoing: dict):
    q=deque(starts); seen=set(); parent={}; depth={s:0 for s in starts}
    while q:
        cur=q.popleft()
        if cur in seen: continue
        seen.add(cur)
        for e in outgoing.get(cur,[]):
            if not (e.get('propagation') or {}).get('ownership'): continue
            nxt=e.get('target_key')
            if not nxt: continue
            if nxt not in depth:
                depth[nxt]=depth.get(cur,0)+1
                parent[nxt]=(cur,e)
            if nxt not in seen: q.append(nxt)
    return seen,parent,depth

def _path_to(node: str, parent: dict):
    keys=[node]; edges=[]; cur=node
    while cur in parent:
        prev,e=parent[cur]
        edges.append(e); keys.append(prev); cur=prev
    keys.reverse(); edges.reverse()
    return keys,edges

def _conditions_for_path(path_keys:list[str], path_edges:list[dict], outgoing:dict, records:dict):
    out=[]; seen=set()
    # Only conditions that gate execution/state of the chosen path belong to the mechanic.
    # Leech/Crit/MultiplicativeModifier validators constrain a sub-formula, not
    # whether the damage effect itself executes.
    execution_fields={"ValidatorArray","Validator","CombineArray","DisableValidatorArray","RemoveValidatorArray","CaseArray","AreaArray"}

    # Switch/search branch validators are attached to the same CaseArray/AreaArray
    # element as the executed effect. A validator from another sibling branch must
    # never gate the current mechanic. Match the chosen execution edge back to the
    # effective field node and retain only validators from that exact node.
    branch_validators={}
    for pe in path_edges:
        field=pe.get('field')
        if field not in {"CaseArray","AreaArray"}: continue
        if not (pe.get('propagation') or {}).get('execution'): continue
        src=pe.get('source_key'); target_id=pe.get('target_id') or _oid(pe.get('target_key'))
        allowed=set()
        rec=records.get(src) or {}
        for node in _field_nodes(rec,field):
            attrs=node.get('attrs') or {}
            if target_id and target_id not in {str(v) for v in attrs.values() if v is not None}:
                continue
            for ak,av in attrs.items():
                if 'Validator' in str(ak) and av:
                    allowed.add(str(av))
        branch_validators[(src,field)]=allowed

    for k in path_keys:
        for e in outgoing.get(k,[]):
            if not (e.get('propagation') or {}).get('condition'): continue
            field=e.get('field')
            if field not in execution_fields:
                continue
            if field in {"CaseArray","AreaArray"}:
                branch_key=(k,field)
                if branch_key not in branch_validators:
                    continue
                allowed=branch_validators[branch_key]
                if not allowed or str(e.get('target_id') or '') not in allowed:
                    continue
            tgt=e.get('target_key')
            sig=(k,e.get('relation'),tgt,e.get('target_id'))
            if sig in seen: continue
            seen.add(sig)
            item={"source_key":k,"relation":e.get('relation'),"field":field,"target_key":tgt,"target_id":e.get('target_id')}
            # Follow validator composition to expose talent gates without making
            # validators owners. Bounded traversal is condition-only.
            if tgt:
                q=deque([tgt]); vseen=set(); talents=set()
                while q and len(vseen)<64:
                    cur=q.popleft()
                    if cur in vseen: continue
                    vseen.add(cur)
                    for e2 in outgoing.get(cur,[]):
                        if not (e2.get('propagation') or {}).get('condition'): continue
                        if e2.get('relation')=='CHECKS_TALENT':
                            tid=e2.get('target_id') or _oid(e2.get('target_key'))
                            if tid: talents.add(tid)
                        elif e2.get('target_key') and _type(e2.get('target_key')).startswith('CValidator'):
                            q.append(e2.get('target_key'))
                if talents: item['talent_ids']=sorted(talents)
            out.append(item)
    return out

def _talents_checked_by_validator(start_key: str | None, outgoing: dict) -> list[str]:
    if not start_key: return []
    q=deque([start_key]); seen=set(); talents=set()
    while q and len(seen)<64:
        cur=q.popleft()
        if cur in seen: continue
        seen.add(cur)
        for e in outgoing.get(cur,[]):
            if not (e.get('propagation') or {}).get('condition'): continue
            if e.get('relation')=='CHECKS_TALENT':
                tid=e.get('target_id') or _oid(e.get('target_key'))
                if tid: talents.add(str(tid))
            elif e.get('target_key') and _type(e.get('target_key')).startswith('CValidator'):
                q.append(e.get('target_key'))
    return sorted(talents)

def _formula_condition(source_key: str, field: str, validator_id: str | None, outgoing: dict) -> list[dict]:
    if not validator_id: return []
    edge=next((e for e in outgoing.get(source_key,[]) if e.get('field')==field and
               str(e.get('target_id') or '')==str(validator_id) and
               (e.get('propagation') or {}).get('condition')),None)
    target_key=edge.get('target_key') if edge else None
    item={"source_key":source_key,"relation":"FORMULA_CONDITION","field":field,
          "target_key":target_key,"target_id":str(validator_id)}
    tids=_talents_checked_by_validator(target_key,outgoing)
    if tids: item['talent_ids']=tids
    return [item]

def _damage_formula_modifiers(records: dict, key: str, rec: dict, outgoing: dict, base_value=None) -> list[dict]:
    """Materialize CEffectDamage conditional arithmetic without changing ownership.

    MultiplicativeModifierArray in Storm semantics is a relative delta: -0.4
    means base * (1 - 0.4), +0.5 means base * (1 + 0.5).  FlatModifierArray is
    additive.  Validators constrain the sub-formula only and therefore live on
    the formula modifier, not on the damage execution condition list.
    """
    out=[]
    for field,operation in (('MultiplicativeModifierArray','MULTIPLY_BASE_DELTA'),('FlatModifierArray','ADD_FLAT')):
        for node in _field_nodes(rec,field):
            attrs=node.get('attrs') or {}; raw=attrs.get('Modifier')
            if raw is None: continue
            scalar=_resolve_scalar(records,raw); numeric=_to_number(scalar.get('value'))
            validator=attrs.get('Validator')
            row={
                "operation":operation,"index":attrs.get('index'),"raw_value":scalar.get('raw'),
                "value":scalar.get('value') if scalar.get('value') is not None else scalar.get('raw'),
                "formula":scalar,"conditions":_formula_condition(key,field,validator,outgoing),
                "validator_id":validator,"crit":attrs.get('Crit'),"provenance":node.get('provenance') or {},
            }
            b=_to_number(base_value)
            if b is not None and numeric is not None:
                row['candidate_value_if_active']=(float(b)*(1.0+float(numeric)) if operation=='MULTIPLY_BASE_DELTA' else float(b)+float(numeric))
            out.append(row)
    return out

def _periodicity(path_edges:list[dict]):
    rel=[e.get('relation') for e in path_edges]
    if 'PERIODIC_EXECUTES' in rel:
        return {"kind":"PERIODIC","evidence":"PERIODIC_EXECUTES"}
    return None

def _mechanic_common(key, rec, parent, depth, outgoing, records):
    path_keys,path_edges=_path_to(key,parent)
    return {
        "object_id":rec.get('object_id'), "object_key":key,
        "graph_depth":depth.get(key,0),
        "chain":[_oid(x) for x in path_keys], "typed_chain":path_keys,
        "relations":[e.get('relation') for e in path_edges],
        "conditions":_conditions_for_path(path_keys,path_edges,outgoing,records),
        "periodicity":_periodicity(path_edges),
    }

def _damage_instance(records,key,rec,parent,depth,outgoing):
    raw,prov=_field_value(rec,'Amount')
    formula=None; value=None
    if raw is not None:
        scalar=_resolve_scalar(records,raw)
        formula={**scalar,"model":"FLAT_AMOUNT"}
        value=scalar.get('value') if scalar.get('value') is not None else scalar.get('raw')
        # Engine uses tiny no-response damage as an interrupt/signal. Keep it out
        # of combat damage while retaining the record in the typed graph.
        flags={str((n.get('attrs') or {}).get('index')) for n in _field_nodes(rec,'Flags') if str((n.get('attrs') or {}).get('value'))=='1'}
        nv=_to_number(scalar.get('value'))
        if nv is not None and abs(float(nv)) <= 0.001 and 'NoBehaviorResponse' in flags:
            return None
    else:
        # Percent-health CEffectDamage records legitimately have no Amount.
        vf,_p=_field_value(rec,'VitalFractionMax')
        if vf is not None:
            scalar=_resolve_scalar(records,vf); n=_to_number(scalar.get('value'))
            formula={**scalar,"model":"PERCENT_MAX_HEALTH","fraction":n,"value_percent":(float(n)*100 if n is not None else None)}
            value=None
            prov=_p
        else:
            # Kill/dummy/reveal damage records with no numeric damage formula are
            # control signals, not combat damage instances.
            return None
    x=_mechanic_common(key,rec,parent,depth,outgoing,records)
    x.update({
        "effect_id":rec.get('object_id'), "value":value,
        "raw_value":formula.get('raw') if formula else None, "formula":formula,
        "file":(prov or {}).get('file'), "execution_class":"TYPED_OWNERSHIP",
        "evidence":"TYPED_REFERENCE_OWNERSHIP",
    })
    fmods=_damage_formula_modifiers(records,key,rec,outgoing,value)
    if fmods:
        x['formula_modifiers']=fmods
        formula['modifiers']=fmods
    if formula and formula.get('model')=='PERCENT_MAX_HEALTH':
        x['value_percent_max_health']=formula.get('value_percent')
    return x

def _heal_instances(records,key,rec,parent,depth,outgoing):
    typ=rec.get('xml_type') or ''
    out=[]
    base=_mechanic_common(key,rec,parent,depth,outgoing,records)
    if typ.startswith('CEffectCreateHealer'):
        vital,_=_field_value(rec,'RechargeVital')
        if str(vital or 'Life').lower() not in {'life','health',''}:
            return []
        for field in ('RechargeVitalRate','RechargeVitalFraction'):
            raw,prov=_field_value(rec,field)
            if raw is None: continue
            sc=_resolve_scalar(records,raw); x=dict(base)
            x.update({"effect_id":rec.get('object_id'),"field":field,"value":sc.get('value') if sc.get('value') is not None else sc.get('raw'),
                      "raw_value":sc.get('raw'),"formula":sc,"file":(prov or {}).get('file'),"evidence":"TYPED_REFERENCE_OWNERSHIP"})
            out.append(x)
    elif typ.startswith('CEffectModifyUnit'):
        # VitalArray child stores the vital identity; nested facts store Change/ChangeFraction.
        life_indexes={str((n.get('attrs') or {}).get('index') or '').lower() for n in _field_nodes(rec,'VitalArray')}
        if not ({'life','health'} & life_indexes): return []
        for fact in _nested_facts(rec,'VitalArray'):
            tag=fact.get('tag')
            if tag not in {'Change','ChangeFraction','Value'}: continue
            attrs=fact.get('attrs') or {}; raw=attrs.get('value',attrs.get('Value'))
            if raw is None: continue
            sc=_resolve_scalar(records,raw); n=_to_number(sc.get('value'))
            # Negative life changes are damage/cost, not healing.
            if n is not None and n <= 0: continue
            x=dict(base); x.update({"effect_id":rec.get('object_id'),"field":tag,
                "value":sc.get('value') if sc.get('value') is not None else sc.get('raw'),"raw_value":sc.get('raw'),"formula":sc,
                "file":(fact.get('provenance') or {}).get('file'),"evidence":"TYPED_REFERENCE_OWNERSHIP_NESTED_SCALAR"})
            out.append(x)
    return out


def _behavior_heal_instances(records,key,rec,parent,depth,outgoing):
    """Extract high-confidence damage-leech healing from CBehavior Modification.

    GameData encodes lifesteal/spell-vamp in Modification/VitalDamageLeechArray.
    This is a derived heal mechanic, not a CEffectCreateHealer terminal.
    """
    if not str(rec.get('xml_type') or '').startswith('CBehavior'):
        return []
    facts=_nested_facts(rec,'Modification')
    has_life=False
    direct_fraction=None
    kind_fractions=[]
    prov={}
    for fact in facts:
        path=fact.get('path') or []
        tag=str(fact.get('tag') or '')
        attrs=fact.get('attrs') or {}
        if tag=='VitalDamageLeechArray':
            idx=str(attrs.get('index') or '').lower()
            if idx in {'life','health'}:
                has_life=True; prov=fact.get('provenance') or prov
                raw=attrs.get('value',attrs.get('Value'))
                if raw is not None: direct_fraction=(raw,fact.get('provenance') or {})
        elif tag=='KindArray' and 'VitalDamageLeechArray' in path:
            raw=attrs.get('value',attrs.get('Value'))
            if raw is not None:
                kind_fractions.append((str(attrs.get('index') or 'ALL').upper(),raw,fact.get('provenance') or {}))
    if not has_life: return []
    rows=[]; base=_mechanic_common(key,rec,parent,depth,outgoing,records)
    candidates=kind_fractions or ([('ALL',direct_fraction[0],direct_fraction[1])] if direct_fraction else [])
    for damage_kind,raw,pv in candidates:
        damage_kind={'BASIC':'BASIC_ATTACK','SPELL':'SPELL','ABILITY':'ABILITY'}.get(damage_kind,damage_kind)
        sc=_resolve_scalar(records,raw); n=_to_number(sc.get('value'))
        formula={**sc,"model":"DAMAGE_DEALT_FRACTION","source_event":"DAMAGE_DEALT","source_damage_type":damage_kind}
        if n is not None:
            formula['fraction']=n; formula['value_percent']=float(n)*100.0
        x=dict(base); x.update({
            "effect_id":rec.get('object_id'),"field":"VitalDamageLeechArray","value":formula.get('value_percent') if n is not None else sc.get('raw'),
            "raw_value":sc.get('raw'),"formula":formula,"source_event":"DAMAGE_DEALT","source_damage_type":damage_kind,
            "duration":_duration(records,rec),"file":(pv or prov).get('file'),"evidence":"TYPED_BEHAVIOR_VITAL_DAMAGE_LEECH",
        })
        rows.append(x)
    return rows

def _shield_instances(records,key,rec,parent,depth,outgoing):
    typ=rec.get('xml_type') or ''; out=[]; base=_mechanic_common(key,rec,parent,depth,outgoing,records)
    if typ.startswith('CEffectCreateHealer'):
        vital,_=_field_value(rec,'RechargeVital')
        buff,_=_field_value(rec,'RechargeShieldsTargetBehaviorBuff')
        if str(vital or '').lower()=='shields' or buff:
            vals=[]
            for field in ('RechargeVitalRate','RechargeVitalFraction'):
                raw,prov=_field_value(rec,field)
                if raw is not None: vals.append((field,_resolve_scalar(records,raw),prov))
            x=dict(base); x.update({"kind":"SHIELD_RECHARGE","behavior_id":buff,"values":[{"field":f,"formula":sc} for f,sc,_ in vals],
                                  "file":vals[0][2].get('file') if vals else None,"evidence_class":"TYPED_CREATE_HEALER_SHIELD"})
            out.append(x)
    if typ.startswith('CBehaviorBuff'):
        limits=[]
        for n in _field_nodes(rec,'DamageResponse'):
            attrs=n.get('attrs') or {}
            if 'ModifyLimit' in attrs:
                limits.append((attrs.get('ModifyLimit'), n.get('provenance') or {}, 'DIRECT_FIELD'))
        for fact in _nested_facts(rec,'DamageResponse'):
            if fact.get('tag')!='ModifyLimit': continue
            attrs=fact.get('attrs') or {}; raw=attrs.get('value',attrs.get('Value'))
            if raw is not None: limits.append((raw, fact.get('provenance') or {}, 'NESTED_SCALAR'))
        seen_limits=set()
        for raw,prov,source in limits:
            sig=(str(raw),prov.get('file'),prov.get('order'))
            if sig in seen_limits: continue
            seen_limits.add(sig)
            sc=_resolve_scalar(records,raw)
            x=dict(base); x.update({"kind":"DAMAGE_ABSORB_LIMIT","limit":sc.get('value') if sc.get('value') is not None else sc.get('raw'),
                                   "formula":sc,"duration":_duration(records,rec),"alignment":_alignment(rec),
                                   "file":prov.get('file'),"evidence_class":"TYPED_DAMAGE_RESPONSE_MODIFY_LIMIT_"+source})
            out.append(x)
        # StormShield and related behaviors often encode shield capacity as a
        # fraction of a vital max, not as a flat ModifyLimit.
        for fact in _nested_facts(rec,'DamageResponse'):
            if fact.get('tag')!='ModifyLimitVitalMaxFractionArray': continue
            attrs=fact.get('attrs') or {}; raw=attrs.get('value',attrs.get('Value'))
            if raw is None: continue
            vital=str(attrs.get('index') or 'Life')
            sc=_resolve_scalar(records,raw); n=_to_number(sc.get('value'))
            formula={**sc,"model":"PERCENT_MAX_VITAL","vital":vital}
            if n is not None:
                formula['fraction']=n; formula['value_percent']=float(n)*100.0
            x=dict(base); x.update({"kind":"DAMAGE_ABSORB_MAX_VITAL_FRACTION","vital":vital,
                                   "fraction":n,"value_percent":formula.get('value_percent'),"formula":formula,
                                   "duration":_duration(records,rec),"alignment":_alignment(rec),
                                   "file":(fact.get('provenance') or {}).get('file'),
                                   "evidence_class":"TYPED_DAMAGE_RESPONSE_MAX_VITAL_FRACTION"})
            out.append(x)
    return out

def _modifier_instances(records,key,rec,parent,depth,outgoing):
    if not str(rec.get('xml_type') or '').startswith('CBehavior'): return []
    base=_mechanic_common(key,rec,parent,depth,outgoing,records); out=[]
    alignment=_alignment(rec); duration=_duration(records,rec)
    known={
        'DamageDealtFraction':'DAMAGE_DEALT_FRACTION',
        'DamageTakenFraction':'DAMAGE_TAKEN_FRACTION',
        'HealDealtFraction':'HEAL_DEALT_FRACTION',
        'HealTakenFraction':'HEAL_TAKEN_FRACTION',
        'AttackSpeedMultiplier':'ATTACK_SPEED_MULTIPLIER',
    }
    for fact in _nested_facts(rec,'Modification'):
        tag=str(fact.get('tag') or '')
        if tag not in known and not (('Damage' in tag or 'Heal' in tag) and ('Fraction' in tag or 'Multiplier' in tag)):
            continue
        attrs=fact.get('attrs') or {}; raw=attrs.get('value',attrs.get('Value'))
        if raw is None: continue
        sc=_resolve_scalar(records,raw); x=dict(base)
        x.update({"modifier_kind":known.get(tag,tag.upper()),"field":tag,"index":attrs.get('index'),
                  "value":sc.get('value') if sc.get('value') is not None else sc.get('raw'),"raw_value":sc.get('raw'),
                  "formula":sc,"alignment":alignment,"duration":duration,
                  "file":(fact.get('provenance') or {}).get('file'),"evidence":"TYPED_NESTED_MODIFICATION_SCALAR"})
        out.append(x)
    return out

def _indirect(records, owned:set[str], outgoing:dict):
    out=[]; seen=set()
    for src in owned:
        for e in outgoing.get(src,[]):
            prop=e.get('propagation') or {}
            if not prop.get('execution') or prop.get('ownership'): continue
            boundary=e.get('owner_boundary')
            if not boundary: continue
            tgt=e.get('target_key')
            sig=(src,tgt,e.get('relation'),boundary)
            if sig in seen: continue
            seen.add(sig)
            out.append({"source_key":src,"target_key":tgt,"target_id":e.get('target_id'),"relation":e.get('relation'),
                        "owner_boundary":boundary,"evidence":"EXECUTION_WITHOUT_OWNERSHIP"})
    return out

def _talent_gate_ids(item:dict) -> list[str]:
    ids=set()
    for c in item.get('conditions') or []:
        ids.update(str(x) for x in (c.get('talent_ids') or []) if x)
    return sorted(ids)

def extract_ability(records:dict, typed:dict, ability:dict, *, outgoing=None, by_id=None, active_talent_entries=None, talent_mutations=None):
    outgoing=outgoing if outgoing is not None else _build_graph(typed)
    by_id=by_id if by_id is not None else _records_by_id(records)
    starts=_start_keys(by_id,ability)
    owned,parent,depth=_owned_closure(starts,outgoing)
    damage=[]; heal=[]; shield=[]; modifiers=[]
    for key in sorted(owned):
        rec=records.get(key)
        if not rec: continue
        typ=str(rec.get('xml_type') or '')
        if typ=='CEffectDamage':
            inst=_damage_instance(records,key,rec,parent,depth,outgoing)
            if inst: damage.append(inst)
        if typ.startswith(('CEffectCreateHealer','CEffectModifyUnit')): heal.extend(_heal_instances(records,key,rec,parent,depth,outgoing))
        if typ.startswith('CBehavior'): heal.extend(_behavior_heal_instances(records,key,rec,parent,depth,outgoing))
        if typ.startswith(('CEffectCreateHealer','CBehaviorBuff')): shield.extend(_shield_instances(records,key,rec,parent,depth,outgoing))
        if typ.startswith('CBehavior'): modifiers.extend(_modifier_instances(records,key,rec,parent,depth,outgoing))
    active_talent_entries=active_talent_entries or {}
    talent_mutations=talent_mutations or {}
    dormant_combat=[]
    def _gate(kind, rows):
        kept=[]
        for x in rows:
            if _apply_zero_chance_gate(x,records,talent_mutations): kept.append(x)
            else: dormant_combat.append({"kind":kind,"object_id":x.get('object_id') or x.get('effect_id') or x.get('behavior_id'),**(x.get('dormant_zero_chance') or {})})
        return kept
    damage=_gate('DAMAGE',damage)
    _heal=[]
    for x in heal:
        if _apply_zero_chance_gate(x,records,talent_mutations): _heal.append(x)
        else: dormant_combat.append({"kind":"HEAL","object_id":x.get('object_id') or x.get('effect_id'),**(x.get('dormant_zero_chance') or {})})
    heal=_heal
    _shield=[]
    for x in shield:
        if _apply_zero_chance_gate(x,records,talent_mutations): _shield.append(x)
        else: dormant_combat.append({"kind":"SHIELD","object_id":x.get('object_id') or x.get('behavior_id'),**(x.get('dormant_zero_chance') or {})})
    shield=_shield
    modifiers=_gate('MODIFIER',modifiers)
    field_mutations=[]
    for target_key in sorted(owned):
        for m in talent_mutations.get(target_key) or []:
            field_mutations.append(dict(m))
    indirect=_indirect(records,owned,outgoing)
    # Talent-gated mechanics are preserved, but never materialized as unconditional
    # base ability mechanics. This is essential for cases such as Brightwing
    # Unstable Anomaly on Polymorph.
    conditional_damage=[x for x in damage if _talent_gate_ids(x)]
    damage=[x for x in damage if not _talent_gate_ids(x)]
    conditional_heal=[x for x in heal if _talent_gate_ids(x)]
    heal=[x for x in heal if not _talent_gate_ids(x)]
    conditional_shield=[x for x in shield if _talent_gate_ids(x)]
    shield=[x for x in shield if not _talent_gate_ids(x)]
    conditional_modifiers=[x for x in modifiers if _talent_gate_ids(x)]
    modifiers=[x for x in modifiers if not _talent_gate_ids(x)]
    terminals=[]
    for x in damage+heal+shield+conditional_damage+conditional_heal+conditional_shield:
        terminals.append({"object_id":x.get('object_id') or x.get('effect_id'),"object_key":x.get('object_key'),
                          "chain":x.get('chain'),"relations":x.get('relations'),"execution_class":"TYPED_OWNERSHIP",
                          "talent_gate_ids":_talent_gate_ids(x)})
    classes=[]
    if damage or heal or shield: classes.append('DIRECT_TYPED_COMBAT')
    if conditional_damage or conditional_heal or conditional_shield: classes.append('TALENT_GATED_TYPED_COMBAT')
    if modifiers: classes.append('BEHAVIOR_MODIFIER')
    if conditional_modifiers: classes.append('TALENT_GATED_MODIFIER')
    if indirect: classes.append('INDIRECT_OWNER_BOUNDARY')
    if not classes: classes=['NO_TYPED_COMBAT_TERMINAL']
    return {
        "record_key":ability.get('record_key'),"damage":damage,"heal":heal,"shield_candidates":shield,
        "conditional_damage":conditional_damage,"conditional_heal":conditional_heal,"conditional_shield_candidates":conditional_shield,
        "modifiers":modifiers,"conditional_modifiers":conditional_modifiers,"resource_candidates":[],"indirect_effects":indirect,
        "talent_field_mutations":field_mutations,
        "dormant_survivability":[x for x in dormant_combat if x.get('kind') in {'HEAL','SHIELD'}],
        "dormant_combat":dormant_combat,
        "graph_nodes":len(owned),"xml_start_count":len(starts),
        "mana_cost":ability.get('mana_cost'),"charges":ability.get('charges'),"charge_data":ability.get('charge_data'),
        "cooldown_reference":ability.get('cooldown'),
        "execution_evidence":{"builder":"typed_reference_combat_v1_15","starts":starts,"classes":classes,"terminals":terminals,
                              "edge_kinds":sorted({r for x in terminals for r in (x.get('relations') or [])})},
    }

def run_combat(replay_catalog_path:Path, typed_graph_path:Path, herodata_path:Path, output_path:Path, report_path:Path):
    cat=load(replay_catalog_path); typed=load(typed_graph_path); hd=load(herodata_path); records=cat.get('records',{})
    outgoing=_build_graph(typed); by_id=_records_by_id(records); active_talent_entries=_active_talent_entry_map(records,hd)
    talent_mutations=_active_talent_field_mutations(records,hd,by_id)
    heroes={}; total=wd=wh=ws=wm=ind=cd=ch=cs=cm=ad=ah=ash=am=dormant_count=dormant_combat_count=field_mutation_count=formula_modifier_count=0; review=[]
    for hero,h in hd.get('heroes',{}).items():
        arr=[]
        for a in h.get('abilities',[]) or []:
            if not a.get('id'): continue
            total+=1; x=extract_ability(records,typed,a,outgoing=outgoing,by_id=by_id,active_talent_entries=active_talent_entries,talent_mutations=talent_mutations); arr.append(x)
            dormant_count+=len(x.get('dormant_survivability') or [])
            dormant_combat_count+=len(x.get('dormant_combat') or [])
            field_mutation_count+=len(x.get('talent_field_mutations') or [])
            formula_modifier_count+=sum(len(d.get('formula_modifiers') or []) for d in (x.get('damage') or [])+(x.get('conditional_damage') or []))
            wd+=bool(x['damage']); wh+=bool(x['heal']); ws+=bool(x['shield_candidates']); wm+=bool(x['modifiers']); ind+=bool(x['indirect_effects'])
            cd+=bool(x['conditional_damage']); ch+=bool(x['conditional_heal']); cs+=bool(x['conditional_shield_candidates']); cm+=bool(x['conditional_modifiers'])
            ad+=bool(x['damage'] or x['conditional_damage']); ah+=bool(x['heal'] or x['conditional_heal']); ash+=bool(x['shield_candidates'] or x['conditional_shield_candidates']); am+=bool(x['modifiers'] or x['conditional_modifiers'])
            if a.get('category') in {'BASIC','HEROIC'} and not (x['damage'] or x['heal'] or x['shield_candidates'] or x['modifiers']):
                review.append({"hero":hero,"record_key":a.get('record_key'),"ability_id":a.get('id'),"name":a.get('name'),"reason":"NO_TYPED_COMBAT_MECHANIC"})
        heroes[hero]=arr
    data={"metadata":{"builder":"combat_typed_engine_v1_15","source":"replay_effective_catalog+typed_reference_graph","fail_closed":True},"heroes":heroes}
    report={"summary":{"abilities_total":total,"with_damage":wd,"with_heal":wh,"with_shield_candidates":ws,"with_modifiers":wm,"with_indirect_effects":ind,
                       "with_any_damage":ad,"with_any_heal":ah,"with_any_shield":ash,"with_any_modifiers":am,
                       "with_talent_gated_damage":cd,"with_talent_gated_heal":ch,"with_talent_gated_shield":cs,"with_talent_gated_modifiers":cm,
                       "typed_reference_unresolved":len(typed.get('unresolved',[]) or []),"dormant_survivability_suppressed":dormant_count,
                       "dormant_combat_suppressed":dormant_combat_count,"talent_field_mutations_materialized":field_mutation_count,
                       "damage_formula_modifiers_materialized":formula_modifier_count},"review":review}
    save(output_path,data); save(report_path,report); return report
