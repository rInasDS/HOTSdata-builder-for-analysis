from __future__ import annotations
from copy import deepcopy
from pathlib import Path
from collections import defaultdict, Counter
from json_backend import load, save

ARRAY_TAG_HINTS=("Array","Events")

def _key(d): return (d.get("xml_type"), d.get("object_id"))
def _is_array(tag, nodes):
    return tag.endswith(ARRAY_TAG_HINTS) or any("index" in n.get("attrs",{}) for n in nodes)
def _prov(d, op, tag=None, index=None):
    x={"operation":op,"file":d.get("file"),"mod":d.get("mod"),"order":d.get("order")}
    if tag is not None:x["field"]=tag
    if index is not None:x["index"]=str(index)
    return x

def _nested_facts(definition, definition_operation):
    """Preserve nested XML scalar/reference facts without pretending we can yet
    normalize arbitrary nested patch semantics. Direct children are already
    represented in fields; only depth>1 descendants are retained here.
    """
    out=[]
    for node in definition.get("descendants",[]) or []:
        depth=int(node.get("depth") or 0)
        if depth <= 1:
            continue
        attrs=deepcopy(node.get("attrs",{}) or {})
        if not attrs:
            continue
        path=list(node.get("path") or [node.get("tag")])
        out.append({
            "tag":node.get("tag"),
            "path":path,
            "depth":depth,
            "attrs":attrs,
            "provenance":{
                **_prov(definition,"NESTED_FACT",node.get("tag"),attrs.get("index")),
                "definition_operation":definition_operation,
            },
        })
    return out

def _apply_children(state, definition):
    # state: tag -> ordered list of {tag,attrs,provenance}
    grouped=defaultdict(list)
    for c in definition.get("children",[]): grouped[c["tag"]].append(c)
    for tag, patches in grouped.items():
        cur=state.setdefault(tag,[])
        array=_is_array(tag, cur+patches)
        if not array:
            # Repeated unindexed same-tag children are an array in practice.
            array=len(patches)>1 or len(cur)>1
        if not array:
            p=patches[-1]; attrs=deepcopy(p.get("attrs",{}))
            if attrs.get("removed")=="1":
                state[tag]=[]
            else:
                state[tag]=[{"tag":tag,"attrs":attrs,"provenance":_prov(definition,"SET",tag)}]
            continue
        for p in patches:
            attrs=deepcopy(p.get("attrs",{})); idx=attrs.get("index")
            if idx is None:
                if attrs.get("removed")!="1":
                    cur.append({"tag":tag,"attrs":attrs,"provenance":_prov(definition,"APPEND",tag,len(cur))})
                continue
            try: i=int(idx)
            except ValueError:
                # Named indexes are stable keys; replace matching named index or append.
                found=next((j for j,n in enumerate(cur) if n.get("attrs",{}).get("index")==idx),None)
                if attrs.get("removed")=="1":
                    if found is not None: cur.pop(found)
                elif found is None: cur.append({"tag":tag,"attrs":attrs,"provenance":_prov(definition,"PATCH",tag,idx)})
                else: cur[found]={"tag":tag,"attrs":attrs,"provenance":_prov(definition,"PATCH",tag,idx)}
                continue
            if attrs.get("removed")=="1":
                if 0 <= i < len(cur):
                    cur[i]={"tag":tag,"attrs":{"__hole__":"1","index":str(i)},"provenance":_prov(definition,"REMOVE",tag,i)}
                else:
                    while len(cur)<i:
                        cur.append({"tag":tag,"attrs":{"__hole__":"1","index":str(len(cur))},"provenance":_prov(definition,"IMPLICIT_HOLE",tag,len(cur))})
                    cur.append({"tag":tag,"attrs":{"__hole__":"1","index":str(i)},"provenance":_prov(definition,"REMOVE",tag,i)})
                continue
            node={"tag":tag,"attrs":attrs,"provenance":_prov(definition,"PATCH" if i<len(cur) else "APPEND_AT_INDEX",tag,i)}
            if i < len(cur): cur[i]=node
            else:
                while len(cur)<i: cur.append({"tag":tag,"attrs":{"__hole__":"1"},"provenance":_prov(definition,"IMPLICIT_HOLE",tag,len(cur))})
                cur.append(node)
    return state

