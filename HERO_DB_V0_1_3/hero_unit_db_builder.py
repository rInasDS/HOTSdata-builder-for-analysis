from __future__ import annotations
import argparse, hashlib, json, math, tempfile
from collections import defaultdict, deque, Counter
from pathlib import Path

from catalog_stream import iter_objects
from effective_catalog import EffectiveCatalogCompiler
from json_backend import load, save
from unit_effective_catalog import build_unit_effective_catalog_data, _build_raw_reference_index, _edges_for_key
from typed_reference_resolver import build_typed_reference_graph_data, _materialize_typed_edge, _is_pure_missile_ammo, classify_reference

VERSION="0.2"
CATALOG_REQUIRED="storm_catalog_index_v2_4_ancestor_context"
INCLUDE_PREFIX=("CHero","CTalent","CUnit","CWeapon","CTurret","CAbil","CEffect","CBehavior","CValidator","CAccumulator","CArmor","CUpgrade","CMover","CFootprint","CRequirement")
EXCLUDED_PATH_TOKENS=("heroesmapmods","heroesbrawlmods","singleplayermaps","tutorial","startingexperience")


def _keep_type(t:str)->bool:
    return t=="const" or any(t.startswith(p) for p in INCLUDE_PREFIX)

def build_global_compact(catalog_index:Path)->dict:
    out={}; defs_count=0; excluded=Counter(); source_meta={}
    # metadata is small enough to read from header with a lightweight normal read only
    try:
        with catalog_index.open('r',encoding='utf-8') as f:
            head=f.read(2_000_000)
        m='"metadata"'
        if m in head:
            dec=json.JSONDecoder(); i=head.index(m)+len(m); i=head.index(':',i)+1
            source_meta,_=dec.raw_decode(head[i:].lstrip())
    except Exception:
        source_meta={}
    for oid,defs in iter_objects(catalog_index):
        kept=[]
        for d in defs:
            t=str(d.get('xml_type') or '')
            if not _keep_type(t): continue
            fp=str(d.get('file') or '').lower()
            bad=next((x for x in EXCLUDED_PATH_TOKENS if x in fp),None)
            if bad:
                excluded[bad]+=1; continue
            kept.append(d)
        if kept:
            out[oid]=kept; defs_count+=len(kept)
    return {
        "metadata":{
            "builder":"pyhots_hero_global_compact_v0_1",
            "source_builder":source_meta.get('builder'),
            "source_ancestor_context_retained":source_meta.get('ancestor_context_retained'),
            "global_provenance_filter":True,
            "definitions":defs_count,
            "excluded_definition_counts":dict(excluded),
        },
        "objects":out,
    }

def _field_nodes(rec,field): return list((rec.get('fields') or {}).get(field,[]) or []) if rec else []
def _node_value(n):
    a=n.get('attrs') or {}
    for k in ('value','Value','Link','Unit','Ability'):
        if a.get(k) is not None:return str(a[k])
    return None

def _field_values(rec,field):
    out=[]
    for n in _field_nodes(rec,field):
        v=_node_value(n)
        if v is not None: out.append(v)
    return out

def _scalar(rec,field):
    vals=_field_values(rec,field)
    return vals[-1] if vals else None

def _numeric(v):
    if v is None:return None
    try:return float(v)
    except Exception:return None

SOURCE_LOCATION_SELECTORS={'Caster','CasterUnit','CasterPoint','Source','SourceUnit','SourcePoint','SourceUnitOrPoint'}
TARGET_LOCATION_SELECTORS={'Target','TargetUnit','TargetPoint','TargetUnitOrPoint','MousePoint'}

def _vision_center_for_record(rec):
    """Normalize only directly evidenced location selectors; never infer from names."""
    if not rec:return ('UNRESOLVED',None)
    loc=_scalar(rec,'WhichLocation')
    unit=_scalar(rec,'WhichUnit')
    typ=str(rec.get('xml_type') or '')
    for selector,field in ((loc,'WhichLocation'),(unit,'WhichUnit')):
        if selector in SOURCE_LOCATION_SELECTORS:
            return ('SOURCE_POINT',{'field':field,'value':selector})
        if selector in TARGET_LOCATION_SELECTORS:
            return ('TARGET_POINT',{'field':field,'value':selector})
    if typ.startswith('CAbilEffectTarget'):
        return ('TARGET_POINT',{'field':'xml_type','value':typ})
    if typ.startswith('CAbilEffectInstant'):
        return ('SOURCE_POINT',{'field':'xml_type','value':typ})
    return ('UNRESOLVED',None)

def _scope_to_vision_center(scope):
    if scope in {'HERO_SELF','HERO_FORM'}:return 'SELF'
    if scope in {'OWNED_ENTITY','OWNED_ENTITY_RUNTIME_FORM'}:return 'OWNED_ENTITY'
    if scope=='TARGET_POINT':return 'TARGET_POINT'
    return None

def _effect_target_scope(center, kind):
    if center=='TARGET_POINT':return 'TARGET_POINT'
    if kind=='UNIT_REVEAL':return 'TARGET_POINT' if center=='TARGET_POINT' else 'AREA'
    if kind in {'AREA_REVEAL_RADIUS','AREA_REVEAL_SHAPE','SEARCH_REVEAL','REVEAL_BEHAVIOR','DEATH_REVEAL'}:return 'AREA'
    return 'AREA' if center in {'SOURCE_POINT','AREA_EFFECT'} else 'EFFECT_RUNTIME_SCOPE_UNRESOLVED'

def _unit_key_for_id(records,uid):
    exact=f"CUnit::{uid}"
    if exact in records:return exact
    hits=[k for k,r in records.items() if k.startswith('CUnit') and r.get('object_id')==uid]
    return hits[0] if len(hits)==1 else None

def _raw_unit_candidates(raw,uid):
    hits=[]
    for d in (raw.get('objects') or {}).get(uid,[]):
        if str(d.get('xml_type') or '').startswith('CUnit'):
            hits.append(f"{d.get('xml_type')}::{uid}")
    return sorted(set(hits))

def _primary_unit_id(hero_id, hero_rec, raw):
    explicit=_field_values(hero_rec,'Unit')
    if len(set(explicit))==1:return explicit[-1],"CHERO_EXPLICIT_UNIT"
    candidate="Hero"+hero_id
    cands=_raw_unit_candidates(raw,candidate)
    if len(cands)==1:return candidate,"EXACT_HERO_ID_CUNIT"
    return None,"UNRESOLVED"

def _playable_heroes(chero_records, raw):
    """Playable roster requires local CHero ProductId+AttributeId declarations.
    Effective inheritance is intentionally insufficient: form CHero children inherit
    these fields from the playable parent (e.g. GreymaneWorgen) but are not roster entries.
    """
    out=[]
    for k,r in chero_records.items():
        oid=k.split('::',1)[1]
        local=set()
        for d in (raw.get('objects') or {}).get(oid,[]):
            if d.get('xml_type')!='CHero': continue
            for c in d.get('children',[]) or []:
                if c.get('tag') in {'ProductId','AttributeId'} and (c.get('attrs') or {}).get('value') is not None:
                    local.add(c.get('tag'))
        if {'ProductId','AttributeId'} <= local:
            out.append(oid)
    return sorted(out)

def _variant_owner(rec, playable:set[str]):
    for x in reversed(rec.get('inheritance_chain') or []):
        if x.get('type')=='CHero' and x.get('id') in playable:
            return x.get('id')
    return None

def _collect_forms(hero_id, hero_rec, chero_records, raw, playable):
    forms=[]
    p,status=_primary_unit_id(hero_id,hero_rec,raw)
    if p: forms.append({"unit_id":p,"role":"PRIMARY","proof":status,"hero_catalog_id":hero_id})
    for n in _field_nodes(hero_rec,'AlternateUnitArray'):
        a=n.get('attrs') or {}; uid=a.get('value') or a.get('Unit') or a.get('Link')
        if uid:
            forms.append({"unit_id":str(uid),"role":"CHERO_ALTERNATE","proof":"CHERO_ALTERNATE_UNIT_ARRAY","hero_catalog_id":hero_id,"provenance":n.get('provenance')})
    for k,r in chero_records.items():
        cid=k.split('::',1)[1]
        if cid==hero_id or cid in playable: continue
        if _variant_owner(r,playable)!=hero_id: continue
        uid,why=_primary_unit_id(cid,r,raw)
        if uid:
            forms.append({"unit_id":uid,"role":"CHERO_VARIANT","proof":why,"hero_catalog_id":cid,"variant_parent":hero_id})
    ded={}
    order={"PRIMARY":0,"CHERO_ALTERNATE":1,"CHERO_VARIANT":2}
    for f in forms:
        u=f['unit_id']
        if u not in ded or order[f['role']]<order[ded[u]['role']]: ded[u]=f
    return sorted(ded.values(),key=lambda x:(order[x['role']],x['unit_id']))

def _reconstruct_groups(definition, root_tag, group_tag='Modifications'):
    """Reconstruct sibling groups from preorder depth/ordinal retained by catalog v2.4.
    This is evidence extraction, not effective mutation application.
    """
    groups=[]; root_ord=None; current=None
    for n in definition.get('descendants',[]) or []:
        path=n.get('path') or []
        depth=int(n.get('depth') or len(path) or 0)
        tag=n.get('tag')
        if depth==1 and tag==root_tag:
            root_ord=n.get('same_tag_ordinal',0); current=None
        elif depth==2 and path and path[0]==root_tag and tag==group_tag:
            ga=dict(n.get('attrs') or {})
            direct_values={k:str(v) for k,v in ga.items() if k not in {'index','removed'} and v is not None} if root_tag=='LevelScalingArray' else {}
            current={"root_ordinal":root_ord,"group_ordinal":n.get('same_tag_ordinal',0),"root_attrs":{},
                     "group_attrs":ga,"values":direct_values,"nodes":[]}
            groups.append(current)
        elif current is not None and depth>=3 and path and path[0]==root_tag and group_tag in path:
            a=n.get('attrs') or {}; value=a.get('value')
            current['nodes'].append({"tag":tag,"attrs":a,"path":path})
            if value is not None: current['values'][tag]=str(value)
    # root attrs are available as direct descendants nodes at depth1
    roots={}
    for n in definition.get('descendants',[]) or []:
        if int(n.get('depth') or 0)==1 and n.get('tag')==root_tag:
            roots[n.get('same_tag_ordinal',0)]=dict(n.get('attrs') or {})
    for g in groups:g['root_attrs']=roots.get(g['root_ordinal'],{})
    return groups

