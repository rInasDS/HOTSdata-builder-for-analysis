from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BASE_DIR.parent

DEFAULT_RAW_XML_MODS = (PROJECT_ROOT / "raw_xml" / "mods").resolve()

INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
FACTS_DIR = BASE_DIR / "facts"
REPORTS_DIR = BASE_DIR / "reports"

DEFAULT_HERODATA_FILE = INPUT_DIR / "herodata_97771_enus.json"

CATALOG_INDEX_FILE = OUTPUT_DIR / "storm_catalog_index.json"
CATALOG_REPORT_FILE = REPORTS_DIR / "storm_catalog_index_report.json"
EFFECTIVE_CATALOG_FILE = OUTPUT_DIR / "effective_catalog.json"
EFFECTIVE_CATALOG_REPORT_FILE = REPORTS_DIR / "effective_catalog_report.json"
REPLAY_EFFECTIVE_CATALOG_FILE = OUTPUT_DIR / "replay_effective_catalog.json"
REPLAY_EFFECTIVE_CATALOG_REPORT_FILE = REPORTS_DIR / "replay_effective_catalog_report.json"
HERODATA_DUPLICATES_REPORT_FILE = REPORTS_DIR / "herodata_duplicate_ability_ids.json"
TYPED_REFERENCE_GRAPH_FILE = OUTPUT_DIR / "typed_reference_graph.json"
TYPED_REFERENCE_REPORT_FILE = REPORTS_DIR / "typed_reference_report.json"

HERODATA_CANONICAL_FILE = OUTPUT_DIR / "herodata_canonical.json"
HERODATA_IMPORT_REPORT_FILE = REPORTS_DIR / "herodata_import_report.json"

ABILITY_CATALOG_FACTS_FILE = FACTS_DIR / "ability_catalog_facts.json"
ABILITY_CATALOG_REPORT_FILE = REPORTS_DIR / "ability_catalog_report.json"

TALENT_FACTS_FILE = FACTS_DIR / "talent_facts.json"
TALENT_REPORT_FILE = REPORTS_DIR / "talent_report.json"

TARGETING_FACTS_FILE = FACTS_DIR / "targeting_facts.json"
TARGETING_REPORT_FILE = REPORTS_DIR / "targeting_report.json"

PROJECTILE_FACTS_FILE = FACTS_DIR / "projectile_facts.json"
PROJECTILE_REPORT_FILE = REPORTS_DIR / "projectile_report.json"

AREA_FACTS_FILE = FACTS_DIR / "area_geometry_facts.json"
AREA_REPORT_FILE = REPORTS_DIR / "area_geometry_report.json"

COMBAT_FACTS_FILE = FACTS_DIR / "combat_facts.json"
COMBAT_REPORT_FILE = REPORTS_DIR / "combat_report.json"

SPATIAL_MODEL_FILE = OUTPUT_DIR / "spatial_models.json"
SPATIAL_MODEL_REPORT_FILE = REPORTS_DIR / "spatial_model_report.json"

SEMANTIC_BUNDLE_FILE = OUTPUT_DIR / "semantic_bundle.json"

FINAL_DB_FILE = OUTPUT_DIR / "abilities_complete.json"
MERGE_REPORT_FILE = REPORTS_DIR / "merge_report.json"

VALIDATION_REPORT_FILE = REPORTS_DIR / "validation_report.json"
REVIEW_QUEUE_FILE = OUTPUT_DIR / "review_queue.json"

SEMANTIC_FACTS_FILE = FACTS_DIR / "semantic_ability_facts.json"
SEMANTIC_REPORT_FILE = REPORTS_DIR / "semantic_ability_report.json"


TALENT_MODIFIER_FACTS_FILE = FACTS_DIR / "talent_modifier_facts.json"
TALENT_MODIFIER_REPORT_FILE = REPORTS_DIR / "talent_modifier_report.json"

TALENT_MODIFIERS_FILE = OUTPUT_DIR / "talent_modifiers.json"
TALENT_RUNTIME_RULES_FILE = OUTPUT_DIR / "talent_runtime_rules.json"
TALENT_DECODER_REPORT_FILE = REPORTS_DIR / "talent_decoder_report.json"
TALENT_RULE_MINER_REPORT_FILE = REPORTS_DIR / "talent_rule_miner_report.json"
REGRESSION_REPORT_FILE = REPORTS_DIR / "regression_report.json"


SUBEFFECT_GRAPH_FILE = OUTPUT_DIR / "ability_subeffects.json"
SUBEFFECT_GRAPH_REPORT_FILE = REPORTS_DIR / "ability_subeffects_report.json"
BASELINE_MANIFEST_FILE = BASE_DIR / "baseline_v38_manifest.json"
BASELINE_GUARD_REPORT_FILE = REPORTS_DIR / "baseline_guard_report.json"

BUILD_VERSION = "3.12.10"

COMBAT_SEMANTICS_AUDIT_FILE = REPORTS_DIR / "combat_semantics_audit.json"
COMBAT_SEMANTIC_GATE_REPORT_FILE = REPORTS_DIR / "combat_semantic_gate_report.json"
REVIEW_TRIAGE_FILE = REPORTS_DIR / "review_triage.json"
