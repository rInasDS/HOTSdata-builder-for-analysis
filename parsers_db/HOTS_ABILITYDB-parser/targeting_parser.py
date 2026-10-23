from __future__ import annotations
from collections import Counter
from fact_utils import load,save,ability_records,source_fact
from xml_semantics import ability_starts,descendants,const_value

def run_targeting(catalog_path, herodata_path, output_path, report_path):
    cat=load(catalog_path);hd=load(herodata_path);objects=cat.get("objects",{})
    heroes={};total=known=unknown=0;kind_counts=Counter();review=[]
    for hero,a in ability_records(hd):
        total+=1;facts=[];starts=ability_starts(objects,a)
        for d in starts:
            typ=str(d.get("xml_type") or "")
            if typ=="CAbilEffectTarget":
                facts.append(source_fact("TARGETING","POINT",{"object_id":d.get("object_id"),"xml_type":typ,"file":d.get("file")},"HIGH"))
            elif typ=="CAbilEffectInstant":
                facts.append(source_fact("TARGETING","SELF_OR_INSTANT",{"object_id":d.get("object_id"),"xml_type":typ,"file":d.get("file")},"MEDIUM"))
            elif typ.startswith("CAbil"):
                facts.append(source_fact("TARGETING","OTHER_ABILITY_TYPE",{"object_id":d.get("object_id"),"xml_type":typ,"file":d.get("file")},"LOW"))
            for node in descendants(d):
                tag=node.get("tag");attrs=node.get("attrs",{})
                if node.get("depth")==1 and tag=="Range":
                    x=const_value(objects,attrs.get("value"))
                    if x is not None:
                        facts.append(source_fact("CAST_RANGE",x,{"object_id":d.get("object_id"),"field":"Range","raw":attrs.get("value"),"file":d.get("file")},"HIGH"))
                if tag=="Flags":
                    idx=attrs.get("index")
                    if idx:
                        facts.append(source_fact("ABILITY_FLAG",{"index":idx,"value":attrs.get("value")},{"object_id":d.get("object_id"),"file":d.get("file")},"HIGH"))
        meaningful=[f for f in facts if f["kind"]=="TARGETING"]
        if meaningful:
            known+=1
            for f in meaningful:kind_counts[str(f.get("value"))]+=1
        else:
            unknown+=1;review.append({"hero":hero,"record_key":a.get("record_key"),"ability_id":a.get("id"),"reason":"NO_TARGETING_FACT"})
        heroes.setdefault(hero,[]).append({"record_key":a.get("record_key"),"ability_id":a.get("id"),"name":a.get("name"),"facts":facts})
    data={"metadata":{"builder":"targeting_parser_v1"},"heroes":heroes}
    report={"summary":{"abilities_total":total,"with_targeting_fact":known,"without_targeting_fact":unknown},
            "targeting_types":dict(kind_counts),"review":review}
    save(output_path,data);save(report_path,report);return report