def _raw_group_evidence(raw, oid, xml_prefix, root_tag):
    out=[]; defs=[d for d in (raw.get('objects') or {}).get(oid,[]) if str(d.get('xml_type') or '').startswith(xml_prefix)]
    for d in sorted(defs,key=lambda z:z.get('order',0)):
        for g in _reconstruct_groups(d,root_tag):
            out.append({
                "values":g['values'],"root_attrs":g['root_attrs'],
                "root_ordinal":g['root_ordinal'],"group_ordinal":g['group_ordinal'],
                "provenance":{"file":d.get('file'),"mod":d.get('mod'),"order":d.get('order'),"definition_type":d.get('xml_type')},
            })
    return out



def _build_type_index(raw):
    return {oid: sorted({str(d.get("xml_type") or "") for d in defs if d.get("xml_type")}) for oid,defs in (raw.get("objects") or {}).items()}

def _resolve_any(compiler, type_index, oid, prefixes):
    types=[t for t in type_index.get(str(oid),[]) if any(t.startswith(p) for p in prefixes)]
    if len(types)!=1:
        return None,None
    key=f"{types[0]}::{oid}"
    return key,compiler.resolve((types[0],str(oid)))

def _vision_primitives_for_record(key, rec):
    """Extract typed vision primitives from one effective catalog record.

    This reports fields only. It does not assert who receives a behavior/effect or
    where an effect is centered; those are separate execution/scope proofs.
    """
    if not rec:return []
    out=[]
    typ=str(rec.get('xml_type') or '')
    oid=rec.get('object_id')
    # Behavior/unit stat modifiers.
    for nf in rec.get('nested_facts',[]) or []:
        path=nf.get('path') or []; tag=nf.get('tag'); a=nf.get('attrs') or {}
        if tag in {'SightBonus','SightMaximum'} and 'Modification' in path:
            out.append({
                'kind':'SIGHT_BONUS' if tag=='SightBonus' else 'SIGHT_MAXIMUM',
                'value':a.get('value'),'source_key':key,'source_id':oid,'source_type':typ,
                'path':path,'provenance':nf.get('provenance'),
            })
    # Persistent/area reveal primitives.
    rr=_scalar(rec,'RevealRadius')
    if rr is not None:
        out.append({'kind':'AREA_REVEAL_RADIUS','radius':rr,'source_key':key,'source_id':oid,'source_type':typ,
                    'reveal_flags':[n.get('attrs') or {} for n in _field_nodes(rec,'RevealFlags')],
                    'provenance':(_field_nodes(rec,'RevealRadius')[-1] or {}).get('provenance') if _field_nodes(rec,'RevealRadius') else None})
    rw=_scalar(rec,'RevealRectangleWidth'); rh=_scalar(rec,'RevealRectangleHeight'); ra=_scalar(rec,'RevealArc')
    if rw is not None or rh is not None or ra is not None:
        out.append({'kind':'AREA_REVEAL_SHAPE','rectangle_width':rw,'rectangle_height':rh,'arc':ra,
                    'source_key':key,'source_id':oid,'source_type':typ})
    # EnumArea RevealerParams are direct reveal semantics, distinct from ordinary search radius.
    rp=_field_nodes(rec,'RevealerParams')
    if rp:
        radii=[]
        for nf in rec.get('nested_facts',[]) or []:
            if nf.get('tag')=='Radius' and (nf.get('path') or [None])[0]=='AreaArray':
                radii.append((nf.get('attrs') or {}).get('value'))
        areas=[]
        for n in _field_nodes(rec,'AreaArray'):
            a=n.get('attrs') or {}; areas.append({'effect':a.get('Effect'),'radius_bonus':a.get('RadiusBonus'),'provenance':n.get('provenance')})
        out.append({'kind':'SEARCH_REVEAL','radii':[x for x in radii if x is not None],
                    'revealer_params':[n.get('attrs') or {} for n in rp],
                    'reveal_flags':[nf.get('attrs') or {} for nf in rec.get('nested_facts',[]) or [] if (nf.get('path') or [None])[0]=='RevealerParams' and nf.get('tag')=='RevealFlags'],
                    'areas':areas,'source_key':key,'source_id':oid,'source_type':typ})
    ru=_field_nodes(rec,'RevealUnit')
    for n in ru:
        out.append({'kind':'UNIT_REVEAL','params':n.get('attrs') or {},'source_key':key,'source_id':oid,'source_type':typ,'provenance':n.get('provenance')})
    dr=_scalar(rec,'DeathRevealRadius')
    if dr is not None:
        out.append({'kind':'DEATH_REVEAL','radius':dr,'duration':_scalar(rec,'DeathRevealDuration'),'reveal_type':_scalar(rec,'DeathRevealType'),
                    'source_key':key,'source_id':oid,'source_type':typ})
    for n in _field_nodes(rec,'BehaviorCategories'):
        a=n.get('attrs') or {}
        if str(a.get('index'))=='Reveal' and str(a.get('value')) in {'1','true','True'}:
            out.append({'kind':'REVEAL_BEHAVIOR','duration':_scalar(rec,'Duration'),
                        'source_key':key,'source_id':oid,'source_type':typ,'provenance':n.get('provenance')})
            break
    center,center_evidence=_vision_center_for_record(rec)
    for p in out:
        p.setdefault('vision_center',center)
        if center_evidence is not None:p.setdefault('vision_center_evidence',center_evidence)
    return out

def _execution_edges_for_record(key, rec, compiler, type_index):
    out=[]
    if not rec:return out
    for field,nodes in (rec.get('fields') or {}).items():
        for n in nodes or []:
            attrs=n.get('attrs') or {}
            for attr,val in attrs.items():
                if val is None:continue
                rule=classify_reference(key,field,attr,[field],'CANDIDATE_REFERENCE')
                if not rule.execution or not rule.expected_prefixes:continue
                tk,tr=_resolve_any(compiler,type_index,str(val),rule.expected_prefixes)
                if tk and tr:
                    out.append({'source_key':key,'target_key':tk,'relation':rule.relation,'field':field,'attr':attr,
                                'provenance':n.get('provenance')})
    return out

def _walk_execution(start_keys, compiler, type_index, max_depth=48):
    """Walk only structurally executable edges. Unknown/reference-only links never enter."""
    q=deque((k,[]) for k in start_keys if k); seen=set(); out=[]
    while q:
        key,path=q.popleft()
        if key in seen or len(path)>max_depth:continue
        seen.add(key)
        typ,oid=key.split('::',1)
        rec=compiler.resolve((typ,oid))
        if not rec:continue
        out.append({'key':key,'record':rec,'path':path})
        for e in _execution_edges_for_record(key,rec,compiler,type_index):
            q.append((e['target_key'],path+[{k:e.get(k) for k in ('source_key','target_key','relation','field','attr','provenance')}]))
    return out

def _vision_primitives_for_walk(walked):
    """Direct primitives plus search/reveal context with fail-closed center normalization."""
    out=[]; rec_by_key={w['key']:w['record'] for w in walked}
    def decorate(p,w):
        q=dict(p)
        if q.get('vision_center') in {None,'UNRESOLVED'}:
            # Nearest executable ancestor with an explicit selector wins.
            keys=[e.get('source_key') for e in reversed(w.get('path') or []) if e.get('source_key')]
            for k in keys:
                c,ev=_vision_center_for_record(rec_by_key.get(k))
                if c!='UNRESOLVED':
                    q['vision_center']=c; q['vision_center_evidence']={'inherited_from':k,**(ev or {})}; break
        if q.get('vision_center') in {None,'UNRESOLVED'} and q.get('kind') in {'AREA_REVEAL_RADIUS','AREA_REVEAL_SHAPE','SEARCH_REVEAL','REVEAL_BEHAVIOR','DEATH_REVEAL'}:
            q['vision_center']='AREA_EFFECT'
            q['vision_center_evidence']={'proof':'EXECUTION_REACHABLE_SPATIAL_VISION_EFFECT_EXACT_CENTER_UNRESOLVED'}
        return q
    for w in walked:
        for p in _vision_primitives_for_record(w['key'],w['record']):
            out.append({**decorate(p,w),'execution_path':w['path']})
    for w in walked:
        rec=w['record']; cats=_field_nodes(rec,'BehaviorCategories')
        is_reveal=any(str((n.get('attrs') or {}).get('index'))=='Reveal' and str((n.get('attrs') or {}).get('value')) in {'1','true','True'} for n in cats)
        if not is_reveal:continue
        for e in reversed(w['path']):
            sk=e.get('source_key'); sr=rec_by_key.get(sk)
            if not sr or not str(sr.get('xml_type') or '').startswith('CEffectEnumArea'):continue
            radii=[(nf.get('attrs') or {}).get('value') for nf in sr.get('nested_facts',[]) or [] if nf.get('tag')=='Radius' and (nf.get('path') or [None])[0]=='AreaArray']
            if radii:
                base={'kind':'SEARCH_REVEAL','radii':[x for x in radii if x is not None],
                      'source_key':sk,'source_id':sr.get('object_id'),'source_type':sr.get('xml_type'),
                      'reveal_behavior_id':rec.get('object_id'),'reveal_duration':_scalar(rec,'Duration'),
                      'proof':'ENUMAREA_EXECUTES_APPLY_REVEAL_BEHAVIOR','execution_path':w['path']}
                fake={'key':sk,'record':sr,'path':w['path']}
                out.append(decorate(base,fake))
            break
    return _dedupe_mechanics(out)

