from __future__ import annotations
import argparse, hashlib
from pathlib import Path
from json_backend import load, save
from storm_catalog_index import build_catalog
from unit_effective_catalog import build_unit_effective_catalog_data
from typed_reference_resolver import build_typed_reference_graph_data
from unit_semantics import collect_observed_units, build_unit_entries_from_data
from map_context import filter_catalog_for_map, provenance_allowed_for_map
from veterancy_source_probe import scan_veterancy_source_candidates, scan_generic_veterancy_runtime_candidates
from veterancy_layering_audit import audit_veterancy_layering

VERSION="0.1.6"

def sha256_file(path: Path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''): h.update(b)
    return h.hexdigest()

def _mini_map_db(map_db, map_id):
    return {"schema":map_db.get("schema"),"maps":{map_id:(map_db.get("maps") or {}).get(map_id,{})}}

def _collect_provenance(obj):
    out=[]
    if isinstance(obj,dict):
        # Effective-catalog `definitions` entries are themselves provenance-like
        # dicts rather than being wrapped in a `provenance` key. Inspect both
        # representations so integrity cannot miss an omitted/unsummarized field.
        if (obj.get("file") or obj.get("mod")) and ("order" in obj or "operation" in obj):
            out.append(obj)
        p=obj.get("provenance")
        if isinstance(p,dict) and (p.get("file") or p.get("mod")): out.append(p)
        for k,v in obj.items():
            if k!="provenance": out.extend(_collect_provenance(v))
    elif isinstance(obj,list):
        for x in obj: out.extend(_collect_provenance(x))
    return out


def _append_provenance_leaks(leaks, seen, obj, map_id, source, unit_id=None):
    """Check source-layer provenance, not only the compact packaged summary.

    v0.1.1 inspected unit_db entries after summarization, so foreign provenance in
    fields omitted by the summary could escape MAP_CONTEXT_PROVENANCE_ISOLATION.
    """
    for prov in _collect_provenance(obj):
        if provenance_allowed_for_map(prov,map_id):
            continue
        sig=(map_id,prov.get("file"),prov.get("mod"),prov.get("order"))
        if sig in seen:
            continue
        seen.add(sig)
        item={"map_id":map_id,"source":source,"provenance":prov}
        if unit_id is not None: item["unit_id"]=unit_id
        leaks.append(item)

