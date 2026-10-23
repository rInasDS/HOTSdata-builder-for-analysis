from __future__ import annotations
import argparse, re
from pathlib import Path
import xml.etree.ElementTree as ET
from collections import Counter
from json_backend import load, save
from storm_catalog_index import build_catalog

VERSION="0.1.1"
KEYWORDS=("shrub","bush","brush","pathing","footprint","blocker","visionblock","sightblock","cliff","terrain")
SHRUB_WORDS=("shrub","bush","brush")
COORD_KEYS={"x","y","z","posx","posy","position","point","radius","minx","miny","maxx","maxy","offsetx","offsety"}
ASSET_HINTS=("terrain","pathing","placement","region","footprint","cliff","shrub","bush","brush","vision","blocker")


def _text_blob(defn):
    vals=[defn.get("object_id","") or "",defn.get("xml_type","") or ""]
    for d in defn.get("definitions",[]) or []: vals += [d.get("file","") or "",d.get("mod","") or ""]
    for f,nodes in (defn.get("fields") or {}).items():
        vals.append(str(f))
        for n in nodes or []:
            vals.extend(str(k) for k in (n.get("attrs") or {}).keys()); vals.extend(str(v) for v in (n.get("attrs") or {}).values())
    for n in defn.get("nested_facts",[]) or []:
        vals.extend(n.get("path") or []); vals.extend(str(k) for k in (n.get("attrs") or {}).keys()); vals.extend(str(v) for v in (n.get("attrs") or {}).values())
    return " ".join(vals).lower()


def _raw_def_blob(d):
    vals=[d.get("object_id","") or "",d.get("xml_type","") or "",d.get("file","") or "",d.get("mod","") or ""]
    vals.extend(str(x) for x in (d.get("attrs") or {}).values())
    for n in d.get("children",[]) or []:
        vals.append(str(n.get("tag") or "")); vals.extend(str(x) for x in (n.get("attrs") or {}).values())
    for n in d.get("descendants",[]) or []:
        vals.extend(n.get("path") or []); vals.extend(str(x) for x in (n.get("attrs") or {}).values())
    return " ".join(vals).lower()


def _map_aliases(map_db):
    out={}
    for mid,m in (map_db.get("maps") or {}).items():
        vals={mid}
        vals.update((m.get("map_name_observations") or {}).keys())
        out[mid]={re.sub(r"[^a-z0-9]","",v.lower()) for v in vals if v}
    return out


def _file_map_matches(path, aliases):
    norm=re.sub(r"[^a-z0-9]","",str(path).lower())
    return sorted(mid for mid,als in aliases.items() if any(a and a in norm for a in als))


def _scan_non_catalog_xml(mods_root:Path, aliases):
    hits=[]; scanned=0
    for p in sorted(mods_root.rglob("*.xml")):
        scanned+=1
        try:
            raw=p.read_bytes()
            low=raw.lower()
            if not any(k.encode() in low for k in KEYWORDS): continue
            root=ET.fromstring(raw)
            if str(root.tag)=="Catalog": continue
            for elem in root.iter():
                blob=(str(elem.tag)+" "+" ".join(f"{k}={v}" for k,v in elem.attrib.items())).lower()
                matched=[k for k in KEYWORDS if k in blob]
                if not matched: continue
                coord={k:v for k,v in elem.attrib.items() if k.lower() in COORD_KEYS}
                hits.append({"file":str(p),"root_tag":str(root.tag),"tag":str(elem.tag),"attrs":dict(elem.attrib),"matched_keywords":matched,"coordinate_attrs":coord,"map_matches":_file_map_matches(p,aliases)})
                if len(hits)>=5000: return scanned,hits
        except Exception:
            continue
    return scanned,hits


def _asset_candidates(root:Path, aliases):
    out=[]
    for p in sorted(root.rglob("*")):
        if not p.is_file() or p.suffix.lower()==".xml": continue
        low=str(p).lower()
        if any(h in low for h in ASSET_HINTS):
            out.append({"file":str(p),"size":p.stat().st_size,"map_matches":_file_map_matches(p,aliases),"matched_hints":[h for h in ASSET_HINTS if h in low]})
            if len(out)>=5000: break
    return out