def _talent_direct_links(raw, tid):
    abils=[]; behaviors=[]; active=False
    for d in (raw.get('objects') or {}).get(str(tid),[]):
        if d.get('xml_type')!='CTalent':continue
        for n in d.get('descendants',[]) or []:
            path=n.get('path') or []; a=n.get('attrs') or {}; tag=n.get('tag')
            if len(path)==1 and tag=='Abil' and a.get('value') is not None: abils.append(str(a['value']))
            if len(path)==1 and tag=='Active' and str(a.get('value')) in {'1','true','True'}: active=True
            if tag=='BehaviorArray' and 'RankArray' in path and a.get('value') is not None: behaviors.append(str(a['value']))
    return {'abilities':sorted(set(abils)),'behaviors':sorted(set(behaviors)),'active':active}

def _build_talent_validator_map(raw, compiler, type_index):
    out={}
    for oid,types in type_index.items():
        ts=[t for t in types if t.startswith('CValidatorPlayerTalent')]
        if len(ts)!=1:continue
        r=compiler.resolve((ts[0],oid))
        v=_scalar(r,'Value')
        if v: out[oid]=str(v)
    return out

def _vision_candidate_objects(raw):
    tags={'SightBonus','SightMaximum','RevealRadius','RevealRectangleWidth','RevealRectangleHeight','RevealArc','RevealUnit','DeathRevealRadius','RevealerParams'}
    out=set()
    for oid,defs in (raw.get('objects') or {}).items():
        for d in defs:
            if not (str(d.get('xml_type') or '').startswith('CBehavior') or str(d.get('xml_type') or '').startswith('CEffect')):continue
            if any(n.get('tag') in tags for n in d.get('descendants',[]) or []):
                out.add(oid); break
    return out

def _validator_gated_vision(raw, compiler, type_index, validator_to_talent):
    """Vision semantics on objects whose execution/state is explicitly gated by a talent validator.

    The talent check proves conditional association. Downstream vision is walked only
    through executable edges; recipient/location remains a separate scope proof.
    """
    by_talent=defaultdict(list); validator_ids=set(validator_to_talent)
    candidates=set()
    for oid,defs in (raw.get('objects') or {}).items():
        for d in defs:
            if not (str(d.get('xml_type') or '').startswith('CBehavior') or str(d.get('xml_type') or '').startswith('CEffect')):continue
            hit=False
            for n in d.get('descendants',[]) or []:
                if 'Validator' not in str(n.get('tag') or ''):continue
                a=n.get('attrs') or {}
                if any(str(v) in validator_ids for v in a.values() if v is not None): hit=True; break
            if hit:candidates.add(oid); break
    for oid in sorted(candidates):
        key,rec=_resolve_any(compiler,type_index,oid,('CBehavior','CEffect'))
        if not rec:continue
        validators=[]
        for field,nodes in (rec.get('fields') or {}).items():
            if 'Validator' not in field:continue
            for n in nodes or []:
                v=_node_value(n)
                if v in validator_to_talent: validators.append((v,validator_to_talent[v],field,n.get('provenance')))
        if not validators:continue
        walked=_walk_execution([key],compiler,type_index)
        for vid,tid,field,prov in validators:
            for p in _vision_primitives_for_walk(walked):
                pk='TALENT_VALIDATOR_GATED_VISION_OBJECT' if p.get('source_key')==key else 'TALENT_VALIDATOR_GATED_EXECUTION_VISION'
                by_talent[tid].append({**p,'proof_kind':pk,'gated_source_key':key,'validator_id':vid,
                                      'validator_field':field,'validator_provenance':prov})
    return by_talent

def _spawn_initializer_behavior_targets_from_graph(records, edges, vision_behavior_ids):
    """Map vision behaviors reached by CEffectCreateUnit.SpawnEffect to SpawnUnit.

    Uses the already-built hero-relevant typed execution graph, avoiding a broad
    all-catalog walk. Only execution edges are traversed.
    """
    adj=defaultdict(list)
    for e in edges:
        if (e.get('propagation') or {}).get('execution'):
            adj[e.get('source_key')].append(e)
    out=defaultdict(list)
    for ck,rec in records.items():
        if not ck.startswith('CEffectCreateUnit'):continue
        units=_field_values(rec,'SpawnUnit'); spawnfx=_field_values(rec,'SpawnEffect')
        if len(units)!=1 or not spawnfx:continue
        starts=[]
        for fx in spawnfx:
            k=next((k for k,r in records.items() if k.startswith('CEffect') and r.get('object_id')==fx),None)
            if k:starts.append(k)
        q=deque((k,[]) for k in starts); seen=set()
        while q:
            cur,path=q.popleft()
            if cur in seen or len(path)>32:continue
            seen.add(cur); rr=records.get(cur)
            if rr and str(rr.get('xml_type') or '').startswith('CEffectApplyBehavior'):
                for bid in _field_values(rr,'Behavior'):
                    if bid in vision_behavior_ids:
                        out[bid].append({'unit_id':units[0],'create_unit_effect':rec.get('object_id'),
                                         'spawn_effects':spawnfx,'apply_behavior_effect':rr.get('object_id'),
                                         'execution_path':path,'proof':'PROVEN_CREATEUNIT_SPAWN_EFFECT_APPLIES_BEHAVIOR'})
            for e in adj.get(cur,[]):
                if e.get('owner_boundary'):continue
                t=e.get('target_key')
                if t:q.append((t,path+[e.get('edge_id')]))
    return out

def _attached_behavior_scopes(forms, grouped):
    out=defaultdict(list)
    for f in forms:
        scope='HERO_SELF' if f.get('role')=='PRIMARY' else 'HERO_FORM'
        for b in f['unit'].get('behaviors',[]) or []:
            out[b.get('behavior_id')].append({'target_scope':scope,'unit_id':f['unit_id'],'proof':'CUNIT_BEHAVIORARRAY'})
    for o in grouped.values():
        for b in o['entity'].get('behaviors',[]) or []:
            out[b.get('behavior_id')].append({'target_scope':'OWNED_ENTITY','unit_id':o['unit_id'],'proof':'CUNIT_BEHAVIORARRAY'})
    return out

def _dedupe_mechanics(items):
    out=[]; seen=set()
    for x in items:
        sig=json.dumps({k:v for k,v in x.items() if k not in {'execution_path','provenance','validator_provenance'}},sort_keys=True,default=str)
        if sig in seen:continue
        seen.add(sig); out.append(x)
    return out

def _runtime_morph_forms(key, records):
    """Direct runtime morph targets proved by CUnit.AbilArray -> CAbilMorph.InfoArray@Unit.
    This is structural morph evidence, not name similarity.
    """
    out=[]; seen_units={key}; q=deque([key])
    while q:
        uk=q.popleft(); ur=records.get(uk)
        if not ur: continue
        for aid in _field_values(ur,'AbilArray'):
            ak=next((k for k,r in records.items() if k.startswith('CAbilMorph') and r.get('object_id')==aid),None)
            ar=records.get(ak) if ak else None
            if not ar: continue
            for n in _field_nodes(ar,'InfoArray'):
                attrs=n.get('attrs') or {}; uid=attrs.get('Unit')
                if not uid: continue
                tk=next((k for k,r in records.items() if k.startswith('CUnit') and r.get('object_id')==str(uid)),None)
                if not tk: continue
                ev={"unit_id":str(uid),"unit_key":tk,"morph_ability_id":aid,"morph_ability_key":ak,"proof":"PROVEN_CABILMORPH_INFOARRAY_UNIT","provenance":n.get('provenance')}
                if not any(x['unit_key']==tk and x['morph_ability_id']==aid for x in out): out.append(ev)
                if tk not in seen_units:
                    seen_units.add(tk); q.append(tk)
    return out

def _unit_summary(key,records):
    r=records.get(key)
    if not r:return {"unit_key":key,"status":"UNRESOLVED_UNIT_ROOT"}
    stats={f:_scalar(r,f) for f in ('LifeStart','LifeMax','LifeRegenRate','EnergyStart','EnergyMax','EnergyRegenRate','ShieldsStart','ShieldsMax','ShieldsRegenRate','Speed','Radius','Sight') if _scalar(r,f) is not None}
    weapons=[]
    for wid in _field_values(r,'WeaponArray'):
        wk=next((k for k,wr in records.items() if k.startswith('CWeapon') and wr.get('object_id')==wid),None)
        wr=records.get(wk) if wk else None
        weapons.append({"weapon_id":wid,"weapon_key":wk,"range":_scalar(wr,'Range'),"period":_scalar(wr,'Period'),"effect":_scalar(wr,'Effect'),"status":"RESOLVED" if wr else "UNRESOLVED"})
    behaviors=[]
    for bid in _field_values(r,'BehaviorArray'):
        bk=next((k for k,br in records.items() if k.startswith('CBehavior') and br.get('object_id')==bid),None)
        br=records.get(bk) if bk else None
        item={"behavior_id":bid,"behavior_key":bk,"type":br.get('xml_type') if br else None}
        if br:
            vp=_vision_primitives_for_record(bk,br)
            if vp:item['vision_primitives']=vp
        if br and str(br.get('xml_type') or '').startswith('CBehaviorVeterancy'):
            levels=[]
            for n in _field_nodes(br,'VeterancyLevelArray'):
                levels.append({"attrs":n.get('attrs') or {},"provenance":n.get('provenance')})
            facts=[n for n in br.get('nested_facts',[]) or [] if (n.get('path') or [None])[0]=='VeterancyLevelArray']
            item['veterancy_evidence']={"levels":levels,"nested_facts":facts,"arithmetic":"UNRESOLVED","cumulative_semantics":"UNRESOLVED","runtime_level_source":"UNRESOLVED"}
        behaviors.append(item)
    morphs=[]
    for m in _runtime_morph_forms(key,records):
        mr=records.get(m['unit_key']); ms=_scalar(mr,'Sight')
        morphs.append({**m,"base_sight":ms,"numeric_base_sight":_numeric(ms)})
    return {
        "unit_key":key,"unit_id":r.get('object_id'),"unit_type":r.get('xml_type'),"status":"RESOLVED",
        "base_stats":stats,"weapons":weapons,
        "abilities":_field_values(r,'AbilArray'),"behaviors":behaviors,
        "runtime_morph_forms":morphs,
        "vision":{"base_sight":stats.get('Sight'),"numeric_base_sight":_numeric(stats.get('Sight')),
                  "attached_behavior_primitives":[p for b in behaviors for p in b.get('vision_primitives',[]) ]},
    }

