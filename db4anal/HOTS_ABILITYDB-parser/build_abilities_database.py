from __future__ import annotations

import argparse
from pathlib import Path

from config import *
from storm_catalog_index import build_catalog
from replay_effective_catalog import build_replay_effective_catalog
from typed_reference_resolver import build_typed_reference_graph
from herodata_importer import import_herodata_file
from ability_catalog_parser import run_ability_catalog
from talent_parser import run_talent_parser
from talent_modifier_parser import run_talent_modifier_parser
from talent_modifier_decoder import run_talent_decoder
from talent_rule_miner import run_rule_miner
from targeting_parser import run_targeting
from projectile_parser import run_projectile
from area_geometry_parser import run_area_geometry
from combat_effect_parser import run_combat_effects
from semantic_ability_classifier import run_semantic_classifier
from spatial_model_builder import run_spatial_model
from subeffect_graph_builder import run_subeffect_graph
from semantic_bundle import run_semantic_bundle
from merge_engine import run_merge
from validator import run_validation
from baseline_guard import run_baseline_guard
from regression_checks import run_regressions
from combat_semantics_audit import run_combat_semantics_audit
from combat_semantic_gate import run_combat_semantic_gate


MENU = """
========================================
 HOTS UNIVERSAL DATABASE BUILDER
========================================
[1]  Raw XML catalog index (catalog records only)
[2]  HeroData import + Replay Effective Catalog
[3]  Ability catalog + role classifier
[4]  Talent family resolver
[5]  Talent mechanic facts / relation parser
[6]  Decode persistent modifiers + runtime rules
[7]  Talent rule miner
[8]  Targeting / cast facts
[9]  Projectile / mover facts
[10] Area / actor / unit geometry facts
[11] Combat effects
[12] Universal spatial semantic resolver
[13] Spatial model
[14] Ability subeffect graph
[15] Package spatial + combat semantics (zero manual overrides)
[16] Merge final database
[17] Validate / review queue
[18] Same-build baseline guard
[19] Regression checks
[20] Combat semantics audit / replay-resolver triage
[21] FULL REPRODUCIBLE BUILD: 1 -> 20
[0]  Exit
"""


def s1(mods):
    r=build_catalog(mods,CATALOG_INDEX_FILE,CATALOG_REPORT_FILE); print("[1/raw]",r["summary"]); return r
def s2(hd):
    r=import_herodata_file(hd,HERODATA_CANONICAL_FILE,HERODATA_IMPORT_REPORT_FILE);print("[2]",r["summary"])
    rr=build_replay_effective_catalog(CATALOG_INDEX_FILE,HERODATA_CANONICAL_FILE,REPLAY_EFFECTIVE_CATALOG_FILE,REPLAY_EFFECTIVE_CATALOG_REPORT_FILE,HERODATA_DUPLICATES_REPORT_FILE)
    print("[2/replay-catalog]",rr["summary"])
    tr=build_typed_reference_graph(REPLAY_EFFECTIVE_CATALOG_FILE,TYPED_REFERENCE_GRAPH_FILE,TYPED_REFERENCE_REPORT_FILE)
    print("[2/typed-refs]",tr["summary"])
    return r
def s3():
    r=run_ability_catalog(CATALOG_INDEX_FILE,HERODATA_CANONICAL_FILE,ABILITY_CATALOG_FACTS_FILE,ABILITY_CATALOG_REPORT_FILE);print("[3]",r["summary"]);return r
def s4():
    r=run_talent_parser(HERODATA_CANONICAL_FILE,TALENT_FACTS_FILE,TALENT_REPORT_FILE);print("[4]",r["summary"]);return r
def s5():
    r=run_talent_modifier_parser(CATALOG_INDEX_FILE,HERODATA_CANONICAL_FILE,TALENT_FACTS_FILE,TALENT_MODIFIER_FACTS_FILE,TALENT_MODIFIER_REPORT_FILE);print("[5]",r["summary"]);return r
def s6():
    r=run_talent_decoder(TALENT_MODIFIER_FACTS_FILE,TALENT_MODIFIERS_FILE,TALENT_RUNTIME_RULES_FILE,TALENT_DECODER_REPORT_FILE);print("[6]",r["summary"]);return r
def s7():
    r=run_rule_miner(TALENT_MODIFIER_FACTS_FILE,TALENT_RULE_MINER_REPORT_FILE);print("[7]",r["summary"]);return r
def s8():
    r=run_targeting(CATALOG_INDEX_FILE,HERODATA_CANONICAL_FILE,TARGETING_FACTS_FILE,TARGETING_REPORT_FILE);print("[8]",r["summary"]);return r
def s9():
    r=run_projectile(CATALOG_INDEX_FILE,HERODATA_CANONICAL_FILE,PROJECTILE_FACTS_FILE,PROJECTILE_REPORT_FILE);print("[9]",r["summary"]);return r
def s10():
    r=run_area_geometry(CATALOG_INDEX_FILE,HERODATA_CANONICAL_FILE,AREA_FACTS_FILE,AREA_REPORT_FILE);print("[10]",r["summary"]);return r
def s11():
    r=run_combat_effects(REPLAY_EFFECTIVE_CATALOG_FILE,TYPED_REFERENCE_GRAPH_FILE,HERODATA_CANONICAL_FILE,COMBAT_FACTS_FILE,COMBAT_REPORT_FILE);print("[11]",r["summary"]);return r