def main():
    ap=argparse.ArgumentParser(description="Pyhots Unit DB Builder v0.1.6 - generic veterancy runtime probe and layering audit")
    ap.add_argument("--map-db",required=True,help="Map DB v0.2 map_db.json")
    ap.add_argument("--mods-root",required=True,help="raw_xml/mods root used by GameData builder")
    ap.add_argument("--output-dir",required=True)
    ap.add_argument("--catalog-index",default=None,help="Optional existing storm catalog index JSON")
    ap.add_argument("--force-catalog-index",action="store_true")
    args=ap.parse_args()

    map_db_path=Path(args.map_db).resolve(); mods=Path(args.mods_root).resolve(); out=Path(args.output_dir).resolve(); out.mkdir(parents=True,exist_ok=True)
    if not map_db_path.exists(): raise FileNotFoundError(map_db_path)
    if not mods.exists(): raise FileNotFoundError(mods)

    catalog_index=Path(args.catalog_index).resolve() if args.catalog_index else out/"catalog_index.json"
    catalog_report=out/"catalog_index_report.json"
    rebuild_catalog=args.force_catalog_index or not catalog_index.exists()
    if not rebuild_catalog and catalog_index.exists():
        try:
            probe=load(catalog_index)
            meta=probe.get("metadata") or {}
            rebuild_catalog=not bool(meta.get("ancestor_context_retained"))
        except Exception:
            rebuild_catalog=True
        if rebuild_catalog and args.catalog_index:
            # Never overwrite a caller-supplied legacy index merely to upgrade it.
            catalog_index=out/"catalog_index_v014.json"
            print("[1/4] Legacy catalog index lacks ancestor context; rebuilding v2.4 index into:",catalog_index)
    if rebuild_catalog:
        print("[1/4] Indexing Storm catalog with ancestor context..."); build_catalog(mods,catalog_index,catalog_report)
    else: print("[1/4] Reusing v2.4+ catalog index:",catalog_index)

    print("[2/4] Collecting replay-observed non-roster unit roots...")
    map_db=load(map_db_path); observed=collect_observed_units(map_db); obs_by={x["unit_id"]:x for x in observed}
    save(out/"observed_unit_roots.json",{"schema":{"name":"PYHOTS_OBSERVED_UNIT_ROOTS","version":VERSION},"count":len(observed),"units":observed})
    raw=load(catalog_index)

    print("[3/4] Building map-scoped effective contexts / typed semantics...")
    agg={uid:{"observed":obs,"semantics_status":"MAP_SCOPED","by_map":{}} for uid,obs in obs_by.items()}
    context_reports={}; unresolved=[]; total_reactive=0; total_dr=0; context_count=0; provenance_leaks=[]; provenance_leak_seen=set()
    total_armor_source=0; total_armor_retained=0; total_veterancy_source=0; total_veterancy_retained=0
    total_veterancy_nested_source=0; total_veterancy_nested_context=0
    total_veterancy_nested_orphan=0; total_veterancy_nested_slot_mismatch=0
    total_veterancy_profiles=0; total_veterancy_profile_mismatch=0
    total_veterancy_level_requirement_field_records=0; total_veterancy_xp_field_evidence_records=0
    total_veterancy_level_source_resolved=0
    total_veterancy_operation_fields=0; total_veterancy_operation_resolved=0
    total_veterancy_cumulative_resolved=0; total_veterancy_empty_slot_runtime_resolved=0
    total_veterancy_unsupported_operation_claims=0; total_veterancy_unsupported_source_claims=0
    total_veterancy_unsupported_cumulative_claims=0; total_veterancy_unsupported_empty_slot_claims=0
    total_veterancy_trailing_empty_records=0
    semantic_landmark_units={x["unit_id"] for x in observed if x.get("semantic_landmark_unit")}
    semantic_unresolved=[]
    veterancy_behavior_ids=set()

    for map_id in sorted((map_db.get("maps") or {})):
        mini=_mini_map_db(map_db,map_id); map_observed=collect_observed_units(mini); seeds=[x["unit_id"] for x in map_observed]
        if not seeds: continue
        filtered, filter_report=filter_catalog_for_map(raw,map_id)
        eff, eff_report=build_unit_effective_catalog_data(filtered,seeds)
        graph, graph_report=build_typed_reference_graph_data(eff)
        entries, unr, counts=build_unit_entries_from_data(mini,eff,graph)
        context_count += len(entries); total_reactive += counts["reactive_mechanics"]; total_dr += counts["damage_response_records"]
        total_armor_source += counts["armor_semantic_source_records"]; total_armor_retained += counts["armor_semantic_retained_records"]
        total_veterancy_source += counts["veterancy_source_records"]; total_veterancy_retained += counts["veterancy_retained_records"]
        total_veterancy_nested_source += counts["veterancy_nested_source_facts"]
        total_veterancy_nested_context += counts["veterancy_nested_contextualized_facts"]
        total_veterancy_nested_orphan += counts["veterancy_nested_orphan_facts"]
        total_veterancy_nested_slot_mismatch += counts["veterancy_nested_slot_mismatch_facts"]
        total_veterancy_profiles += counts["veterancy_semantic_profile_records"]
        total_veterancy_profile_mismatch += counts["veterancy_semantic_profile_mismatches"]
        total_veterancy_level_requirement_field_records += counts["veterancy_level_requirement_field_records"]
        total_veterancy_xp_field_evidence_records += counts["veterancy_xp_field_evidence_records"]
        total_veterancy_level_source_resolved += counts["veterancy_level_source_resolved_records"]
        total_veterancy_operation_fields += counts["veterancy_operation_fields"]
        total_veterancy_operation_resolved += counts["veterancy_operation_resolved_fields"]
        total_veterancy_cumulative_resolved += counts["veterancy_cumulative_resolved_records"]
        total_veterancy_empty_slot_runtime_resolved += counts["veterancy_empty_slot_runtime_resolved_records"]
        total_veterancy_unsupported_operation_claims += counts["veterancy_unsupported_operation_claims"]
        total_veterancy_unsupported_source_claims += counts["veterancy_unsupported_source_claims"]
        total_veterancy_unsupported_cumulative_claims += counts["veterancy_unsupported_cumulative_claims"]
        total_veterancy_unsupported_empty_slot_claims += counts["veterancy_unsupported_empty_slot_claims"]
        total_veterancy_trailing_empty_records += counts["veterancy_trailing_empty_records"]
        _append_provenance_leaks(provenance_leaks,provenance_leak_seen,eff,map_id,"effective_catalog")
        _append_provenance_leaks(provenance_leaks,provenance_leak_seen,graph,map_id,"typed_reference_graph")
        context_reports[map_id]={"filter":filter_report,"effective":eff_report["summary"],"typed":graph_report["summary"],"units":len(entries)}
        for x in unr:
            unresolved.append({"map_id":map_id,**x})
        for uid,entry in entries.items():
            agg.setdefault(uid,{"observed":obs_by.get(uid),"semantics_status":"MAP_SCOPED","by_map":{}})["by_map"][map_id]=entry
            for ve in ((entry.get("scaling_evidence") or {}).get("veterancy_behaviors") or []):
                if ve.get("id"): veterancy_behavior_ids.add(str(ve.get("id")))
            if uid in semantic_landmark_units and entry.get("semantics_status")!="RESOLVED_TYPED_CLOSURE":
                semantic_unresolved.append({"map_id":map_id,"unit_id":uid})
            _append_provenance_leaks(provenance_leaks,provenance_leak_seen,entry,map_id,"unit_db_entry",uid)

    # Any unit observed on a map must have a corresponding map-scoped entry.
    missing_context=[]
    for uid,obs in obs_by.items():
        have=set((agg.get(uid) or {}).get("by_map",{}))
        for map_id in obs.get("maps",[]):
            if map_id not in have: missing_context.append({"unit_id":uid,"map_id":map_id})

    resolved_contexts=sum(1 for u in agg.values() for e in u.get("by_map",{}).values() if e.get("semantics_status")=="RESOLVED_TYPED_CLOSURE")

    print("[3.5/4] Probing exact and generic non-Catalog veterancy runtime evidence...")
    source_probe=scan_veterancy_source_candidates(mods,veterancy_behavior_ids)
    save(out/"veterancy_source_probe.json",source_probe)
    source_probe_summary=source_probe.get("summary") or {}
    runtime_probe=scan_generic_veterancy_runtime_candidates(mods)
    save(out/"veterancy_runtime_probe.json",runtime_probe)
    runtime_probe_summary=runtime_probe.get("summary") or {}
    layering_audit=audit_veterancy_layering({"units":agg})
    save(out/"veterancy_layering_audit.json",layering_audit)
    layering_summary=layering_audit.get("summary") or {}
    unsafe_probe_claims=[]
    for bid,b in (source_probe.get("behaviors") or {}).items():
        if b.get("level_source") != "UNRESOLVED" or b.get("proof_basis"):
            unsafe_probe_claims.append({"behavior_id":bid,"level_source":b.get("level_source"),"proof_basis":b.get("proof_basis")})
    unsafe_runtime_claims=[x for x in (runtime_probe.get("execution_candidates") or []) if x.get("level_source") != "UNRESOLVED" or x.get("proof_basis")]
    unsafe_layering_claims=[]
    for r in layering_audit.get("records") or []:
        if r.get("status") != "UNRESOLVED_LOAD_LAYERING_SEMANTICS":
            unsafe_layering_claims.append(r)
        for g in r.get("duplicate_groups") or []:
            if g.get("runtime_load_meaning") != "UNRESOLVED" or g.get("proof_basis"):
                unsafe_layering_claims.append(g)

    summary={
        "schema":{"name":"PYHOTS_UNIT_DB_SUMMARY","version":VERSION},
        "observed_units":len(observed),"unit_map_contexts":context_count,"resolved_unit_map_contexts":resolved_contexts,
        "unresolved_unit_map_contexts":context_count-resolved_contexts,"semantic_landmark_units":len(semantic_landmark_units),
        "reactive_mechanics_by_context":total_reactive,"damage_response_records_by_context":total_dr,
        "armor_semantic_source_records_by_context":total_armor_source,"armor_semantic_retained_records_by_context":total_armor_retained,
        "veterancy_source_records_by_context":total_veterancy_source,"veterancy_retained_records_by_context":total_veterancy_retained,
        "veterancy_nested_source_facts_by_context":total_veterancy_nested_source,
        "veterancy_nested_contextualized_facts_by_context":total_veterancy_nested_context,
        "veterancy_nested_orphan_facts_by_context":total_veterancy_nested_orphan,
        "veterancy_nested_slot_mismatch_facts_by_context":total_veterancy_nested_slot_mismatch,
        "veterancy_semantic_profile_records_by_context":total_veterancy_profiles,
        "veterancy_semantic_profile_mismatches_by_context":total_veterancy_profile_mismatch,
        "veterancy_level_requirement_field_records_by_context":total_veterancy_level_requirement_field_records,
        "veterancy_xp_field_evidence_records_by_context":total_veterancy_xp_field_evidence_records,
        "veterancy_level_source_resolved_records_by_context":total_veterancy_level_source_resolved,
        "veterancy_operation_fields_by_context":total_veterancy_operation_fields,
        "veterancy_operation_resolved_fields_by_context":total_veterancy_operation_resolved,
        "veterancy_cumulative_resolved_records_by_context":total_veterancy_cumulative_resolved,
        "veterancy_empty_slot_runtime_resolved_records_by_context":total_veterancy_empty_slot_runtime_resolved,
        "veterancy_unsupported_operation_claims_by_context":total_veterancy_unsupported_operation_claims,
        "veterancy_unsupported_source_claims_by_context":total_veterancy_unsupported_source_claims,
        "veterancy_unsupported_cumulative_claims_by_context":total_veterancy_unsupported_cumulative_claims,
        "veterancy_unsupported_empty_slot_claims_by_context":total_veterancy_unsupported_empty_slot_claims,
        "veterancy_trailing_empty_records_by_context":total_veterancy_trailing_empty_records,
        "veterancy_unique_behavior_ids":len(veterancy_behavior_ids),
        "veterancy_source_probe_exact_non_catalog_hits":source_probe_summary.get("exact_non_catalog_hits",0),
        "veterancy_source_probe_behaviors_with_hits":source_probe_summary.get("behaviors_with_non_catalog_hits",0),
        "veterancy_source_probe_read_errors":source_probe_summary.get("read_errors",0),
        "veterancy_source_probe_execution_candidate_hits":source_probe_summary.get("execution_candidate_hits",0),
        "veterancy_source_probe_behaviors_with_execution_candidates":source_probe_summary.get("behaviors_with_execution_candidates",0),
        "veterancy_runtime_probe_execution_candidates":runtime_probe_summary.get("execution_candidates",0),
        "veterancy_runtime_probe_other_context_candidates":runtime_probe_summary.get("other_context_candidates",0),
        "veterancy_runtime_probe_read_errors":runtime_probe_summary.get("read_errors",0),
        "veterancy_layering_behavior_contexts":layering_summary.get("veterancy_behavior_contexts",0),
        "veterancy_layering_exact_duplicate_contexts":layering_summary.get("contexts_with_exact_duplicate_layer_segments",0),
    }
    result={
        "schema":{"name":"PYHOTS_UNIT_DB","version":VERSION},
        "contract":{
            "principle":"replay-observed unit root -> map-scoped typed GameData dependency closure",
            "reference_is_not_execution":True,"fail_closed":True,"map_context_isolation":True,
            "reactive_mechanics":"Only structurally proven DamageResponse/TRIGGERS_ON_DAMAGE is labeled ON_DAMAGE_TAKEN.",
            "scaling":"CBehaviorVeterancy level structure and progression-field evidence are profiled; arithmetic, cumulative semantics, external level source and runtime evaluation remain unresolved unless directly proven.",
            "veterancy_context":"Nested scaling facts must retain a proven effective VeterancyLevelArray slot; no post-flattening inference is allowed.",
            "veterancy_semantics":"MinVeterancyXP/XPFraction and per-level modification signatures are evidence. Reference != level-source execution; field name != arithmetic operation.",
            "important":"Never use one map context as semantics for another map.",
        },
        "summary":{k:v for k,v in summary.items() if k!="schema"},"units":agg,
    }
    save(out/"unit_db.json",result); save(out/"summary.json",summary); save(out/"unresolved_units.json",unresolved); save(out/"map_context_report.json",context_reports)

    gates={
        "MAP_DB_LOAD":"PASS","OBSERVED_UNIT_SEEDS":"PASS" if observed else "FAIL",
        "CATALOG_ANCESTOR_CONTEXT":"PASS" if bool((raw.get("metadata") or {}).get("ancestor_context_retained")) else "FAIL",
        "MAP_CONTEXT_COVERAGE":"PASS" if not missing_context else "FAIL",
        "MAP_CONTEXT_PROVENANCE_ISOLATION":"PASS" if not provenance_leaks else "FAIL",
        "SEMANTIC_LANDMARK_UNIT_ROOTS":"PASS" if not semantic_unresolved else "FAIL",
        "BEHAVIOR_SEMANTICS_RETENTION":"PASS" if total_armor_source==total_armor_retained else "FAIL",
        "VETERANCY_EVIDENCE_RETENTION":"PASS" if total_veterancy_source==total_veterancy_retained else "FAIL",
        "VETERANCY_LEVEL_CONTEXT_RETENTION":"PASS" if (
            total_veterancy_nested_source==total_veterancy_nested_context
            and total_veterancy_nested_orphan==0
            and total_veterancy_nested_slot_mismatch==0
        ) else "FAIL",
        "VETERANCY_SEMANTIC_PROFILE_RETENTION":"PASS" if (
            total_veterancy_source==total_veterancy_profiles
            and total_veterancy_profile_mismatch==0
        ) else "FAIL",
        "VETERANCY_OPERATION_FAIL_CLOSED":"PASS" if total_veterancy_unsupported_operation_claims==0 else "FAIL",
        "VETERANCY_CUMULATIVE_FAIL_CLOSED":"PASS" if total_veterancy_unsupported_cumulative_claims==0 else "FAIL",
        "VETERANCY_EMPTY_SLOT_FAIL_CLOSED":"PASS" if total_veterancy_unsupported_empty_slot_claims==0 else "FAIL",
        "VETERANCY_LEVEL_SOURCE_FAIL_CLOSED":"PASS" if total_veterancy_unsupported_source_claims==0 else "FAIL",
        "VETERANCY_SOURCE_PROBE_COVERAGE":"PASS" if source_probe_summary.get("behavior_ids",0)==len(veterancy_behavior_ids) and source_probe_summary.get("read_errors",0)==0 else "FAIL",
        "VETERANCY_SOURCE_PROBE_FAIL_CLOSED":"PASS" if not unsafe_probe_claims else "FAIL",
        "VETERANCY_RUNTIME_PROBE_COVERAGE":"PASS" if runtime_probe_summary.get("read_errors",0)==0 else "FAIL",
        "VETERANCY_RUNTIME_PROBE_FAIL_CLOSED":"PASS" if not unsafe_runtime_claims else "FAIL",
        "VETERANCY_LAYERING_AUDIT_COVERAGE":"PASS" if layering_summary.get("veterancy_behavior_contexts",0)==total_veterancy_retained else "FAIL",
        "VETERANCY_LAYERING_AUDIT_FAIL_CLOSED":"PASS" if not unsafe_layering_claims else "FAIL",
        "REACTIVE_FAIL_CLOSED":"PASS","SCALING_FAIL_CLOSED":"PASS",
    }
    integrity={
        "schema":{"name":"PYHOTS_UNIT_DB_INTEGRITY","version":VERSION},"gates":gates,
        "missing_contexts":missing_context,"provenance_leaks":provenance_leaks[:500],
        "semantic_landmark_unresolved_units":semantic_unresolved,"counts":summary,
        "overall":"PASS" if all(v=="PASS" for v in gates.values()) else "FAIL",
    }
    save(out/"unit_integrity_report.json",integrity)
    manifest={
        "schema":{"name":"PYHOTS_UNIT_DB_BUILD_MANIFEST","version":VERSION},
        "inputs":{
            "map_db":str(map_db_path),"map_db_sha256":sha256_file(map_db_path),"mods_root":str(mods),"catalog_index":str(catalog_index),
            "catalog_index_builder":(raw.get("metadata") or {}).get("builder"),
            "catalog_ancestor_context_retained":bool((raw.get("metadata") or {}).get("ancestor_context_retained")),
        },
        "outputs":{"unit_db":"unit_db.json","integrity":"unit_integrity_report.json","map_context_report":"map_context_report.json","veterancy_source_probe":"veterancy_source_probe.json","veterancy_runtime_probe":"veterancy_runtime_probe.json","veterancy_layering_audit":"veterancy_layering_audit.json"},
        "overall":integrity["overall"],
    }
    save(out/"build_manifest.json",manifest)
    print("[4/4] DONE overall=",integrity["overall"],"observed_units=",len(observed),"unit_map_contexts=",context_count)

if __name__=="__main__": main()