def _owned_entities_for_root(root_key, raw, records, graph_edges):
    adj=defaultdict(list)
    for e in graph_edges: adj[e.get('source_key')].append(e)
    out=[]; q=deque([(root_key,[],None)]); seen=set(); launch_seen=set()
    while q:
        cur,path,root_ability=q.popleft()
        state=(cur,root_ability)
        if state in seen or len(path)>48:continue
        seen.add(state)
        for e in adj.get(cur,[]):
            t=e.get('target_key'); rel=e.get('relation'); prop=e.get('propagation') or {}
            next_ability=root_ability
            if rel=='UNIT_HAS_ABILITY' and t: next_ability=t
            np=path+[e.get('edge_id')]
            if rel=='SPAWNS_UNIT' and t:
                out.append({"unit_key":t,"unit_id":e.get('target_id'),"proof":"PROVEN_EXECUTION_SPAWN_UNIT","source_root_ability":root_ability,"edge_path":np,"terminal_edge":e})
                continue
            # Corroborated launched gameplay unit: only accept when the current root ability
            # explicitly lists the same ProducedUnitArray and AmmoUnit is not a pure missile.
            if cur and cur.startswith('CEffectLaunchMissile') and cur not in launch_seen:
                launch_seen.add(cur)
                sr=records.get(cur); ammo=_field_values(sr,'AmmoUnit')
                if len(ammo)==1 and root_ability and not _is_pure_missile_ammo(records,sr):
                    ar=records.get(root_ability); produced=set(_field_values(ar,'ProducedUnitArray'))
                    if ammo[0] in produced:
                        uk=next((k for k,r in records.items() if k.startswith('CUnit') and r.get('object_id')==ammo[0]),None)
                        if uk:
                            out.append({"unit_key":uk,"unit_id":ammo[0],"proof":"PROVEN_ABILITY_PRODUCED_EXECUTED_AMMO_UNIT","source_root_ability":root_ability,"source_launch_effect":cur,"edge_path":list(path)})
            if t and (prop.get('ownership') or prop.get('execution')) and not e.get('owner_boundary'):
                q.append((t,np,next_ability))
    ded={}
    for e in out:
        sig=(e['unit_key'],e['proof'],e.get('source_root_ability'),e.get('source_launch_effect'))
        ded[sig]=e
    return list(ded.values())

def _talent_summary(hero_rec, raw):
    talents=[]
    for n in _field_nodes(hero_rec,'TalentTreeArray'):
        a=n.get('attrs') or {}; tid=a.get('Talent') or a.get('value')
        if not tid:continue
        muts=_raw_group_evidence(raw,str(tid),'CTalent','AbilityModificationArray')
        normalized=[]
        for m in muts:
            v=m['values']; typ=v.get('Type'); catalog=v.get('Catalog') or m['root_attrs'].get('Catalog'); entry=v.get('Entry'); field=v.get('Field')
            normalized.append({
                "type":typ,"catalog":catalog,"entry":entry,"field":field,"value":v.get('Value'),"string_replacement":v.get('StringReplacement'),
                "root_attrs":m['root_attrs'],"provenance":m['provenance'],
                "semantic_hint":"VISION_FIELD_MUTATION" if catalog=='Unit' and field=='Sight' else "RAW_CATALOG_MUTATION_EVIDENCE",
                "operation_semantics":"FLAT_MODIFICATION_DECLARED" if typ=='FlatModification' else (typ or "UNRESOLVED"),
            })
        talents.append({"talent_id":str(tid),"tier":a.get('Tier'),"column":a.get('Column'),"mutations":normalized,"provenance":n.get('provenance')})
    return talents

def _hero_level_scaling(hero_id,raw):
    groups=_raw_group_evidence(raw,hero_id,'CHero','LevelScalingArray')
    out=[]
    for g in groups:
        v=g['values']; out.append({
            "ability":g['root_attrs'].get('Ability'),"catalog":v.get('Catalog'),"entry":v.get('Entry'),"field":v.get('Field'),"value":v.get('Value'),
            "affected_by_overdrive":v.get('AffectedByOverdrive'),"affected_by_ability_power":v.get('AffectedByAbilityPower'),
            "field_is_integer":v.get('FieldIsInteger'),"provenance":g['provenance'],
            "semantics":"LEVEL_SCALING_MODIFICATION_EVIDENCE","runtime_arithmetic":"UNRESOLVED_ENGINE_LEVEL_SCALING_APPLICATION"
        })
    return out



SCALING_CATALOG_PREFIXES={
    'Unit':('CUnit',), 'Effect':('CEffect',), 'Behavior':('CBehavior',),
    'Accumulator':('CAccumulator',), 'Validator':('CValidator',),
    'Weapon':('CWeapon',), 'Abil':('CAbil',), 'Ability':('CAbil',),
}

def _resolve_scaling_target(compiler, type_index, catalog, entry):
    if not entry:
        return None,None,'MISSING_ENTRY'
    prefixes=SCALING_CATALOG_PREFIXES.get(catalog)
    # Some CHero.LevelScalingArray rows omit Catalog while naming an ability.
    # Resolve only when the target id is unambiguous across gameplay types.
    if prefixes is None and catalog is None:
        types=[t for t in type_index.get(str(entry),[]) if any(t.startswith(p) for p in ('CUnit','CEffect','CBehavior','CAccumulator','CValidator','CWeapon','CAbil'))]
        if len(types)!=1:return None,None,'CATALOG_OMITTED_TARGET_AMBIGUOUS'
        key=f"{types[0]}::{entry}"
        return key,compiler.resolve((types[0],str(entry))),'RESOLVED_CATALOG_OMITTED_UNIQUE_TYPE'
    if not prefixes:
        return None,None,'UNSUPPORTED_CATALOG'
    key,rec=_resolve_any(compiler,type_index,str(entry),prefixes)
    return key,rec,'RESOLVED' if rec else 'TARGET_UNRESOLVED'

def _inheritance_ids(rec):
    return [str(x.get('id')) for x in (rec or {}).get('inheritance_chain',[]) if x.get('id') is not None]

def _basic_attack_damage_proof(forms, compiler, type_index):
    out={}
    for f in forms:
        uid=f.get('unit_id')
        for w in (f.get('unit') or {}).get('weapons',[]) or []:
            eid=w.get('effect')
            if not eid:continue
            ek,er=_resolve_any(compiler,type_index,eid,('CEffect',))
            if not ek:continue
            for x in _walk_execution([ek],compiler,type_index):
                r=x.get('record') or {}
                if str(r.get('xml_type') or '').startswith('CEffectDamage'):
                    oid=str(r.get('object_id'))
                    out.setdefault(oid,[]).append({
                        'form_unit_id':uid,'weapon_id':w.get('weapon_id'),'weapon_effect_id':eid,
                        'damage_effect_key':x.get('key'),'execution_path':x.get('path') or [],
                        'proof':'PROVEN_WEAPON_EXECUTION_REACHES_DAMAGE_EFFECT',
                    })
    return out

def _scaling_semantic_class(target_rec, field, ability, entry, basic_attack_damage):
    typ=str((target_rec or {}).get('xml_type') or '')
    f=str(field or '')
    if typ.startswith('CUnit'):
        if f in {'LifeStart','LifeMax'}: return 'HERO_HEALTH_POOL'
        if f=='LifeRegenRate': return 'HERO_HEALTH_REGEN'
        if f in {'EnergyStart','EnergyMax'}: return 'HERO_RESOURCE_POOL'
        if f in {'EnergyRegenRate'}: return 'HERO_RESOURCE_REGEN'
        if f in {'ShieldsStart','ShieldsMax'}: return 'HERO_SHIELD_POOL'
        if f in {'ShieldRegenRate','ShieldsRegenRate'}: return 'HERO_SHIELD_REGEN'
        return 'UNIT_PARAMETER'
    if typ.startswith('CEffectDamage'):
        if f=='Amount' and entry in basic_attack_damage:return 'BASIC_ATTACK_DAMAGE'
        if f=='Amount' and ability:return 'ABILITY_DAMAGE'
        if f.startswith('Amount'):return 'DAMAGE_AMOUNT_PARAMETER'
        return 'DAMAGE_EFFECT_PARAMETER'
    if typ.startswith('CEffectCreateHealer'):
        if f=='RechargeVitalRate' and ability:return 'ABILITY_HEAL'
        if f=='RechargeVitalRate':return 'HEAL_AMOUNT_PARAMETER'
        if f.startswith('FlatModifierArray'):return 'HEAL_MODIFIER_PARAMETER'
        return 'HEAL_EFFECT_PARAMETER'
    if typ.startswith('CBehavior'):
        inh=set(_inheritance_ids(target_rec))
        if 'StormShield' in inh and f in {'DamageResponse.ModifyLimit','FlatShieldBonus'}:
            return 'ABILITY_SHIELD' if ability else 'SHIELD_AMOUNT_PARAMETER'
        if f.startswith('DamageResponse.'):
            return 'DAMAGE_RESPONSE_PARAMETER'
        if f.startswith('Modification.VitalMaxArray'):
            return 'BEHAVIOR_VITAL_MAX_PARAMETER'
        if f.startswith('Modification.VitalRegenArray'):
            return 'BEHAVIOR_VITAL_REGEN_PARAMETER'
        if f.startswith('Modification.DamageDealtScaled'):
            return 'DAMAGE_DEALT_MODIFIER_PARAMETER'
        return 'BEHAVIOR_PARAMETER'
    if typ.startswith('CAccumulator'):
        return 'FORMULA_ACCUMULATOR_PARAMETER'
    if typ.startswith('CValidator'):
        return 'VALIDATOR_THRESHOLD_PARAMETER'
    if typ.startswith('CEffectModifyUnit'):
        return 'MODIFY_UNIT_PARAMETER'
    if typ.startswith('CEffect'):
        return 'EFFECT_PARAMETER'
    if typ.startswith('CAbil'):
        return 'ABILITY_PARAMETER'
    return 'UNRESOLVED_SCALING_TARGET'