def s12():
    r=run_semantic_classifier(HERODATA_CANONICAL_FILE,ABILITY_CATALOG_FACTS_FILE,TARGETING_FACTS_FILE,PROJECTILE_FACTS_FILE,AREA_FACTS_FILE,COMBAT_FACTS_FILE,SEMANTIC_FACTS_FILE,SEMANTIC_REPORT_FILE);print("[12]",r["summary"]);return r
def s13():
    r=run_spatial_model(HERODATA_CANONICAL_FILE,ABILITY_CATALOG_FACTS_FILE,SEMANTIC_FACTS_FILE,TARGETING_FACTS_FILE,PROJECTILE_FACTS_FILE,AREA_FACTS_FILE,SPATIAL_MODEL_FILE,SPATIAL_MODEL_REPORT_FILE,REPLAY_EFFECTIVE_CATALOG_FILE,TYPED_REFERENCE_GRAPH_FILE);print("[13]",r["summary"]);return r
def s14():
    r=run_subeffect_graph(HERODATA_CANONICAL_FILE,SPATIAL_MODEL_FILE,AREA_FACTS_FILE,COMBAT_FACTS_FILE,TALENT_MODIFIER_FACTS_FILE,SUBEFFECT_GRAPH_FILE,SUBEFFECT_GRAPH_REPORT_FILE);print("[14]",r["summary"]);return r
def s15():
    r=run_semantic_bundle(SPATIAL_MODEL_FILE,COMBAT_FACTS_FILE,SEMANTIC_BUNDLE_FILE);print("[15] manual_overrides=",r.get("metadata",{}).get("manual_overrides",0));return r
def s16():
    r=run_merge(HERODATA_CANONICAL_FILE,TALENT_FACTS_FILE,TALENT_MODIFIER_FACTS_FILE,SEMANTIC_BUNDLE_FILE,SUBEFFECT_GRAPH_FILE,FINAL_DB_FILE,MERGE_REPORT_FILE);print("[16]",r["summary"]);return r
def s17():
    r=run_validation(FINAL_DB_FILE,VALIDATION_REPORT_FILE,REVIEW_QUEUE_FILE);print("[17]",r["summary"]);return r
def s18():
    r=run_baseline_guard(
        HERODATA_CANONICAL_FILE,BASELINE_MANIFEST_FILE,
        ABILITY_CATALOG_REPORT_FILE,TALENT_MODIFIER_REPORT_FILE,TALENT_RULE_MINER_REPORT_FILE,
        SPATIAL_MODEL_REPORT_FILE,AREA_REPORT_FILE,PROJECTILE_REPORT_FILE,
        COMBAT_REPORT_FILE,VALIDATION_REPORT_FILE,BASELINE_GUARD_REPORT_FILE
    )
    g=run_combat_semantic_gate(COMBAT_FACTS_FILE,HERODATA_CANONICAL_FILE,COMBAT_SEMANTIC_GATE_REPORT_FILE)
    print("[18]",r["summary"],"combat_gate",g["summary"]);return r
def s19():
    r=run_regressions(TALENT_MODIFIER_FACTS_FILE,SPATIAL_MODEL_FILE,SUBEFFECT_GRAPH_FILE,REGRESSION_REPORT_FILE);print("[19]",r["summary"]);return r
def s20():
    r=run_combat_semantics_audit(FINAL_DB_FILE,REVIEW_QUEUE_FILE,COMBAT_SEMANTICS_AUDIT_FILE,REVIEW_TRIAGE_FILE)
    print("[20]",r["summary"]);return r


def run(choice,mods,hd):
    funcs={
        "1":lambda:s1(mods),"2":lambda:s2(hd),"3":s3,"4":s4,"5":s5,"6":s6,"7":s7,
        "8":s8,"9":s9,"10":s10,"11":s11,"12":s12,"13":s13,"14":s14,"15":s15,
        "16":s16,"17":s17,"18":s18,"19":s19,"20":s20,
    }
    if choice=="21":
        for n in [str(i) for i in range(1,21)]:
            print("\\n=== STEP",n,"===")
            funcs[n]()
        print("\\nFULL REPRODUCIBLE BUILD COMPLETE:",FINAL_DB_FILE)
    elif choice in funcs:
        funcs[choice]()
    elif choice=="0":
        return
    else:
        print("Unknown step:",choice)


def main():
    print("BUILD VERSION:", BUILD_VERSION)
    ap=argparse.ArgumentParser()
    ap.add_argument("--mods-root",default=str(DEFAULT_RAW_XML_MODS))
    ap.add_argument("--herodata",default=str(DEFAULT_HERODATA_FILE))
    ap.add_argument("--step",choices=[str(i) for i in range(22)],default=None)
    args=ap.parse_args()

    mods=Path(args.mods_root).resolve()
    hd=Path(args.herodata).resolve()

    for d in (OUTPUT_DIR,FACTS_DIR,REPORTS_DIR):
        d.mkdir(parents=True,exist_ok=True)

    if args.step is not None:
        return run(args.step,mods,hd)

    while True:
        print(MENU)
        print("raw_xml/mods:",mods)
        print("HeroData:",hd)
        c=input("Choose step: ").strip()
        if c=="0":
            return
        try:
            run(c,mods,hd)
        except Exception as e:
            print("[ERROR]",repr(e))
        input("\\nEnter -> menu...")


if __name__=="__main__":
    main()
