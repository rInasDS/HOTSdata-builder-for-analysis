from __future__ import annotations
from collections import deque
from xml_semantics import ability_starts, descendants

# Source-edge graph: this module records only explicit GameData references.
# It deliberately does not infer ownership from names, hero prefixes or reachability.
EFFECT_TAGS={"Effect","PrepEffect","CursorEffect","InitialEffect","FinalEffect","ExpireEffect","LaunchEffect","ImpactEffect","SearchEffect","CreateEffect","ApplyEffect","PeriodicEffect","PeriodicEffectArray","EffectArray","CaseDefault"}
EFFECT_ATTRS={"Effect","LaunchEffect","ImpactEffect","InitialEffect","FinalEffect"}
BEHAVIOR_TAGS={"Behavior","BehaviorArray","BehaviorLink"}
WEAPON_TAGS={"Weapon","WeaponArray","WeaponEnableArray","WeaponDisableArray"}
TERMINAL={"CEffectDamage":"DAMAGE","CEffectCreateHealer":"HEAL","CEffectModifyUnit":"MODIFY_UNIT","CEffectCreateUnit":"SUMMON"}

# Explicit edge semantics. Only these XML fields create graph edges.
def source_edges(defn, objects):
    out=[]
    for n in descendants(defn):
        tag=str(n.get("tag") or ""); a=n.get("attrs") or {}; path=n.get("path") or [tag]
        candidates=[]
        if tag in EFFECT_TAGS:
            for key in ("value","Value"):
                if a.get(key): candidates.append((a[key],"EFFECT",tag,key))
        if tag in BEHAVIOR_TAGS:
            for key in ("value","Value"):
                if a.get(key): candidates.append((a[key],"BEHAVIOR",tag,key))
        if tag in WEAPON_TAGS:
            for key in ("value","Value"):
                if a.get(key): candidates.append((a[key],"WEAPON",tag,key))
        for key in EFFECT_ATTRS:
            if a.get(key): candidates.append((a[key],"EFFECT",tag,key))
        for ref,kind,field,key in candidates:
            ref=str(ref)
            if ref in objects:
                out.append({"to":ref,"kind":kind,"field":field,"attribute":key,"path":path})
    seen=set(); ans=[]
    for e in out:
        k=(e["to"],e["kind"],e["field"],e["attribute"],tuple(e["path"]))
        if k not in seen: seen.add(k); ans.append(e)
    return ans

def build_source_graph(objects, ability, max_depth=10):
    starts=ability_starts(objects,ability)
    q=deque((s["object_id"],0) for s in starts)
    seen=set(); nodes={}; edges=[]
    while q:
        oid,depth=q.popleft()
        if oid in seen or depth>max_depth: continue
        seen.add(oid)
        defs=objects.get(oid,[])
        nodes[oid]={"object_id":oid,"xml_types":sorted({str(d.get("xml_type") or "") for d in defs}),"files":sorted({str(d.get("file") or "") for d in defs if d.get("file")}),"depth":depth}
        if depth>=max_depth: continue
        for d in defs:
            for e in source_edges(d,objects):
                row={"from":oid,**e}
                edges.append(row); q.append((e["to"],depth+1))
    # deterministic edge dedupe
    uniq=[]; keys=set()
    for e in edges:
        k=(e["from"],e["to"],e["kind"],e["field"],e["attribute"],tuple(e["path"]))
        if k not in keys: keys.add(k); uniq.append(e)
    return {"starts":[s["object_id"] for s in starts],"nodes":nodes,"edges":uniq}

def _adj(graph):
    a={}
    for e in graph["edges"]: a.setdefault(e["from"],[]).append(e)
    return a

def _types(graph,oid): return set(graph["nodes"].get(oid,{}).get("xml_types") or [])

def _paths(graph,max_depth=10):
    adj=_adj(graph); out=[]; q=deque((s,[s],[]) for s in graph["starts"]); seen=set()
    while q:
        oid,chain,edges=q.popleft()
        state=(oid,tuple((e["from"],e["to"],e["field"]) for e in edges))
        if state in seen or len(edges)>max_depth: continue
        seen.add(state)
        if any(t in TERMINAL for t in _types(graph,oid)):
            out.append((oid,chain,edges)); continue
        for e in adj.get(oid,[]):
            if e["to"] not in chain: q.append((e["to"],chain+[e["to"]],edges+[e]))
    return out

def _classify_path(graph, oid, edges):
    fields=[e["field"] for e in edges]; kinds=[e["kind"] for e in edges]
    terminal_types=_types(graph,oid); terminal_kind=next((TERMINAL[t] for t in terminal_types if t in TERMINAL),None)
    if not terminal_kind: return None
    # Pattern semantics are based on explicit edge fields, never names.
    if "WeaponEnableArray" in fields or "WeaponArray" in fields or any("CWeapon" in _types(graph,e["to"]) for e in edges):
        cls="WEAPON_MODE"
    elif terminal_kind=="SUMMON": cls="SUMMON"
    elif "PeriodicEffect" in fields or "PeriodicEffectArray" in fields: cls="PERIODIC_EFFECT"
    elif any(k=="BEHAVIOR" for k in kinds): cls="STATE_ACTIVATION"
    else: cls="DIRECT_EFFECT"
    return cls,terminal_kind

def resolve_execution(objects, ability, max_depth=10):
    graph=build_source_graph(objects,ability,max_depth=max_depth)
    terminals=[]; classes=[]
    for oid,chain,edges in _paths(graph,max_depth=max_depth):
        c=_classify_path(graph,oid,edges)
        if not c: continue
        cls,kind=c
        terminals.append({"object_id":oid,"terminal_kind":kind,"xml_types":sorted(_types(graph,oid)),"chain":chain,"edges":edges,"execution_class":cls})
        if cls not in classes: classes.append(cls)
    if not terminals: classes=["UNRESOLVED_EXECUTION_PATTERN"]
    # semantic terminal dedupe: one source terminal/path, not one XML definition
    ded=[]; seen=set()
    for t in terminals:
        k=(t["object_id"],t["execution_class"],tuple(t["chain"]))
        if k not in seen: seen.add(k); ded.append(t)
    return {"classes":classes,"starts":graph["starts"],"terminals":ded,"edge_kinds":sorted({e["kind"] for e in graph["edges"]}),"source_graph":{"nodes":list(graph["nodes"].values()),"edges":graph["edges"]}}