class EffectiveCatalogCompiler:
    def __init__(self, raw):
        self.raw=raw; self.by_key=defaultdict(list); self.keys_by_id=defaultdict(list); self.cache={}; self.resolving=set(); self.unresolved=[]
        for oid,defs in raw.get("objects",{}).items():
            for d in defs:self.by_key[(d.get("xml_type"),oid)].append(d)
        for k in self.by_key:
            self.by_key[k].sort(key=lambda d:d.get("order",0))
            self.keys_by_id[k[1]].append(k)

    def parent_key(self, child_type, parent_id):
        """Resolve explicit catalog inheritance without crossing unrelated catalogs.

        HOTS uses abstract/base catalog classes as parents of concrete subclasses,
        e.g. CAccumulatorToken(parent=BaseAccumulator) where BaseAccumulator is
        CAccumulator. Exact type wins; otherwise accept the unique longest base
        type that is a strict prefix of the child type.
        """
        exact=(child_type,parent_id)
        if exact in self.by_key:
            return exact
        candidates=[k for k in self.keys_by_id.get(parent_id,[])
                    if k[0] and child_type and child_type.startswith(k[0])]
        if not candidates:
            return None
        max_len=max(len(k[0]) for k in candidates)
        best=[k for k in candidates if len(k[0])==max_len]
        return best[0] if len(best)==1 else None
    def resolve(self,key):
        if key in self.cache:return deepcopy(self.cache[key])
        if key in self.resolving:
            self.unresolved.append({"code":"INHERITANCE_CYCLE","type":key[0],"id":key[1]}); return None
        defs=self.by_key.get(key,[])
        if not defs:return None
        self.resolving.add(key)
        state={}; chain=[]; attrs={}; nested=[]; parent_id=None
        # Parent is catalog inheritance, resolved before local layered definitions.
        for d in defs:
            p=d.get("attrs",{}).get("parent")
            if p: parent_id=p; break
        if parent_id:
            pkey=self.parent_key(key[0],parent_id)
            parent=self.resolve(pkey) if pkey else None
            if parent:
                state=deepcopy(parent["fields"]); attrs=deepcopy(parent.get("effective_attrs",{})); nested=deepcopy(parent.get("nested_facts",[])); chain+=parent.get("inheritance_chain",[])+[{"type":pkey[0],"id":parent_id}]
            else:self.unresolved.append({"code":"UNRESOLVED_PARENT","type":key[0],"id":key[1],"parent":parent_id})
        contributions=[]
        for n,d in enumerate(defs):
            defop="DEFINE" if n==0 else "CONTRIBUTE"
            attrs.update({k:v for k,v in d.get("attrs",{}).items() if k not in ("id","parent")})
            _apply_children(state,d)
            nested.extend(_nested_facts(d,defop))
            contributions.append(_prov(d,defop))
        rec={"object_id":key[1],"xml_type":key[0],"parent":parent_id,"effective_attrs":attrs,"fields":state,
             "nested_facts":nested,"inheritance_chain":chain,"definitions":contributions}
        self.resolving.remove(key); self.cache[key]=deepcopy(rec); return rec
    def compile(self):
        records={}
        for key in sorted(self.by_key):
            r=self.resolve(key); records[f"{key[0]}::{key[1]}"]=r
        counts=Counter(x["code"] for x in self.unresolved)
        return {"metadata":{"builder":"effective_catalog_v1_1_nested_facts","raw_definitions":sum(len(v) for v in self.by_key.values()),"effective_records":len(records),"fail_closed":True},
                "records":records,"unresolved":self.unresolved,"unresolved_counts":dict(counts)}

def build_effective_catalog(raw_index_path:Path,out_path:Path,report_path:Path):
    raw=load(raw_index_path); result=EffectiveCatalogCompiler(raw).compile(); save(out_path,result)
    layered=sum(1 for r in result["records"].values() if len(r["definitions"])>1)
    inherited=sum(1 for r in result["records"].values() if r.get("parent"))
    report={"summary":{"effective_records":len(result["records"]),"layered_records":layered,"inherited_records":inherited,"unresolved":len(result["unresolved"])},"unresolved_counts":result["unresolved_counts"],"unresolved_list_count":len(result["unresolved"]), "unresolved_truncated":False, "unresolved":result["unresolved"]}
    save(report_path,report); return report