def _talent_scaling_interactions(level_scaling, talents):
    out=[]
    for i,s in enumerate(level_scaling):
        if not s.get('catalog') or not s.get('entry') or not s.get('field'):continue
        for t in talents:
            for m in t.get('mutations',[]) or []:
                if m.get('catalog')==s.get('catalog') and m.get('entry')==s.get('entry') and m.get('field')==s.get('field'):
                    out.append({
                        'scaling_record_index':i,'talent_id':t.get('talent_id'),'tier':t.get('tier'),'column':t.get('column'),
                        'mutation':m,'interaction_semantics':'SAME_CATALOG_FIELD_MUTATION',
                        'application_order':'UNRESOLVED_ENGINE_TALENT_VS_LEVEL_SCALING_ORDER',
                    })
    return out

def _typed_level_scaling(level_scaling, talents, forms, compiler, type_index):
    basic=_basic_attack_damage_proof(forms,compiler,type_index)
    interactions=_talent_scaling_interactions(level_scaling,talents)
    by_idx=defaultdict(list)
    for x in interactions:by_idx[x['scaling_record_index']].append(x)
    out=[]
    for i,s in enumerate(level_scaling):
        key,rec,status=_resolve_scaling_target(compiler,type_index,s.get('catalog'),s.get('entry'))
        sem=_scaling_semantic_class(rec,s.get('field'),s.get('ability'),s.get('entry'),basic)
        item={**s,
              'target_key':key,'target_type':(rec or {}).get('xml_type'),'target_resolution':status,
              'semantic_class':sem,'runtime_arithmetic':'UNRESOLVED_ENGINE_LEVEL_SCALING_APPLICATION',
              'talent_interactions':by_idx.get(i,[])}
        if s.get('entry') in basic:item['basic_attack_proofs']=basic[s['entry']]
        out.append(item)
    return out

def _normalize_veterancy_scaling(forms):
    out=[]
    for f in forms:
        for b in (f.get('unit') or {}).get('behaviors',[]) or []:
            ve=b.get('veterancy_evidence')
            if not ve:continue
            mods=[]
            for nf in ve.get('nested_facts',[]) or []:
                vc=nf.get('veterancy_level_context') or {}
                a=nf.get('attrs') or {}; tag=nf.get('tag'); idx=a.get('index')
                semantic='VETERANCY_MODIFICATION'
                if tag=='VitalMaxArray' and idx=='Energy':semantic='RESOURCE_MAX_LEVEL_DELTA_EVIDENCE'
                elif tag=='VitalRegenArray' and idx=='Energy':semantic='RESOURCE_REGEN_LEVEL_DELTA_EVIDENCE'
                elif tag=='VitalMaxArray' and idx=='Life':semantic='HEALTH_MAX_LEVEL_DELTA_EVIDENCE'
                elif tag=='VitalRegenArray' and idx=='Life':semantic='HEALTH_REGEN_LEVEL_DELTA_EVIDENCE'
                mods.append({
                    'level_index':vc.get('level_index'),'level_context_status':vc.get('status'),
                    'field_operation':vc.get('field_operation'),'tag':tag,'index':idx,'value':a.get('value'),
                    'semantic_class':semantic,'operation_semantics':'UNRESOLVED_ENGINE_VETERANCY_MODIFICATION_OPERATION',
                    'provenance':nf.get('provenance'),
                })
            level_indexes=[]
            for lv in ve.get('levels',[]) or []:
                a=lv.get('attrs') or {}
                if a.get('index') is not None:
                    try:level_indexes.append(int(a.get('index')))
                    except Exception:pass
            out.append({
                'form_unit_id':f.get('unit_id'),'behavior_id':b.get('behavior_id'),'behavior_type':b.get('type'),
                'catalog_slot_count':len(ve.get('levels',[]) or []),'explicit_level_indices':sorted(set(level_indexes)),
                'modifications':mods,'runtime_level_source':'UNRESOLVED_ENGINE_LEVEL_SOURCE',
                'cumulative_semantics':'UNRESOLVED_ENGINE_VETERANCY_CUMULATIVE_SEMANTICS',
            })
    return out

def _scaling_level_domain(veterancy):
    slots=[x.get('catalog_slot_count') for x in veterancy if x.get('catalog_slot_count') is not None]
    max_exp=max([i for x in veterancy for i in (x.get('explicit_level_indices') or [])], default=None)
    return {
        'catalog_veterancy_slot_counts':sorted(set(slots)),
        'max_explicit_veterancy_index':max_exp,
        'normal_1_30_catalog_evidence':'PROVEN_30_SLOT_VETERANCY_PRESENT' if 30 in slots else 'UNRESOLVED',
        'display_level_to_slot_mapping':'UNRESOLVED_ENGINE_LEVEL_MAPPING',
        'post_catalog_slot_behavior':'UNRESOLVED_ENGINE_BEYOND_CATALOG_SLOTS',
        'runtime_arithmetic':'UNRESOLVED_ENGINE_LEVEL_SCALING_APPLICATION',
    }

