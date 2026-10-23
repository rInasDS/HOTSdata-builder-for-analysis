from __future__ import annotations
from collections import Counter
from fact_utils import load,save,ability_records,source_fact
from xml_semantics import ability_starts,traverse,values_for_tag

def run_projectile(catalog_path, herodata_path, output_path, report_path):
    cat=load(catalog_path);hd=load(herodata_path);objects=cat.get("objects",{})
    heroes={};total=with_proj=with_mover=0;review=[]
    for hero,a in ability_records(hd):
        total+=1;facts=[];starts=ability_starts(objects,a);reached=traverse(objects,starts,max_depth=8)
        has_proj=False;has_mover=False
        for n in reached:
            d=n["defn"];typ=str(d.get("xml_type") or "");oid=n["id"]
            if typ=="CEffectLaunchMissile":
                has_proj=True
                facts.append(source_fact("DELIVERY","PROJECTILE",{"object_id":oid,"xml_type":typ,"graph_depth":n["depth"],"chain":n["chain"],"file":d.get("file")},"HIGH"))
            if typ.startswith("CMover"):
                has_mover=True
                facts.append(source_fact("MOVER_TYPE",typ,{"object_id":oid,"graph_depth":n["depth"],"chain":n["chain"],"file":d.get("file")},"HIGH"))
                for x in values_for_tag(objects,d,{"Speed","Acceleration","MaxSpeed","MinSpeed","Length","Range"}):
                    facts.append(source_fact("MOVER_"+x["tag"].upper(),x["value"],{"object_id":oid,"graph_depth":n["depth"],"raw":x["raw"],"file":d.get("file")},"MEDIUM"))
        if has_proj:with_proj+=1
        if has_mover:with_mover+=1
        if not facts:
            review.append({"hero":hero,"record_key":a.get("record_key"),"ability_id":a.get("id"),"reason":"NO_PROJECTILE_FACT"})
        heroes.setdefault(hero,[]).append({"record_key":a.get("record_key"),"ability_id":a.get("id"),"name":a.get("name"),"facts":facts})
    data={"metadata":{"builder":"projectile_parser_v1"},"heroes":heroes}
    report={"summary":{"abilities_total":total,"with_projectile":with_proj,"with_mover":with_mover},"review":review}
    save(output_path,data);save(report_path,report);return report