def run(mods_root:Path,map_db_path:Path,out_dir:Path,catalog_index:Path|None=None,assets_root:Path|None=None):
    out_dir.mkdir(parents=True,exist_ok=True)
    map_db=load(map_db_path); aliases=_map_aliases(map_db)
    idx=catalog_index or out_dir/"catalog_index.json"
    if not idx.exists(): build_catalog(mods_root,idx,out_dir/"catalog_index_report.json")
    raw=load(idx)

    validators=[]; shrub_behaviors=[]; spatial_catalog=[]; pathing_catalog=[]
    type_counts=Counter()
    for oid,defs in (raw.get("objects") or {}).items():
        for d in defs:
            xt=d.get("xml_type",""); type_counts[xt]+=1; blob=_raw_def_blob(d)
            if xt.startswith("CValidatorLocationShrub"):
                validators.append({"object_id":oid,"xml_type":xt,"file":d.get("file"),"mod":d.get("mod"),"attrs":d.get("attrs"),"children":d.get("children")})
            if oid in {"ShrubStatusController","ShrubProximityDetector","ShrubProximityRevealed"} or (xt.startswith("CBehavior") and any(w in oid.lower() for w in SHRUB_WORDS)):
                shrub_behaviors.append({"object_id":oid,"xml_type":xt,"file":d.get("file"),"children":d.get("children"),"descendants":[x for x in d.get("descendants",[]) if any(w in (" ".join(x.get("path") or [])+" "+str(x.get("attrs"))).lower() for w in SHRUB_WORDS) or "damageresponse" in ("/".join(x.get("path") or [])).lower()]})
            is_spatial_type=xt.startswith(("CFootprint","CPathing","CTerrain","CActor","CDoodad"))
            if is_spatial_type and any(w in blob for w in SHRUB_WORDS):
                coords=[]
                for n in d.get("descendants",[]) or []:
                    ca={k:v for k,v in (n.get("attrs") or {}).items() if k.lower() in COORD_KEYS}
                    if ca: coords.append({"path":n.get("path"),"attrs":ca})
                spatial_catalog.append({"object_id":oid,"xml_type":xt,"file":d.get("file"),"mod":d.get("mod"),"coordinate_like_facts":coords[:50]})
            if xt.startswith(("CFootprint","CPathing","CTerrain")) or any(h in blob for h in ("pathing","blocker","cliff","visionblock","sightblock")):
                if len(pathing_catalog)<5000:
                    pathing_catalog.append({"object_id":oid,"xml_type":xt,"file":d.get("file"),"mod":d.get("mod"),"matched":[h for h in ("pathing","footprint","blocker","cliff","visionblock","sightblock") if h in blob]})

    xml_scanned,noncat=_scan_non_catalog_xml(mods_root,aliases)
    asset_root=assets_root or mods_root
    assets=_asset_candidates(asset_root,aliases)
    explicit_spatial=[x for x in spatial_catalog if x.get("coordinate_like_facts")]
    explicit_spatial += [x for x in noncat if x.get("coordinate_attrs") and any(w in x.get("matched_keywords",[]) for w in SHRUB_WORDS)]

    report={
        "schema":{"name":"PYHOTS_STATIC_MAP_PROBE","version":VERSION},
        "contract":{"probe_only":True,"no_geometry_inference_from_player_paths":True,"fail_closed":True},
        "map_ids":sorted(aliases),
        "shrub_runtime_mechanics":{
            "status":"PROVEN" if validators else "UNRESOLVED",
            "location_validator_count":len(validators),"location_validators":validators,
            "behavior_evidence_count":len(shrub_behaviors),"behavior_evidence":shrub_behaviors,
            "interpretation":"GameData contains explicit shrub-location validators; this proves shrub runtime rules, not shrub polygon coordinates." if validators else "No explicit shrub location validator found in supplied catalog.",
        },
        "shrub_geometry":{
            "status":"CANDIDATE_SOURCE_FOUND" if explicit_spatial else "UNRESOLVED",
            "explicit_spatial_candidates":explicit_spatial[:1000],
            "catalog_shrub_spatial_candidates":spatial_catalog[:1000],
            "non_catalog_xml_candidates":[x for x in noncat if any(w in x.get("matched_keywords",[]) for w in SHRUB_WORDS)][:1000],
            "note":"Candidate sources are not accepted as shrub polygons until their coordinate semantics and map placement are proven.",
        },
        "pathing_visibility_sources":{
            "catalog_candidates":pathing_catalog[:2000],"asset_candidates":assets[:2000],
            "status":"CANDIDATE_SOURCE_FOUND" if pathing_catalog or assets else "UNRESOLVED",
        },
        "scan_summary":{"catalog_objects":sum(len(v) for v in (raw.get("objects") or {}).values()),"xml_files_scanned_for_non_catalog":xml_scanned,"non_catalog_keyword_hits":len(noncat),"asset_candidates":len(assets),"top_catalog_types":type_counts.most_common(50)},
    }
    save(out_dir/"static_map_probe_report.json",report)
    integ={"schema":{"name":"PYHOTS_STATIC_MAP_PROBE_INTEGRITY","version":VERSION},"gates":{"MAP_DB_LOAD":"PASS","CATALOG_INDEX":"PASS","SHRUB_RUNTIME_RULES":"PASS" if validators else "UNRESOLVED","SHRUB_GEOMETRY":"CANDIDATE" if explicit_spatial else "UNRESOLVED"},"overall":"PASS"}
    save(out_dir/"static_map_integrity_report.json",integ)
    return report


def main():
    ap=argparse.ArgumentParser(description="Pyhots Static Map Semantics Probe v0.1")
    ap.add_argument("--mods-root",required=True)
    ap.add_argument("--map-db",required=True)
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--catalog-index",default=None,help="Reuse catalog_index.json from Unit DB Builder")
    ap.add_argument("--assets-root",default=None,help="Optional wider extracted game assets root")
    args=ap.parse_args()
    r=run(Path(args.mods_root).resolve(),Path(args.map_db).resolve(),Path(args.output_dir).resolve(),Path(args.catalog_index).resolve() if args.catalog_index else None,Path(args.assets_root).resolve() if args.assets_root else None)
    print("DONE shrub_runtime=",r["shrub_runtime_mechanics"]["status"],"shrub_geometry=",r["shrub_geometry"]["status"])
if __name__=="__main__": main()
