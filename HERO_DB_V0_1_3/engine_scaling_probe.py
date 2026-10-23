#!/usr/bin/env python3
"""Pyhots Hero Unit DB v0.2.4 — build-matched Engine/GameData scaling application probe.

Evidence is deliberately separated:
  * local CHero/CBehavior/CAccumulator declarations from the frozen v0.2 DB/compact;
  * optional build-matched Galaxy scripts supplied by the operator;
  * a pinned external Galaxy snapshot used only as non-freezing corroboration.

The probe only promotes runtime arithmetic when a caller supplies both HeroesLib.galaxy
and GameLib.galaxy AND explicitly supplies equal compact/script build identities.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import pathlib
import re
from typing import Any, Iterable

SCHEMA_NAME = "PYHOTS_HERO_UNIT_DB_ENGINE_SCALING_APPLICATION_EVIDENCE"
SCHEMA_VERSION = "0.2.4"
PINNED_EXTERNAL_COMMIT = "b7587cca570f6a7ce27efa3336a6495d54a71720"
PINNED_REPOSITORY = "SC2Mapster/SC2GameData"
PINNED_HEROES_LIB = (
    "https://raw.githubusercontent.com/SC2Mapster/SC2GameData/"
    f"{PINNED_EXTERNAL_COMMIT}/mods/heroesdata.stormmod/base.stormdata/TriggerLibs/HeroesLib.galaxy"
)
PINNED_GAME_LIB = (
    "https://raw.githubusercontent.com/SC2Mapster/SC2GameData/"
    f"{PINNED_EXTERNAL_COMMIT}/mods/heroesdata.stormmod/base.stormdata/TriggerLibs/GameLib.galaxy"
)


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: pathlib.Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_text(path: pathlib.Path | None) -> str | None:
    if path is None:
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def sorted_counter(values: Iterable[Any]) -> dict[str, int]:
    c = collections.Counter("None" if v is None else str(v) for v in values)
    return dict(sorted(c.items(), key=lambda kv: (-kv[1], kv[0])))


def iter_scaling(db: dict[str, Any]):
    for hero_id, hero in sorted((db.get("heroes") or {}).items()):
        typed = ((hero.get("scaling_evidence") or {}).get("typed_level_scaling") or [])
        for rec in typed:
            x = dict(rec)
            x["hero_id"] = hero_id
            yield x


def iter_veterancy(db: dict[str, Any]):
    for hero_id, hero in sorted((db.get("heroes") or {}).items()):
        attached = ((hero.get("scaling_evidence") or {}).get("attached_veterancy_scaling") or [])
        for block in attached:
            b = dict(block)
            b["hero_id"] = hero_id
            yield b


def compact_objects(compact: dict[str, Any]):
    objects = compact.get("objects") or {}
    if not isinstance(objects, dict):
        return
    for object_id, versions in objects.items():
        if not isinstance(versions, list):
            continue
        for obj in versions:
            if isinstance(obj, dict):
                yield object_id, obj


def audit_accumulators(compact: dict[str, Any]) -> dict[str, Any]:
    accum = []
    fields_of_interest = {
        "ApplicationRule", "Scale", "Ratio", "MinAccumulation", "MaxAccumulation",
        "TokenId", "UnitSource"
    }
    for object_id, obj in compact_objects(compact):
        t = obj.get("xml_type")
        if not isinstance(t, str) or not t.startswith("CAccumulator"):
            continue
        attrs = obj.get("attrs") or {}
        descendants = obj.get("descendants") or []
        explicit: dict[str, str] = {}
        for k in fields_of_interest:
            if k in attrs:
                explicit[k] = attrs[k]
        for d in descendants:
            if not isinstance(d, dict):
                continue
            tag = d.get("tag") or d.get("field")
            dattrs = d.get("attrs") or {}
            if tag in fields_of_interest:
                if "value" in dattrs:
                    explicit[tag] = dattrs["value"]
                elif "Value" in dattrs:
                    explicit[tag] = dattrs["Value"]
        accum.append({"object_id": object_id, "xml_type": t, "explicit_fields": explicit})

    type_counts = sorted_counter(a["xml_type"] for a in accum)
    explicit_field_counts = collections.Counter()
    application_rule_counts = collections.Counter()
    for a in accum:
        for k in a["explicit_fields"]:
            explicit_field_counts[k] += 1
        rule = a["explicit_fields"].get("ApplicationRule")
        if rule is not None:
            application_rule_counts[rule] += 1
    return {
        "definition_count": len(accum),
        "type_counts": dict(sorted(type_counts.items())),
        "explicit_field_counts": dict(sorted(explicit_field_counts.items())),
        "application_rule_counts": dict(sorted(application_rule_counts.items())),
        "examples": accum[:20],
        "relationship_to_chero_level_scaling": "SEPARATE_FORMULA_SYSTEM_NOT_APPLICATION_PROOF",
    }


def audit_veterancy(db: dict[str, Any]) -> dict[str, Any]:
    blocks = list(iter_veterancy(db))
    behavior_counts = collections.Counter()
    slot_counts = collections.Counter()
    modifications = []
    heroes_30 = set()
    for block in blocks:
        bid = block.get("behavior_id")
        if bid:
            behavior_counts[bid] += 1
        slots = block.get("catalog_slot_count")
        if slots is not None:
            slot_counts[str(slots)] += 1
            if slots == 30:
                heroes_30.add(block.get("hero_id"))
        for m in block.get("modifications") or []:
            x = dict(m)
            x["hero_id"] = block.get("hero_id")
            x["behavior_id"] = bid
            modifications.append(x)

    excellent = [m for m in modifications if m.get("behavior_id") == "ExcellentMana"]
    guldan = [m for m in modifications if m.get("behavior_id") == "GuldanVeterancyMana"]
    return {
        "behavior_counts": dict(sorted(behavior_counts.items())),
        "catalog_slot_counts": dict(sorted(slot_counts.items())),
        "modification_count": len(modifications),
        "heroes_with_30_slot_veterancy": len(heroes_30),
        "excellent_mana_sample": excellent[:8],
        "guldan_veterancy_mana_sample": guldan[:4],
        "operation_semantics": "UNRESOLVED_ENGINE_VETERANCY_MODIFICATION_OPERATION",
        "cumulative_semantics": "UNRESOLVED_ENGINE_VETERANCY_CUMULATIVE_SEMANTICS",
        "runtime_level_source": "UNRESOLVED_ENGINE_LEVEL_SOURCE",
    }


def row_projection(rec: dict[str, Any]) -> dict[str, Any]:
    return {
        k: rec.get(k)
        for k in (
            "hero_id", "record_index", "ability", "catalog", "entry", "field", "value",
            "affected_by_overdrive", "affected_by_ability_power", "field_is_integer", "semantic_class",
            "target_resolution", "target_type"
        )
    }


def audit_local_scaling(db: dict[str, Any]) -> dict[str, Any]:
    rows = list(iter_scaling(db))
    modes = collections.Counter()
    special = []
    for r in rows:
        overdrive = r.get("affected_by_overdrive")
        value = r.get("value")
        catalog = r.get("catalog")
        if overdrive == "1" and value is not None:
            mode = "PERCENT_FLAG_DECLARED"
        elif overdrive == "1" and value is None:
            mode = "PERCENT_FLAG_DECLARED_VALUE_DEFAULT_DEPENDENT"
        elif overdrive != "1" and value is not None and catalog is not None:
            mode = "OVERDRIVE_FLAG_ABSENT_VALUE_DECLARED"
        else:
            mode = "DEFAULT_DEPENDENT_INCOMPLETE_RECORD"
        modes[mode] += 1
        if mode != "PERCENT_FLAG_DECLARED":
            x = row_projection(r)
            x["runtime_interpretation"] = "UNRESOLVED_PENDING_BUILD_MATCHED_GALAXY_APPLICATION"
            special.append(x)

    return {
        "record_count": len(rows),
        "affected_by_overdrive_counts": sorted_counter(r.get("affected_by_overdrive") for r in rows),
        "affected_by_ability_power_counts": sorted_counter(r.get("affected_by_ability_power") for r in rows),
        "field_is_integer_counts": sorted_counter(r.get("field_is_integer") for r in rows),
        "value_counts": sorted_counter(r.get("value") for r in rows),
        "semantic_class_counts": sorted_counter(r.get("semantic_class") for r in rows),
        "target_type_counts": sorted_counter(r.get("target_type") for r in rows),
        "declaration_mode_counts": dict(sorted(modes.items())),
        "non_percent_declared_records": special,
        "runtime_arithmetic": "UNRESOLVED_BUILD_MATCHED_GALAXY_APPLICATION_MISSING",
    }


def galaxy_semantic_anchors(heroes_text: str | None, game_text: str | None) -> dict[str, Any]:
    """Detect the exact application-path anchors used by the v0.2.4 promoter."""
    if heroes_text is None and game_text is None:
        return {
            "status": "MISSING_BUILD_MATCHED_GALAXY_SNAPSHOT",
            "heroes_lib": None,
            "game_lib": None,
            "build_match": "UNRESOLVED",
            "application_semantics": "UNRESOLVED_BUILD_MATCHED_GALAXY_APPLICATION_MISSING",
        }

    h = heroes_text or ""
    g = game_text or ""
    heroes_findings = {
        "loads_level_scaling_array_as_group_list": (
            "CatalogFieldValueCount" in h and "LevelScalingArray" in h
            and "lv_levelUpAbilitiesCount" in h and "lv_numberOfFieldChanges" in h
        ),
        "loads_affected_by_overdrive": "AffectedByOverdrive" in h and "lv_percentScaled" in h,
        "loads_value": "lv_valueAddedPerLevel" in h and ".Value" in h,
        "loads_catalog_entry_field": all(x in h for x in ("lv_catalog", "lv_entry", "lv_field")),
        "loads_field_is_integer": "lv_fieldIsInteger" in h and "FieldIsInteger" in h,
    }

    apply_start = g.find("void libGame_gf_ApplyLevelUpHeroStatsForHeroSingleLevel")
    apply_end = g.find("void libGame_gf_RemoveLevelUpHeroStatsForPlayer", apply_start + 1) if apply_start >= 0 else -1
    apply_body = g[apply_start:apply_end] if apply_start >= 0 and apply_end > apply_start else ""
    level_start = g.find("bool auto_libGame_gf_LevelUpHero_TriggerFunc")
    level_end = g.find("void libGame_gf_ApplyAllLevelUpHeroStatsForHero", level_start + 1) if level_start >= 0 else -1
    if level_end <= level_start:
        level_end = g.find("trigger auto_libGame_gf_SpawnHero_Trigger", level_start + 1) if level_start >= 0 else -1
    level_body = g[level_start:level_end] if level_start >= 0 and level_end > level_start else ""

    game_findings = {
        "percent_reads_current_field": (
            "CatalogFieldValueGetAsFixed" in apply_body and "lv_valueAddedPerLevel" in apply_body
            and "lv_percentScaled" in apply_body
        ),
        "uses_add_operation": "c_upgradeOperationAdd" in apply_body and (
            "CatalogFieldValueModifyFixed" in apply_body or "CatalogFieldValueModifyInt" in apply_body
        ),
        "flat_else_value": bool(re.search(r"lv_newValue\s*=\s*[^;]*lv_valueAddedPerLevel", apply_body)),
        "integer_and_fixed_paths": "CatalogFieldValueModifyInt" in apply_body and "CatalogFieldValueModifyFixed" in apply_body,
        "percent_value_gt_one_zero_guard": "lv_newValue = 0.0" in apply_body and "> 1.0" in apply_body,
        "vector_offset_y_flat_add_path": all(x in apply_body for x in ("InitialOffset", "ExpireOffset", "FinalOffset", "PeriodicOffsetArray[0]", "lv_yOffsetReal +=")),
        "unit_gain_level_trigger": "TriggerAddEventUnitGainLevel" in g,
        "hero_dummy_veterancy_gate": "HeroDummyVeterancy" in g and "EventUnitBehavior" in g,
        "levelup_calls_scaling": "libGame_gf_LevelUpHero" in g and "libGame_gf_ApplyLevelUpHeroStatsForHeroSingleLevel(lp_hero)" in g,
        "initial_spawn_calls_levelup": "SetTalentTreeHeroLevel(lp_player, 0)" in g and "libGame_gf_LevelUpHero(libGame_gv_players[lp_player].lv_heroUnit)" in g,
        "talent_mutations_read_current_player_catalog": (
            "c_talentModificationFlatModification" in g and "c_talentModificationMultiplyLevelModification" in g
            and "CatalogFieldValueGetAsFixed(lv_catalog, lv_entry, lv_field, lp_player)" in g
        ),
        "no_explicit_level_cap_in_scaling_application_function": bool(apply_body) and not any(
            token in apply_body for token in ("bALMaxLevel", "HeroMaxLevel", "<= 30", "< 30")
        ),
        "no_explicit_level_cap_around_scaling_call": bool(level_body) and not any(
            token in level_body for token in ("bALMaxLevel", "HeroMaxLevel", "<= 30", "< 30")
        ),
        "percent_scaling_enabled_default": "dEBUG_PercentScalingEnabled = true" in g,
    }
    hero_ok = all(heroes_findings.values())
    game_ok = all(game_findings.values())
    return {
        "status": "SCRIPT_SEMANTICS_MATCH" if hero_ok and game_ok else "SCRIPT_SEMANTICS_INCOMPLETE",
        "heroes_lib": heroes_findings,
        "game_lib": game_findings,
        "build_match": "UNASSESSED",
        "application_semantics": (
            "PROVEN_PERCENT_CURRENT_VALUE_ADD_OR_FLAT_ADD_PER_FLAG" if hero_ok and game_ok
            else "UNRESOLVED_SCRIPT_ANCHORS_INCOMPLETE"
        ),
    }

def probe(
    hero_db_path: pathlib.Path,
    compact_path: pathlib.Path,
    *,
    heroes_lib_path: pathlib.Path | None = None,
    game_lib_path: pathlib.Path | None = None,
    compact_build: str | None = None,
    script_build: str | None = None,
) -> dict[str, Any]:
    db = load_json(hero_db_path)
    compact = load_json(compact_path)
    local = audit_local_scaling(db)
    accum = audit_accumulators(compact)
    veterancy = audit_veterancy(db)

    script_evidence = galaxy_semantic_anchors(read_text(heroes_lib_path), read_text(game_lib_path))
    scripts_supplied = heroes_lib_path is not None and game_lib_path is not None
    explicit_match = bool(compact_build and script_build and compact_build == script_build)
    if scripts_supplied:
        script_evidence["heroes_lib_sha256"] = sha256(heroes_lib_path)
        script_evidence["game_lib_sha256"] = sha256(game_lib_path)
        script_evidence["compact_build"] = compact_build
        script_evidence["script_build"] = script_build
        script_evidence["build_match"] = "PROVEN_EXPLICIT_EQUAL_BUILD_ID" if explicit_match else "UNRESOLVED_OR_MISMATCH"
    if script_evidence["status"] == "SCRIPT_SEMANTICS_MATCH" and explicit_match:
        script_evidence["status"] = "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION"
        script_evidence["application_semantics"] = "PROVEN_PERCENT_CURRENT_VALUE_ADD_OR_FLAT_ADD_PER_FLAG"
    elif scripts_supplied:
        script_evidence["application_semantics"] = "UNRESOLVED_BUILD_MATCHED_GALAXY_APPLICATION"

    modes = local["declaration_mode_counts"]
    specials = local["non_percent_declared_records"]
    uther = [r for r in specials if r.get("hero_id") == "Uther"]
    rexxar = [r for r in specials if r.get("hero_id") == "Rexxar"]
    missing_value_percent = [r for r in specials if r.get("affected_by_overdrive") == "1" and r.get("value") is None]

    static_schema_ok = (db.get("schema") or {}).get("version") == "0.2"
    runtime_states = sorted_counter(
        r.get("runtime_arithmetic") for r in iter_scaling(db)
    )
    fail_closed = set(runtime_states.keys()) == {"UNRESOLVED_ENGINE_LEVEL_SCALING_APPLICATION"}

    build_matched = script_evidence["status"] == "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION"
    if build_matched:
        for r in specials:
            if r.get("affected_by_overdrive") == "1" and r.get("value") is None:
                r["runtime_interpretation"] = "PROVEN_PERCENT_OPERATION_VALUE_DEFAULT_UNRESOLVED"
            elif r.get("affected_by_overdrive") is None and r.get("value") is not None:
                r["runtime_interpretation"] = "UNRESOLVED_AFFECTED_BY_OVERDRIVE_DEFAULT"
            else:
                r["runtime_interpretation"] = "UNRESOLVED_INCOMPLETE_LEVEL_SCALING_DECLARATION"
        local["runtime_arithmetic"] = "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION_WITH_6_FAIL_CLOSED_DECLARATION_EXCEPTIONS"

    declaration_total = sum(modes.values())
    is_97771 = str(compact_build or "") == "2.55.17.97771"
    gates = {
        "STATIC_HERO_DB_V0_2_INPUT": "PASS" if static_schema_ok else "FAIL",
        "ALL_LEVEL_SCALING_RECORDS_AUDITED": "PASS" if local["record_count"] > 0 and declaration_total == local["record_count"] else "FAIL",
        "DECLARATION_EXCEPTIONS_RETAINED": "PASS" if len(specials) == (local["record_count"] - modes.get("PERCENT_FLAG_DECLARED", 0)) else "FAIL",
        "PERCENT_FLAG_VALUE_DEFAULT_DEPENDENCY_RETAINED": "PASS" if all(r.get("value") is None for r in missing_value_percent) else "FAIL",
        "UTHER_DEFAULT_DEPENDENT_ROWS_FAIL_CLOSED": "PASS" if (not is_97771 or (len(uther) == 2 and all(r.get("value") is None for r in uther))) else "FAIL",
        "REXXAR_UNFLAGGED_ROWS_NOT_FORCED_PERCENT": "PASS" if (not is_97771 or (len(rexxar) == 2 and all(r.get("value") == "0.500000" for r in rexxar))) else "FAIL",
        "BUILD_97771_DECLARATION_REGRESSION": "PASS" if (not is_97771 or (local["record_count"] == 1641 and modes.get("PERCENT_FLAG_DECLARED") == 1635 and len(specials) == 6 and len(missing_value_percent) == 2)) else "FAIL",
        "ACCUMULATOR_APPLICATION_SEPARATED": "PASS" if accum["relationship_to_chero_level_scaling"] == "SEPARATE_FORMULA_SYSTEM_NOT_APPLICATION_PROOF" else "FAIL",
        "BUILD_MATCHED_RUNTIME_APPLICATION": "PASS" if (fail_closed or build_matched) else "FAIL",
    }

    if build_matched:
        local_runtime = "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION"
        external_status = "CORROBORATION_ONLY_NOT_NEEDED_FOR_FREEZE"
    else:
        local_runtime = "UNRESOLVED_BUILD_MATCHED_GALAXY_SNAPSHOT_MISSING" if not scripts_supplied else "UNRESOLVED_BUILD_MATCH"
        external_status = "PROVEN_FOR_PINNED_EXTERNAL_SCRIPT_SNAPSHOT"

    return {
        "schema": {"name": SCHEMA_NAME, "version": SCHEMA_VERSION},
        "principle": "Replay proves observed runtime events; GameData proves declarations; build-matched Galaxy script is required to freeze CHero level-scaling application arithmetic.",
        "inputs": {
            "hero_unit_db": {
                "path": str(hero_db_path), "sha256": sha256(hero_db_path), "schema": db.get("schema")
            },
            "hero_global_compact": {
                "path": str(compact_path), "sha256": sha256(compact_path), "metadata": compact.get("metadata")
            },
        },
        "local_catalog_evidence": local,
        "accumulator_evidence": accum,
        "attached_veterancy_evidence": veterancy,
        "build_matched_galaxy_evidence": script_evidence,
        "external_script_corroboration": {
            "classification": "EXTERNAL_PINNED_SCRIPT_CORROBORATION_NON_FREEZING",
            "repository": PINNED_REPOSITORY,
            "commit": PINNED_EXTERNAL_COMMIT,
            "heroes_lib": {"url": PINNED_HEROES_LIB, "path": "mods/heroesdata.stormmod/base.stormdata/TriggerLibs/HeroesLib.galaxy", "relevant_lines": [688, 832]},
            "game_lib": {"url": PINNED_GAME_LIB, "path": "mods/heroesdata.stormmod/base.stormdata/TriggerLibs/GameLib.galaxy", "relevant_lines": [5398, 5470, 9515, 9568, 17675, 17715]},
            "findings": {
                "loader": "HeroesLib reads LevelScalingArray.Modifications.AffectedByOverdrive as a boolean and Value as fixed into per-hero script memory.",
                "percent_path": "GameLib computes current catalog field value * valueAddedPerLevel when PercentScalingEnabled and percentScaled are true.",
                "flat_path": "Otherwise GameLib uses valueAddedPerLevel directly.",
                "operation": "GameLib applies the computed delta with c_upgradeOperationAdd using CatalogFieldValueModifyFixed/Int.",
                "level_event": "GameLib Level Up trigger listens to UnitGainLevel for HeroDummyVeterancy and calls LevelUpHero; the level path calls ApplyLevelUpHeroStatsForHeroSingleLevel.",
            },
            "build_match_to_local_compact": ("PROVEN_SEPARATELY_BY_BUILD_MATCHED_LOCAL_GALAXY" if build_matched else "UNRESOLVED_LOCAL_COMPACT_BUILD_PROVENANCE_MISSING"),
            "freeze_permission": False,
        },
        "conclusions": {
            "local_chero_declaration_semantics": "PROVEN_FIELDS_AND_FLAGS",
            "external_snapshot_application_semantics": external_status,
            "local_runtime_arithmetic": local_runtime,
            "level_scaling_array_semantics": (
                "PROVEN_GROUP_LIST_APPLIED_ON_EACH_LEVELUP_INVOCATION" if build_matched
                else "UNRESOLVED_BUILD_MATCHED_GROUP_APPLICATION"
            ),
            "percent_operation": (
                "NEXT_VALUE = CURRENT_PLAYER_CATALOG_VALUE + CURRENT_PLAYER_CATALOG_VALUE*VALUE" if build_matched
                else "UNRESOLVED_BUILD_MATCH"
            ),
            "non_percent_operation": (
                "NEXT_VALUE = CURRENT_PLAYER_CATALOG_VALUE + VALUE" if build_matched
                else "UNRESOLVED_BUILD_MATCH"
            ),
            "percent_value_gt_one_guard": "DELTA_FORCED_TO_ZERO" if build_matched else "UNRESOLVED_BUILD_MATCH",
            "integer_storage_path": "FIXED_TO_INT_THEN_ADD_WHEN_FIELD_IS_INTEGER" if build_matched else "UNRESOLVED_BUILD_MATCH",
            "field_is_integer_declaration_counts": local.get("field_is_integer_counts"),
            "field_is_integer_default_semantics": "NOT_ASSUMED_WHEN_DECLARATION_IS_ABSENT",
            "closed_form_percent_projection": "BASE*(1+VALUE)^N_LEVELUP_APPLICATIONS",
            "closed_form_status": (
                "DERIVED_ONLY_IF_SAME_PLAYER_CATALOG_FIELD_HAS_NO_INTERMEDIATE_MUTATION" if build_matched
                else "NON_FREEZING_PROJECTION_UNTIL_BUILD_MATCH_PROVEN"
            ),
            "display_level_to_application_count": (
                "INITIAL_SPAWN_EXPLICITLY_CALLS_LEVELUP_ONCE; SUBSEQUENT_HERODUMMYVETERANCY_GAIN_LEVEL_EVENTS_CALL_LEVELUP" if build_matched
                else "UNRESOLVED_BUILD_MATCHED_LEVEL_EVENT_MAPPING"
            ),
            "post_30_combat_scaling": (
                "NO_EXPLICIT_LEVEL_CAP_IN_GALAXY_APPLICATION_PATH; LEVEL_EVENT_REACHABILITY_ABOVE_30_UNRESOLVED_FOR_BUILD_97771" if build_matched
                else "UNRESOLVED_BUILD_MATCHED_SCRIPT_APPLICATION_BEYOND_30"
            ),
            "veterancy_operation": "UNRESOLVED_ENGINE_VETERANCY_MODIFICATION_OPERATION",
            "talent_vs_level_scaling_order": (
                "LEVEL_SCALING_READS_CURRENT_PLAYER_CATALOG_VALUE; TALENT_MUTATIONS_ALSO_READ/WRITE_PLAYER_CATALOG_STATE" if build_matched
                else "UNRESOLVED_BUILD_MATCHED_GALAXY_ORDER"
            ),
        },
        "integrity": {"gates": gates, "overall": "PASS" if all(v == "PASS" for v in gates.values()) else "FAIL"},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hero-db", "--hero-unit-db", dest="hero_db", required=True)
    ap.add_argument("--compact", "--hero-global-compact", dest="compact", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--heroes-lib")
    ap.add_argument("--game-lib")
    ap.add_argument("--compact-build")
    ap.add_argument("--script-build")
    ns = ap.parse_args()
    result = probe(
        pathlib.Path(ns.hero_db), pathlib.Path(ns.compact),
        heroes_lib_path=pathlib.Path(ns.heroes_lib) if ns.heroes_lib else None,
        game_lib_path=pathlib.Path(ns.game_lib) if ns.game_lib else None,
        compact_build=ns.compact_build, script_build=ns.script_build,
    )
    out = pathlib.Path(ns.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"overall": result["integrity"]["overall"], "output": str(out)}, sort_keys=True))


if __name__ == "__main__":
    main()
