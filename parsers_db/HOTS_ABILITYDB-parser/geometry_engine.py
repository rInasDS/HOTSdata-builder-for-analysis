from __future__ import annotations
import json
from json_backend import load as _orjson_load, save as _orjson_save
from collections import defaultdict
from pathlib import Path
from xml_semantics import ability_starts,traverse,values_for_tag,const_value,descendants

def load(p): return _orjson_load(p)
def save(p,d): return _orjson_save(p,d)

def direct_value(objects,starts,tag):
    vals=[]
    for d in starts:
        for x in values_for_tag(objects,d,{tag}):
            # direct child of ability object only for cast semantics
            if x["xml_depth"]==1: vals.append((x["value"],d,x))
    return vals

def downstream_values(objects,reached,tags,max_graph_depth=5):
    rows=[]
    for n in reached:
        if n["depth"]>max_graph_depth:continue
        for x in values_for_tag(objects,n["defn"],tags):
            rows.append({
                "object_id":n["id"],"xml_type":n["defn"].get("xml_type"),
                "graph_depth":n["depth"],"chain":n["chain"],"file":n["defn"].get("file"),**x
            })
    return rows

def choose(rows,prefer_shallow=True):
    if not rows:return None
    rows=sorted(rows,key=lambda r:(r["graph_depth"],r.get("xml_depth",1),-abs(r["value"])))
    return rows[0]

def resolve_one(objects,a):
    starts=ability_starts(objects,a)
    if not starts:
        return {"record_key":a.get("record_key"),"status":"UNKNOWN","geometry_type":"UNKNOWN","confidence":"LOW","reason":"NO_ABILITY_XML_START"}

    reached=traverse(objects,starts,max_depth=9)
    types={str(n["defn"].get("xml_type") or "") for n in reached}

    direct_ranges=direct_value(objects,starts,"Range")
    direct_range=direct_ranges[0][0] if direct_ranges else None

    radii=downstream_values(objects,reached,{"Radius"},5)
    widths=downstream_values(objects,reached,{"Width"},5)
    lengths=downstream_values(objects,reached,{"Length"},5)
    speeds=downstream_values(objects,reached,{"Speed"},5)
    arcs=downstream_values(objects,reached,{"Arc"},2)

    radius_row=choose(radii);width_row=choose(widths);length_row=choose(lengths);speed_row=choose(speeds);arc_row=choose(arcs)
    radius=radius_row["value"] if radius_row else None
    width=width_row["value"] if width_row else None
    length=length_row["value"] if length_row else None

    has_launch="CEffectLaunchMissile" in types
    has_area=bool(types.intersection({"CEffectEnumArea","CEffectSearch"}))
    has_teleport="CEffectTeleport" in types or any("teleport" in n["id"].lower() for n in reached)
    has_create="CEffectCreateUnit" in types
    is_target=any(str(s.get("xml_type"))=="CAbilEffectTarget" for s in starts)
    is_instant=any(str(s.get("xml_type"))=="CAbilEffectInstant" for s in starts)

    # Detect vector-lock abilities: their direct Range can be a protocol clamp (e.g. 500),
    # not the actual effect reach.
    vector_lock=False
    for s in starts:
        for node in descendants(s):
            if node.get("tag")=="Flags" and node.get("attrs",{}).get("index")=="TargetVectorLock" and node.get("attrs",{}).get("value")=="1":
                vector_lock=True

    if has_teleport:
        gt="MOVEMENT_LINE";conf="HIGH"
    elif has_launch:
        gt="PROJECTILE_LINE";conf="HIGH"
    elif has_area and radius is not None:
        gt="CIRCLE";conf="HIGH" if radius_row["graph_depth"]<=3 else "MEDIUM"
    elif has_create and direct_range is not None:
        gt="SUMMON";conf="MEDIUM"
    elif is_instant and has_area and radius is not None:
        gt="CIRCLE";conf="MEDIUM"
    elif is_target and direct_range is not None and not vector_lock and direct_range < 100:
        gt="POINT_TARGET";conf="HIGH"
    else:
        gt="UNKNOWN";conf="LOW"

    # For vector-locked non-missile abilities we refuse to report Range=500 as cast range.
    cast_range=None if vector_lock and direct_range is not None and direct_range>=100 else direct_range

    evidence=(radii+widths+lengths+speeds+arcs)[:100]
    return {
        "record_key":a.get("record_key"),
        "status":"GEOMETRY_BASED" if gt!="UNKNOWN" else "UNKNOWN",
        "geometry_type":gt,
        "cast_range":cast_range,
        "raw_direct_range":direct_range,
        "radius":radius,"width":width,"length":length,
        "travel_range":length if has_launch and length is not None else (cast_range if has_launch else None),
        "speed":speed_row["value"] if speed_row else None,
        "arc":arc_row["value"] if arc_row else None,
        "self_centered":bool(is_instant and radius is not None),
        "vector_target_lock":vector_lock,
        "confidence":conf,
        "xml_start_count":len(starts),"graph_nodes":len(reached),
        "evidence":evidence,
        "reason":"VECTOR_EFFECT_RANGE_UNRESOLVED" if gt=="UNKNOWN" and vector_lock else ("NO_STRONG_GEOMETRY_PATTERN" if gt=="UNKNOWN" else None)
    }

def run_geometry(catalog_path,herodata_path,output_path,report_path):
    cat=load(catalog_path);hd=load(herodata_path);objects=cat.get("objects",{})
    heroes={};total=resolved=unknown=0;types=defaultdict(int);review=[]
    for hero,h in hd.get("heroes",{}).items():
        arr=[]
        for a in h.get("abilities",[]):
            if not a.get("id"):continue
            total+=1;g=resolve_one(objects,a);arr.append(g);types[g["geometry_type"]]+=1
            if g["geometry_type"]=="UNKNOWN":
                unknown+=1;review.append({"hero":hero,"record_key":a.get("record_key"),"ability_id":a.get("id"),"name":a.get("name"),"reason":g.get("reason")})
            else:resolved+=1
        heroes[hero]=arr
    data={"metadata":{"builder":"geometry_engine_v2_2_semantic"},"heroes":heroes}
    report={"summary":{"abilities_total":total,"resolved":resolved,"unknown":unknown,"deep_graph_only":0},
            "by_geometry_type":dict(sorted(types.items())),"review":review}
    save(output_path,data);save(report_path,report);return report
