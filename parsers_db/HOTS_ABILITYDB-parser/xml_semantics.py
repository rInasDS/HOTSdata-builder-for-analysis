from __future__ import annotations
import math,re
from collections import deque

NUM=re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")

EXEC_TAGS={
    "Effect","PrepEffect","CursorEffect","InitialEffect","FinalEffect","ExpireEffect",
    "LaunchEffect","ImpactEffect","SearchEffect","CreateEffect","ApplyEffect",
    "PeriodicEffectArray","EffectArray","CaseDefault"
}
EXEC_ATTRS={"Effect","LaunchEffect","ImpactEffect","InitialEffect","FinalEffect"}

def raw_num(v):
    if v is None:return None
    s=str(v).strip()
    if not NUM.fullmatch(s):return None
    try:
        x=float(s)
        return x if math.isfinite(x) else None
    except:return None

def const_value(objects,name,seen=None):
    """Resolve simple $const references and common +,-,*,/ expressions."""
    if name is None:return None
    s=str(name).strip()
    direct=raw_num(s)
    if direct is not None:return direct

    if s.startswith("$"):
        cid=s
        seen=set() if seen is None else seen
        if cid in seen:return None
        seen.add(cid)
        for d in objects.get(cid,[]):
            v=d.get("attrs",{}).get("value")
            x=const_value(objects,v,seen)
            if x is not None:return x
        return None

    # Blizzard expression form: +($A $B), *($A 0.25), -(10 2), /(10 2)
    m=re.match(r"^\s*([+\-*/])\((.*)\)\s*$",s)
    if m:
        op=m.group(1); body=m.group(2).strip()
        # Tokenize balanced nested expressions / $constants / numbers.
        tokens=[];buf="";depth=0
        for ch in body:
            if ch=="(":
                depth+=1;buf+=ch
            elif ch==")":
                depth-=1;buf+=ch
            elif ch.isspace() and depth==0:
                if buf:tokens.append(buf);buf=""
            else:buf+=ch
        if buf:tokens.append(buf)
        vals=[const_value(objects,t,seen.copy() if seen else set()) for t in tokens]
        if len(vals)>=2 and all(v is not None for v in vals):
            try:
                acc=vals[0]
                for v in vals[1:]:
                    if op=="+":acc+=v
                    elif op=="-":acc-=v
                    elif op=="*":acc*=v
                    elif op=="/":acc/=v
                return acc
            except:return None
    return None

def descendants(defn):
    return defn.get("descendants") or [
        {"tag":c.get("tag"),"attrs":c.get("attrs",{}),"path":[c.get("tag")],"depth":1}
        for c in defn.get("children",[])
    ]

def semantic_refs(defn,objects):
    out=[]
    for node in descendants(defn):
        tag=str(node.get("tag") or "")
        attrs=node.get("attrs",{})
        # <Effect value="X">, <EffectArray value="X">, etc.
        if tag in EXEC_TAGS:
            for key in ("value","Value"):
                ref=attrs.get(key)
                if ref and str(ref) in objects:
                    out.append((str(ref),f"{tag}@{key}",node.get("path")))
        # <AreaArray Effect="X">, <CaseArray Effect="X">, etc.
        for key in EXEC_ATTRS:
            ref=attrs.get(key)
            if ref and str(ref) in objects:
                out.append((str(ref),f"{tag}@{key}",node.get("path")))
    # deterministic dedupe
    seen=set();res=[]
    for x in out:
        k=(x[0],x[1])
        if k not in seen:seen.add(k);res.append(x)
    return res

def ability_starts(objects,a):
    names=[]
    for f in ("id","ability_id","button_id"):
        if a.get(f):names.append(str(a[f]))
    if a.get("link_id"):
        names += [x for x in str(a["link_id"]).split("|") if x]
    out=[];seen=set()
    for n in names:
        for d in objects.get(n,[]):
            if not str(d.get("xml_type") or "").startswith("CAbil"):continue
            k=(d.get("object_id"),d.get("xml_type"),d.get("file"))
            if k not in seen:seen.add(k);out.append(d)
    return out

def traverse(objects,starts,max_depth=9):
    q=deque((s["object_id"],0,[s["object_id"]],None) for s in starts)
    seen=set();out=[]
    while q:
        oid,depth,chain,via=q.popleft()
        # one visit per object/depth is enough; duplicate definitions retained below
        key=(oid,depth)
        if key in seen:continue
        seen.add(key)
        for d in objects.get(oid,[]):
            out.append({"id":oid,"depth":depth,"chain":chain,"via":via,"defn":d})
            if depth<max_depth:
                for rid,edge,path in semantic_refs(d,objects):
                    q.append((rid,depth+1,chain+[rid],edge))
    return out

def values_for_tag(objects,defn,tags):
    out=[]
    for node in descendants(defn):
        tag=str(node.get("tag") or "")
        if tag not in tags:continue
        attrs=node.get("attrs",{})
        for key in ("value","Value","Radius","Range","Width","Length","Speed","TimeUse","Change","ChangeFraction"):
            if key not in attrs:continue
            x=const_value(objects,attrs.get(key))
            if x is not None:
                out.append({
                    "tag":tag,"attr":key,"value":x,
                    "xml_depth":node.get("depth",1),
                    "path":node.get("path",[]),
                    "raw":attrs.get(key),
                })
    return out