def build(raw:dict):
    compiler=EffectiveCatalogCompiler(raw)
    type_index=_build_type_index(raw)
    validator_to_talent=_build_talent_validator_map(raw,compiler,type_index)
    validator_gated_vision=_validator_gated_vision(raw,compiler,type_index,validator_to_talent)
    vision_behavior_ids={x.get('source_id') for vals in validator_gated_vision.values() for x in vals if str(x.get('source_type') or '').startswith('CBehavior')}
    chero_records={}
    for oid,defs in (raw.get('objects') or {}).items():
        types={d.get('xml_type') for d in defs}
        if 'CHero' in types:
            r=compiler.resolve(('CHero',oid))
            if r: chero_records[f'CHero::{oid}']=r
    playable=_playable_heroes(chero_records,raw); pset=set(playable)
    hero_forms={}; form_ids=[]
    for hid in playable:
        forms=_collect_forms(hid,chero_records[f'CHero::{hid}'],chero_records,raw,pset)
        hero_forms[hid]=forms; form_ids.extend(f['unit_id'] for f in forms)
    eff,eff_report=build_unit_effective_catalog_data(raw,sorted(set(form_ids)))
    graph,graph_report=build_typed_reference_graph_data(eff)
    records=eff['records']; edges=graph['edges']
    spawn_behavior_targets=_spawn_initializer_behavior_targets_from_graph(records,edges,vision_behavior_ids)
    heroes={}; owned_index=defaultdict(list)
    for hid in playable:
        hr=chero_records[f'CHero::{hid}']; forms=[]; owned=[]
        for f in hero_forms[hid]:
            uk=next((k for k,r in records.items() if k.startswith('CUnit') and r.get('object_id')==f['unit_id']),None)
            fs={**f,"unit_key":uk,"unit":_unit_summary(uk,records) if uk else {"status":"UNRESOLVED_UNIT_ROOT","unit_id":f['unit_id']}}
            forms.append(fs)
            if uk:
                for e in _owned_entities_for_root(uk,raw,records,edges):
                    e['source_form_unit_id']=f['unit_id']; e['entity']=_unit_summary(e['unit_key'],records)
                    owned.append(e)
        # dedupe owned evidence but preserve multiple proof origins in a list
        grouped={}
        for e in owned:
            k=e['unit_id']; base=grouped.setdefault(k,{"unit_id":k,"unit_key":e['unit_key'],"entity":e['entity'],"semantic_class":"UNRESOLVED_RUNTIME_ENTITY_CLASS","proofs":[]})
            proof={x:e.get(x) for x in ('proof','source_form_unit_id','source_root_ability','source_launch_effect','edge_path') if e.get(x) is not None}
            base['proofs'].append(proof)
        talents=_talent_summary(hr,raw)
        level_scaling=_hero_level_scaling(hid,raw)
        typed_level_scaling=_typed_level_scaling(level_scaling,talents,forms,compiler,type_index)
        veterancy_scaling=_normalize_veterancy_scaling(forms)
        level_domain=_scaling_level_domain(veterancy_scaling)
        form_ids_set={f['unit_id'] for f in forms}
        form_role_by_id={f['unit_id']:f.get('role') for f in forms}
        owned_ids=set(grouped)
        owned_morph_ids={m['unit_id'] for o in grouped.values() for m in o['entity'].get('runtime_morph_forms',[])}
        vision_mut=[]
        for t in talents:
            for m in t['mutations']:
                if m['semantic_hint']!='VISION_FIELD_MUTATION': continue
                entry=m.get('entry')
                if entry in form_ids_set: scope='HERO_SELF' if form_role_by_id.get(entry)=='PRIMARY' else 'HERO_FORM'
                elif entry in owned_ids: scope='OWNED_ENTITY'
                elif entry in owned_morph_ids: scope='OWNED_ENTITY_RUNTIME_FORM'
                else: scope='OTHER_UNIT_TARGET'
                vision_mut.append({"talent_id":t['talent_id'],"tier":t['tier'],"column":t['column'],"target_scope":scope,"mutation":m})
        owned_vision=[]
        for o in grouped.values():
            profiles=[]
            base_s=(o['entity'].get('vision') or {}).get('base_sight'); base_n=(o['entity'].get('vision') or {}).get('numeric_base_sight')
            if (base_n or 0)>0: profiles.append({"unit_id":o['unit_id'],"form_role":"SPAWN_ROOT","sight":base_s,"vision_center":"OWNED_ENTITY","target_scope":"OWNED_ENTITY"})
            for m in o['entity'].get('runtime_morph_forms',[]):
                if (m.get('numeric_base_sight') or 0)>0: profiles.append({"unit_id":m['unit_id'],"form_role":"RUNTIME_MORPH","sight":m.get('base_sight'),"vision_center":"OWNED_ENTITY","target_scope":"OWNED_ENTITY_RUNTIME_FORM","proof":m.get('proof'),"morph_ability_id":m.get('morph_ability_id')})
            attached=[]
            for b in o['entity'].get('behaviors',[]) or []:
                attached.extend([{**p,'behavior_id':b.get('behavior_id'),'proof_kind':'OWNED_ENTITY_ATTACHED_BEHAVIOR','target_scope':'OWNED_ENTITY','vision_center':'OWNED_ENTITY'} for p in b.get('vision_primitives',[])])
            if profiles or attached: owned_vision.append({"entity_root_unit_id":o['unit_id'],"vision_profiles":profiles,"behavior_vision_primitives":attached,"proofs":o['proofs']})

        # General ability reveal mechanics already present on hero forms.
        ability_vision=[]
        hero_gameplay_abilities=[]
        for n in _field_nodes(hr,'HeroAbilArray'):
            a=n.get('attrs') or {}; aid=a.get('Abil') or a.get('value')
            if aid:hero_gameplay_abilities.append(str(aid))
        for aid in sorted(set(hero_gameplay_abilities)):
            ak,ar=_resolve_any(compiler,type_index,aid,('CAbil',))
            if not ak:continue
            walked=_walk_execution([ak],compiler,type_index)
            for p in _vision_primitives_for_walk(walked):
                ability_vision.append({**p,'ability_id':aid,'proof_kind':'CHERO_HEROABIL_EXECUTION_VISION',
                                       'target_scope':_effect_target_scope(p.get('vision_center'),p.get('kind'))})

        attached_scopes=_attached_behavior_scopes(forms,grouped)
        talent_vision=[]
        talent_meta={t['talent_id']:t for t in talents}
        for t in talents:
            tid=t['talent_id']; links=_talent_direct_links(raw,tid); mechanics=[]
            # Existing catalog Unit.Sight mutations become normalized vision mechanics.
            for vm in vision_mut:
                if vm['talent_id']!=tid:continue
                mechanics.append({'kind':'CATALOG_SIGHT_MUTATION','proof_kind':'TALENT_CATALOG_MUTATION',
                                  'target_scope':vm['target_scope'],'vision_center':_scope_to_vision_center(vm['target_scope']) or 'UNRESOLVED','mutation':vm['mutation']})
            # RankArray-granted behaviors: preserve primitive and keep recipient fail-closed unless independently scoped.
            for bid in links['behaviors']:
                bk,br=_resolve_any(compiler,type_index,bid,('CBehavior',))
                if not br:continue
                for p in _vision_primitives_for_record(bk,br):
                    scopes=attached_scopes.get(bid) or [{'target_scope':'TALENT_BEHAVIOR_RECIPIENT_UNRESOLVED','proof':'CTALENT_RANKARRAY_BEHAVIOR'}]
                    for sc in scopes:
                        mechanics.append({**p,'proof_kind':'TALENT_GRANTED_BEHAVIOR','behavior_id':bid,**sc,'vision_center':_scope_to_vision_center(sc.get('target_scope')) or p.get('vision_center','UNRESOLVED')})
            # Only an active talent's ability is itself talent-granted execution.
            # Passive CTalent.Abil links identify the modified base ability, not ownership
            # of every mechanic reachable from that ability.
            if links.get('active'):
                for aid in links['abilities']:
                    ak,ar=_resolve_any(compiler,type_index,aid,('CAbil',))
                    if not ak:continue
                    walked=_walk_execution([ak],compiler,type_index)
                    for p in _vision_primitives_for_walk(walked):
                        mechanics.append({**p,'proof_kind':'ACTIVE_TALENT_ABILITY_EXECUTION_VISION','ability_id':aid,
                                          'target_scope':_effect_target_scope(p.get('vision_center'),p.get('kind'))})
            # Vision-bearing objects explicitly gated by a CValidatorPlayerTalent.
            for p in validator_gated_vision.get(tid,[]):
                bid=p.get('source_id'); scopes=[]
                root_key=p.get('gated_source_key') or ''
                root_bid=root_key.split('::',1)[1] if root_key.startswith('CBehavior') and '::' in root_key else None
                scope_behavior_ids=[x for x in (bid,root_bid) if x]
                for sbid in scope_behavior_ids:
                    scopes.extend(attached_scopes.get(sbid,[]))
                    for ev in spawn_behavior_targets.get(sbid,[]):
                        uid=ev['unit_id']
                        if uid in owned_ids: scope='OWNED_ENTITY'
                        elif uid in form_ids_set: scope='HERO_SELF' if form_role_by_id.get(uid)=='PRIMARY' else 'HERO_FORM'
                        else: scope='SPAWNED_UNIT_OUTSIDE_PROVEN_HERO_OWNERSHIP'
                        scopes.append({'target_scope':scope,'unit_id':uid,'application_scope':'SPAWN_INITIALIZER','application_proof':ev})
                if not scopes: scopes=[{'target_scope':'VISION_OBJECT_RECIPIENT_UNRESOLVED'}]
                for sc in scopes:
                    mechanics.append({**p,**sc,'vision_center':_scope_to_vision_center(sc.get('target_scope')) or p.get('vision_center','UNRESOLVED')})
            if mechanics:
                talent_vision.append({'talent_id':tid,'tier':t.get('tier'),'column':t.get('column'),
                                      'direct_links':links,'mechanics':_dedupe_mechanics(mechanics)})
        talent_vision=_dedupe_mechanics(talent_vision)
        heroes[hid]={
            "hero_id":hid,"product_id":_scalar(hr,'ProductId'),"attribute_id":_scalar(hr,'AttributeId'),
            "forms":forms,"talents":talents,
            "scaling_evidence":{"chero_level_scaling":level_scaling,"typed_level_scaling":typed_level_scaling,
                                "attached_veterancy_scaling":veterancy_scaling,"level_domain":level_domain,
                                "runtime_evaluator":"UNRESOLVED_ENGINE_LEVEL_SCALING_APPLICATION"},
            "vision":{"self_sources":[{"unit_id":f['unit_id'],"sight":(f['unit'].get('vision') or {}).get('base_sight'),"role":f['role'],"vision_center":"SELF","target_scope":("HERO_SELF" if f['role']=="PRIMARY" else "HERO_FORM"),
                                        "attached_behavior_primitives":(f['unit'].get('vision') or {}).get('attached_behavior_primitives',[])} for f in forms],
                      "talent_mutations":vision_mut,"talent_vision_mechanics":talent_vision,
                      "ability_vision_sources":_dedupe_mechanics(ability_vision),
                      "owned_vision_sources":owned_vision,
                      "semantics_contract":"BASE_SIGHT + SIGHT_MODIFIER + OWNED_SOURCE + AREA/SEARCH/UNIT_REVEAL; vision_center and target_scope are independently normalized and remain UNRESOLVED where GameData does not prove them",
                      "los_resolution":"DEFER_TO_STATIC_MAP_AND_RUNTIME_STATE"},
            "owned_entities":sorted(grouped.values(),key=lambda x:x['unit_id']),
        }
        for o in grouped.values():owned_index[o['unit_id']].append(hid)
    # Integrity
    unresolved_primary=[]; unresolved_forms=[]; missing_sight=[]; bad_ownership=[]; pure_missile_accepts=[]; scaling_ctx_missing=[]
    for hid,h in heroes.items():
        prim=[f for f in h['forms'] if f['role']=='PRIMARY']
        if len(prim)!=1 or prim[0]['unit'].get('status')!='RESOLVED': unresolved_primary.append(hid)
        for f in h['forms']:
            if f['unit'].get('status')!='RESOLVED':unresolved_forms.append({"hero":hid,"unit":f['unit_id']})
        if prim and 'base_sight' not in (prim[0]['unit'].get('vision') or {}):missing_sight.append(hid)
        for o in h['owned_entities']:
            for p in o['proofs']:
                if p['proof'] not in {'PROVEN_EXECUTION_SPAWN_UNIT','PROVEN_ABILITY_PRODUCED_EXECUTED_AMMO_UNIT'}:bad_ownership.append((hid,o['unit_id'],p['proof']))
            if any(p['proof']=='PROVEN_ABILITY_PRODUCED_EXECUTED_AMMO_UNIT' for p in o['proofs']):
                ur=records.get(o['unit_key']);
                # accepted produced ammo must not be an implementation missile
                for n in _field_nodes(ur,'FlagArray'):
                    a=n.get('attrs') or {}
                    if str(a.get('index')).lower()=='missile' and str(a.get('value'))=='1': pure_missile_accepts.append((hid,o['unit_id']))
        for f in h['forms']:
            for b in f['unit'].get('behaviors',[]):
                ve=b.get('veterancy_evidence')
                if ve:
                    for nf in ve.get('nested_facts',[]):
                        if nf.get('veterancy_level_context',{}).get('status')!='RESOLVED':scaling_ctx_missing.append((hid,b['behavior_id']))
    # Known regression fixtures from actual catalog are informational + gated where the hero exists.
    fixtures={}
    def owned(h,u):return u in {x['unit_id'] for x in heroes.get(h,{}).get('owned_entities',[])}
    fixtures['DryadWispUnit']=owned('Dryad','DryadWispUnit')
    fixtures['ZagaraCreepTumor']=owned('Zagara','ZagaraCreepTumor')
    fixtures['RexxarMisha']=owned('Rexxar','RexxarMisha')
    fixtures['ProbiusPylon']=owned('Probius','ProbiusPylon')
    fixtures['DryadSightTalent']=any(m.get('mutation',{}).get('entry')=='HeroDryad' and m.get('mutation',{}).get('field')=='Sight' for m in heroes.get('Dryad',{}).get('vision',{}).get('talent_mutations',[]))
    def tmech(hero,tid,kind,**want):
        for tv in heroes.get(hero,{}).get('vision',{}).get('talent_vision_mechanics',[]):
            if tv.get('talent_id')!=tid:continue
            for m in tv.get('mechanics',[]):
                if m.get('kind')!=kind:continue
                ok=True
                for k,v in want.items():
                    if str(m.get(k))!=str(v):ok=False;break
                if ok:return True
        return False
    fixtures['MurkyEggBaseSight6']=any(o.get('entity_root_unit_id')=='MurkyRespawnEgg' and any(str(p.get('sight'))=='6' for p in o.get('vision_profiles',[])) for o in heroes.get('Murky',{}).get('vision',{}).get('owned_vision_sources',[]))
    fixtures['MurkyFishEyeSightBonus18']=tmech('Murky','MurkyFishEye','SIGHT_BONUS',value='18',target_scope='OWNED_ENTITY',unit_id='MurkyRespawnEgg')
    fixtures['MurkyFishEyeReveal24_5']=tmech('Murky','MurkyFishEye','SEARCH_REVEAL',target_scope='OWNED_ENTITY',unit_id='MurkyRespawnEgg') and any('24.5' in (m.get('radii') or []) for tv in heroes.get('Murky',{}).get('vision',{}).get('talent_vision_mechanics',[]) if tv.get('talent_id')=='MurkyFishEye' for m in tv.get('mechanics',[]) if m.get('kind')=='SEARCH_REVEAL')
    fixtures['MedivhDustSightBonus1_75']=tmech('Medivh','MedivhDustOfAppearance','SIGHT_BONUS',value='1.75')
    fixtures['MedivhDustReveal18']=tmech('Medivh','MedivhDustOfAppearance','AREA_REVEAL_RADIUS',radius='18')
    # Bird's Eye View remains present in GameData but is not in the current CHero talent tree.
    # Preserve that distinction: validate the +14/5s catalog mechanic without attributing stale talent ownership.
    bird_key,bird_rec=_resolve_any(compiler,type_index,'MedivhTransformRavenBirdsEyeView',('CAbil',))
    bird_prims=_vision_primitives_for_walk(_walk_execution([bird_key],compiler,type_index)) if bird_key and bird_rec else []
    fixtures['MedivhBirdsEyeCatalogSightBonus14']=any(p.get('kind')=='SIGHT_BONUS' and str(p.get('value'))=='14' for p in bird_prims)
    fixtures['MedivhBirdsEyeNotCurrentTalent']=not any(t.get('talent_id')=='MedivhTransformRavenBirdsEyeView' for t in heroes.get('Medivh',{}).get('talents',[]))
    fixtures['TassadarOracleReveal5']=any('5' in (m.get('radii') or []) for tv in heroes.get('Tassadar',{}).get('vision',{}).get('talent_vision_mechanics',[]) if tv.get('talent_id')=='TassadarOracle' for m in tv.get('mechanics',[]) if m.get('kind')=='SEARCH_REVEAL')
    fixtures['ChromieTimewalkersReveal10']=tmech('Chromie','ChromieTimewalkersPursuit','AREA_REVEAL_RADIUS',radius='10')
    fixtures['ChromieDeepBreathingSightBonus12']=tmech('Chromie','ChromieDragonsBreathDeepBreathing','SIGHT_BONUS',value='12')
    fixtures['ChromieTimeTrapReveal3_5']=any(m.get('ability_id') in {'ChromieTimeTrap','ChromieTimeTrapDetonate'} and m.get('kind')=='AREA_REVEAL_RADIUS' and str(m.get('radius'))=='3.5' for m in heroes.get('Chromie',{}).get('vision',{}).get('ability_vision_sources',[]))
    fixtures['ZagaraNydusSightMutation']=any(m.get('mutation',{}).get('entry')=='ZagaraCreepTumorBurrowed' and m.get('mutation',{}).get('field')=='Sight' for m in heroes.get('Zagara',{}).get('vision',{}).get('talent_mutations',[]))
    fixtures['ExcellentManaContext']=any(b.get('behavior_id')=='ExcellentMana' and b.get('veterancy_evidence') for h in heroes.values() for f in h['forms'] for b in f['unit'].get('behaviors',[]))
    rmeta=raw.get('metadata') or {}
    source_builder=str(rmeta.get('source_builder') or rmeta.get('source') or '')
    source_v24=('storm_catalog_index_v2_4_ancestor_context' in source_builder) and (rmeta.get('source_ancestor_context_retained') is not False)
    provenance_leaks=[]
    for oid,defs in (raw.get('objects') or {}).items():
        for d in defs:
            fp=str(d.get('file') or '').lower()
            if any(tok in fp for tok in EXCLUDED_PATH_TOKENS): provenance_leaks.append({"object_id":oid,"file":d.get('file')})
    blocking_typed_unresolved=[]
    for u in graph.get('unresolved') or []:
        rule=classify_reference(u.get('source_key',''),u.get('field'),u.get('attr'),u.get('path'),u.get('relation'))
        if rule.execution or rule.ownership:
            blocking_typed_unresolved.append({"source_key":u.get('source_key'),"target_id":u.get('target_id'),"field":u.get('field'),"typed_status":u.get('typed_status'),"relation":rule.relation})
    allowed_centers={'SELF','OWNED_ENTITY','TARGET_POINT','SOURCE_POINT','AREA_EFFECT','UNRESOLVED'}
    bad_vision_centers=[]
    for hid,h in heroes.items():
        pools=[]
        pools.extend(h.get('vision',{}).get('self_sources',[]))
        pools.extend(h.get('vision',{}).get('ability_vision_sources',[]))
        for ov in h.get('vision',{}).get('owned_vision_sources',[]):
            pools.extend(ov.get('vision_profiles',[])); pools.extend(ov.get('behavior_vision_primitives',[]))
        for tv in h.get('vision',{}).get('talent_vision_mechanics',[]):pools.extend(tv.get('mechanics',[]))
        for x in pools:
            c=x.get('vision_center')
            if c not in allowed_centers:bad_vision_centers.append({'hero':hid,'center':c,'source':x.get('source_id') or x.get('unit_id')})
    # v0.2 scaling semantics integrity. Runtime arithmetic intentionally remains fail-closed.
    typed_scaling=[(hid,x) for hid,h in heroes.items() for x in h.get('scaling_evidence',{}).get('typed_level_scaling',[])]
    scaling_target_unresolved=[{'hero':hid,'entry':x.get('entry'),'field':x.get('field'),'catalog':x.get('catalog'),'status':x.get('target_resolution')} for hid,x in typed_scaling if x.get('target_resolution') not in {'RESOLVED','RESOLVED_CATALOG_OMITTED_UNIQUE_TYPE'}]
    primary_30_slot_missing=[]
    for hid,h in heroes.items():
        prim=[f.get('unit_id') for f in h.get('forms',[]) if f.get('role')=='PRIMARY']
        vs=h.get('scaling_evidence',{}).get('attached_veterancy_scaling',[])
        if not any(x.get('form_unit_id') in prim and x.get('catalog_slot_count')==30 for x in vs):primary_30_slot_missing.append(hid)
    scaling_semantic_counts=Counter(x.get('semantic_class') for _,x in typed_scaling)
    scaling_talent_interactions=sum(len(x.get('talent_interactions') or []) for _,x in typed_scaling)
    veterancy_modifications=[(hid,m) for hid,h in heroes.items() for v in h.get('scaling_evidence',{}).get('attached_veterancy_scaling',[]) for m in v.get('modifications',[])]
    arithmetic_leaks=[(hid,x.get('entry')) for hid,x in typed_scaling if x.get('runtime_arithmetic')!='UNRESOLVED_ENGINE_LEVEL_SCALING_APPLICATION']
    arithmetic_leaks += [(hid,m.get('tag')) for hid,m in veterancy_modifications if m.get('operation_semantics')!='UNRESOLVED_ENGINE_VETERANCY_MODIFICATION_OPERATION']
    def scale_fixture(hero,entry,field,semantic):
        return any(x.get('entry')==entry and x.get('field')==field and x.get('semantic_class')==semantic for x in heroes.get(hero,{}).get('scaling_evidence',{}).get('typed_level_scaling',[]))
    scaling_fixtures={
        'DryadHealth04':scale_fixture('Dryad','HeroDryad','LifeMax','HERO_HEALTH_POOL'),
        'DryadBasicAttack04':scale_fixture('Dryad','DryadHeroWeaponDamage','Amount','BASIC_ATTACK_DAMAGE'),
        'DryadAbilityDamage04':scale_fixture('Dryad','DryadNoxiousBlossomDamage','Amount','ABILITY_DAMAGE'),
        'UtherHolyLightHeal':scale_fixture('Uther','HolyLightCreateHealer','RechargeVitalRate','ABILITY_HEAL'),
        'AnduinLightBombShield':scale_fixture('Anduin','AnduinLightBombShield','DamageResponse.ModifyLimit','ABILITY_SHIELD'),
        'UtherDirectAttrScalingCaptured':any(x.get('entry')=='UtherHolyLight' and x.get('field')=='Cost[0].Vital' and x.get('target_type')=='CAbilEffectTarget' for x in heroes.get('Uther',{}).get('scaling_evidence',{}).get('typed_level_scaling',[])),
        'ExcellentManaPlus10':any(m.get('semantic_class')=='RESOURCE_MAX_LEVEL_DELTA_EVIDENCE' and str(m.get('value'))=='10' and m.get('level_index')==1 for _,m in veterancy_modifications),
        'ExcellentManaRegen00976':any(m.get('semantic_class')=='RESOURCE_REGEN_LEVEL_DELTA_EVIDENCE' and str(m.get('value'))=='0.0976' and m.get('level_index')==1 for _,m in veterancy_modifications),
        'AbathurTalentScalingInteraction':any(hid=='Abathur' and x.get('entry')=='CarapaceEvolutionShield' and any(t.get('talent_id')=='AbathurReinforcedCarapace' for t in x.get('talent_interactions',[])) for hid,x in typed_scaling),
    }
    fixtures.update({f'Scaling_{k}':v for k,v in scaling_fixtures.items()})
    gates={
        "CATALOG_V2_4_ANCESTOR_CONTEXT":"PASS" if source_v24 else "FAIL",
        "HERO_ROSTER_90":"PASS" if len(playable)==90 else "FAIL",
        "PRIMARY_UNIT_ROOTS":"PASS" if not unresolved_primary else "FAIL",
        "DECLARED_FORM_UNIT_ROOTS":"PASS" if not unresolved_forms else "FAIL",
        "BASE_SIGHT_RETENTION":"PASS" if not missing_sight else "FAIL",
        "OWNED_ENTITY_CREATION_PROOF":"PASS" if not bad_ownership else "FAIL",
        "PURE_MISSILE_EXCLUSION":"PASS" if not pure_missile_accepts else "FAIL",
        "VETERANCY_LEVEL_CONTEXT_RETENTION":"PASS" if not scaling_ctx_missing else "FAIL",
        "GLOBAL_PROVENANCE_ISOLATION":"PASS" if not provenance_leaks else "FAIL",
        "TYPED_EXECUTION_REFERENCE_CLOSURE":"PASS" if not blocking_typed_unresolved else "FAIL",
        "VISION_CENTER_NORMALIZATION":"PASS" if not bad_vision_centers else "FAIL",
        "VISION_SEMANTICS_REGRESSION_FIXTURES":"PASS" if all(fixtures.get(k) for k in ['DryadSightTalent','DryadWispUnit','ZagaraCreepTumor','ZagaraNydusSightMutation','MurkyEggBaseSight6','MurkyFishEyeSightBonus18','MurkyFishEyeReveal24_5','MedivhDustSightBonus1_75','MedivhDustReveal18','MedivhBirdsEyeCatalogSightBonus14','MedivhBirdsEyeNotCurrentTalent','TassadarOracleReveal5','ChromieTimewalkersReveal10','ChromieDeepBreathingSightBonus12','ChromieTimeTrapReveal3_5']) else "FAIL",
        "SCALING_TYPED_TARGET_CLASSIFICATION":"PASS" if len(typed_scaling)==sum(len(h['scaling_evidence']['chero_level_scaling']) for h in heroes.values()) and all(x.get('status')=='TARGET_UNRESOLVED' and x.get('entry') and x.get('field') for x in scaling_target_unresolved) else "FAIL",
        "HERO_PRIMARY_30_SLOT_VETERANCY":"PASS" if not primary_30_slot_missing else "FAIL",
        "SCALING_RUNTIME_ARITHMETIC_FAIL_CLOSED":"PASS" if not arithmetic_leaks else "FAIL",
        "SCALING_SEMANTICS_REGRESSION_FIXTURES":"PASS" if all(scaling_fixtures.values()) else "FAIL",
        "REAL_CATALOG_REGRESSION_FIXTURES":"PASS" if all(fixtures.values()) else "FAIL",
    }
    overall='PASS' if all(v=='PASS' for v in gates.values()) else 'FAIL'
    summary={
        "heroes":len(playable),"forms":sum(len(h['forms']) for h in heroes.values()),"owned_entity_links":sum(len(h['owned_entities']) for h in heroes.values()),
        "unique_owned_entities":len(owned_index),"owned_vision_sources":sum(len(h['vision']['owned_vision_sources']) for h in heroes.values()),
        "vision_talent_mutations":sum(len(h['vision']['talent_mutations']) for h in heroes.values()),
        "vision_talent_mechanics":sum(sum(len(tv.get('mechanics',[])) for tv in h['vision'].get('talent_vision_mechanics',[])) for h in heroes.values()),
        "hero_ability_vision_primitives":sum(len(h['vision'].get('ability_vision_sources',[])) for h in heroes.values()),
        "chero_level_scaling_records":sum(len(h['scaling_evidence']['chero_level_scaling']) for h in heroes.values()),
        "typed_level_scaling_records":len(typed_scaling),"scaling_target_unresolved":len(scaling_target_unresolved),
        "scaling_semantic_counts":dict(sorted(scaling_semantic_counts.items())),"scaling_talent_interactions":scaling_talent_interactions,
        "veterancy_scaling_modifications":len(veterancy_modifications),
        "typed_edges":len(edges),"typed_unresolved":len(graph.get('unresolved') or []),"typed_execution_unresolved":len(blocking_typed_unresolved),"runtime_morph_links":sum(len(f['unit'].get('runtime_morph_forms',[])) for h in heroes.values() for f in h['forms']) + sum(len(o['entity'].get('runtime_morph_forms',[])) for h in heroes.values() for o in h['owned_entities']),"overall":overall,
    }
    return {
        "schema":{"name":"PYHOTS_HERO_UNIT_DB","version":VERSION},
        "contract":{
            "roster":"CHero with ProductId + AttributeId",
            "principle":"reference != execution != ownership",
            "owned_entity":"Only typed execution spawn, or execution-reachable AmmoUnit corroborated by the owning ability ProducedUnitArray and proven non-missile.",
            "vision":"Base CUnit.Sight + SightBonus/SightMaximum behavior semantics + typed talent mutations + independently positioned owned units + execution-proven area/search/unit reveal primitives. vision_center is normalized as SELF/OWNED_ENTITY/TARGET_POINT/SOURCE_POINT/AREA_EFFECT or UNRESOLVED; recipient/target scope remains separately fail-closed; LOS union is not computed here.",
            "scaling":"Typed CHero LevelScalingArray targets + attached CBehaviorVeterancy level-context are classified as health/resource/basic-attack/ability damage/heal/shield/formula semantics. Runtime arithmetic, cumulative application, talent-vs-level order, display-level mapping, and behavior beyond catalog slots remain UNRESOLVED unless separately proven.",
            "fail_closed":True,
        },
        "summary":summary,"heroes":heroes,"owned_entity_index":dict(sorted(owned_index.items())),
        "integrity":{"overall":overall,"gates":gates,"fixtures":fixtures,"unresolved_primary":unresolved_primary,"unresolved_forms":unresolved_forms,"missing_sight":missing_sight,"bad_ownership":bad_ownership,"pure_missile_accepts":pure_missile_accepts,"scaling_context_missing":scaling_ctx_missing,"blocking_typed_unresolved":blocking_typed_unresolved,"provenance_leaks":provenance_leaks,"bad_vision_centers":bad_vision_centers,"scaling_target_unresolved":scaling_target_unresolved,"primary_30_slot_missing":primary_30_slot_missing,"arithmetic_leaks":arithmetic_leaks,"scaling_semantic_counts":dict(sorted(scaling_semantic_counts.items()))},
        "dependency_report":eff_report,"typed_graph_report":graph_report,
    }

