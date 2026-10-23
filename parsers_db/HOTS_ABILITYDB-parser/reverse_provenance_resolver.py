from __future__ import annotations
from collections import deque, defaultdict
from typed_execution_resolver import source_edges, TERMINAL
from xml_semantics import ability_starts

# Reverse provenance is mechanics-first. Forward reachability is never ownership.
# A terminal mechanic is assigned only to its nearest CAbil ancestor(s) in the
# explicit GameData reference graph. Longer paths through cleanup/global links
# therefore cannot steal mechanics from a closer executor ability.

_CACHE={}

def _index(objects):
    key=id(objects)
    if key in _CACHE: return _CACHE[key]
    rev=defaultdict(list); types=defaultdict(set); ability_roots=set(); terminals=set()
    for oid,defs in objects.items():
        for d in defs:
            typ=str(d.get('xml_type') or '')
            types[oid].add(typ)
            if typ.startswith('CAbil'): ability_roots.add(oid)
            if typ in TERMINAL: terminals.add(oid)
            for e in source_edges(d,objects):
                rev[e['to']].append({'from':oid, **e})
    idx={'reverse':dict(rev),'types':dict(types),'ability_roots':ability_roots,'terminals':terminals}
    _CACHE[key]=idx
    return idx

def _nearest_ability_owners(idx, terminal_id, max_depth=12):
    rev=idx['reverse']; roots=idx['ability_roots']
    q=deque([(terminal_id,0,[terminal_id],[])])
    seen_depth={terminal_id:0}; found=[]; best=None
    while q:
        oid,depth,rev_chain,rev_edges=q.popleft()
        if best is not None and depth>best: break
        if depth>0 and oid in roots:
            best=depth if best is None else best
            if depth==best:
                # Return chain in execution direction: ability -> ... -> terminal.
                found.append({'owner_id':oid,'distance':depth,'chain':list(reversed(rev_chain)),
                              'edges':list(reversed(rev_edges))})
            continue
        if depth>=max_depth: continue
        for e in rev.get(oid,[]):
            parent=e['from']; nd=depth+1
            old=seen_depth.get(parent)
            if old is not None and old<nd: continue
            seen_depth[parent]=nd
            q.append((parent,nd,rev_chain+[parent],rev_edges+[e]))
    # deterministic dedupe
    out=[]; seen=set()
    for x in sorted(found,key=lambda z:(z['distance'],z['owner_id'],tuple(z['chain']))):
        k=(x['owner_id'],tuple(x['chain']))
        if k not in seen: seen.add(k); out.append(x)
    return out

def _classify(idx, terminal_id, proof):
    fields=[e.get('field') for e in proof.get('edges',[])]
    kinds=[e.get('kind') for e in proof.get('edges',[])]
    chain=proof.get('chain',[])
    chain_types=[idx['types'].get(x,set()) for x in chain]
    ttypes=idx['types'].get(terminal_id,set())
    terminal_kind=next((TERMINAL[t] for t in ttypes if t in TERMINAL),None)
    if any(any(str(t).startswith('CWeapon') for t in ts) for ts in chain_types) or 'WeaponEnableArray' in fields or 'WeaponArray' in fields:
        cls='WEAPON_MODE'
    elif terminal_kind=='SUMMON': cls='SUMMON'
    elif 'PeriodicEffect' in fields or 'PeriodicEffectArray' in fields: cls='PERIODIC_EFFECT'
    elif any(k=='BEHAVIOR' for k in kinds): cls='STATE_ACTIVATION'
    else: cls='DIRECT_EFFECT'
    return cls,terminal_kind

def resolve_reverse_provenance(objects, ability, max_depth=12):
    idx=_index(objects)
    starts=ability_starts(objects,ability)
    start_ids={s['object_id'] for s in starts}
    mechanics=[]
    for terminal_id in idx['terminals']:
        owners=_nearest_ability_owners(idx,terminal_id,max_depth=max_depth)
        for proof in owners:
            if proof['owner_id'] not in start_ids: continue
            cls,kind=_classify(idx,terminal_id,proof)
            mechanics.append({'object_id':terminal_id,'terminal_kind':kind,'execution_class':cls,
                              'owner_id':proof['owner_id'],'owner_distance':proof['distance'],
                              'chain':proof['chain'],'edges':proof['edges'],
                              'xml_types':sorted(idx['types'].get(terminal_id,set())),
                              'evidence':'REVERSE_NEAREST_ABILITY_PROVENANCE'})
    mechanics.sort(key=lambda x:(x['owner_distance'],x['object_id'],tuple(x['chain'])))
    classes=[]
    for m in mechanics:
        if m['execution_class'] not in classes: classes.append(m['execution_class'])
    if not mechanics: classes=['UNRESOLVED_EXECUTION_PATTERN']
    return {'classes':classes,'starts':sorted(start_ids),'terminals':mechanics,
            'resolver':'reverse_nearest_ability_provenance_v1'}