def main():
    ap=argparse.ArgumentParser(description='Pyhots Hero Unit DB Builder v0.2')
    ap.add_argument('--catalog-index',required=True,help='Canonical storm_catalog_index_v2_4_ancestor_context JSON')
    ap.add_argument('--output-dir',required=True)
    ap.add_argument('--compact-cache',default=None,help='Optional reusable filtered global compact catalog JSON')
    args=ap.parse_args()
    src=Path(args.catalog_index).resolve(); out=Path(args.output_dir).resolve(); out.mkdir(parents=True,exist_ok=True)
    cache=Path(args.compact_cache).resolve() if args.compact_cache else out/'hero_global_compact.json'
    if cache.exists(): raw=load(cache)
    else:
        print('[1/4] Streaming canonical catalog index and filtering global gameplay catalogs...')
        raw=build_global_compact(src); save(cache,raw)
    meta=raw.get('metadata') or {}
    print('[2/4] Resolving canonical CHero roster/forms and typed unit dependency closure...')
    db=build(raw)
    print('[3/4] Writing Hero Unit DB and integrity report...')
    save(out/'hero_unit_db.json',db)
    save(out/'summary.json',db['summary'])
    save(out/'integrity_report.json',db['integrity'])
    print('[4/4]',db['integrity']['overall'],db['summary'])
    if db['integrity']['overall']!='PASS': raise SystemExit(2)

if __name__=='__main__':main()
