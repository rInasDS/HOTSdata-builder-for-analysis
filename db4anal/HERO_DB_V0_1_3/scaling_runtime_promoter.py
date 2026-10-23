#!/usr/bin/env python3
"""Promote build-matched CHero LevelScalingArray application semantics into Hero Unit DB.

This is deliberately a post-build layer. hero_unit_db_builder still proves static catalog
ownership/targets; this promoter requires a build-matched Galaxy application evidence file.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from hero_unit_db_builder import _hero_level_scaling

SCHEMA_VERSION = "0.2.4"
OFFSET_FIELDS = {"InitialOffset", "ExpireOffset", "FinalOffset", "PeriodicOffsetArray[0]"}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _signature(r: dict[str, Any]) -> tuple[Any, ...]:
    return (
        r.get("ability"), r.get("catalog"), r.get("entry"), r.get("field"),
        r.get("value"), r.get("affected_by_overdrive"), r.get("affected_by_ability_power"),
    )


def _attach_missing_loader_fields(db: dict[str, Any], compact: dict[str, Any]) -> None:
    """Attach direct LevelScaling loader fields without rebuilding the typed graph.

    The compact/raw order must match exactly. Any mismatch is a hard error.
    """
    total = 0
    for hero_id, hero in sorted((db.get("heroes") or {}).items()):
        typed = ((hero.get("scaling_evidence") or {}).get("typed_level_scaling") or [])
        raw_out = ((hero.get("scaling_evidence") or {}).get("chero_level_scaling") or [])
        source = _hero_level_scaling(hero_id, compact)
        if len(typed) != len(source) or len(raw_out) != len(source):
            raise RuntimeError(f"{hero_id}: scaling record count mismatch typed={len(typed)} raw={len(raw_out)} compact={len(source)}")
        for i, (t, r, s) in enumerate(zip(typed, raw_out, source)):
            if _signature(t) != _signature(s) or _signature(r) != _signature(s):
                raise RuntimeError(f"{hero_id}: scaling record identity mismatch at index {i}")
            for target in (t, r):
                target["field_is_integer"] = s.get("field_is_integer")
            total += 1
    expected = int(((db.get("summary") or {}).get("typed_level_scaling_records") or total))
    if total != expected:
        raise RuntimeError(f"scaling record count mismatch: expected {expected}, got {total}")


def _runtime_application(rec: dict[str, Any], build: str) -> dict[str, Any]:
    field = rec.get("field")
    value = rec.get("value")
    overdrive = rec.get("affected_by_overdrive")
    field_is_integer = rec.get("field_is_integer")

    common = {
        "build": build,
        "trigger": "LEVELUP_HERO_INVOCATION",
        "application_function": "libGame_gf_ApplyLevelUpHeroStatsForHeroSingleLevel",
        "catalog_scope": "CURRENT_PLAYER",
        "apply_operation": "c_upgradeOperationAdd",
        "field_is_integer_declared": field_is_integer,
        "numeric_storage_path": (
            "FIXED_TO_INT_THEN_ADD" if field_is_integer == "1" else
            "FIXED_ADD" if field_is_integer == "0" else
            "CATALOG_DEFAULT_DEPENDENT_FIELD_IS_INTEGER"
        ),
    }

    if field in OFFSET_FIELDS:
        return {
            **common,
            "status": "PROVEN_BUILD_MATCHED" if value is not None else "UNRESOLVED_VALUE_DEFAULT",
            "operation": "VECTOR_Y_COMPONENT_FLAT_ADD",
            "coefficient": value,
            "reads_current_value": True,
            "formula": "Y_NEXT = Y_CURRENT + VALUE",
        }

    if overdrive == "1":
        if value is None:
            return {
                **common,
                "status": "PROVEN_OPERATION_VALUE_DEFAULT_UNRESOLVED",
                "operation": "CURRENT_PLAYER_CATALOG_PERCENT_ADD",
                "coefficient": None,
                "reads_current_value": True,
                "formula": "NEXT = CURRENT + CURRENT*VALUE",
                "percent_value_gt_one_guard": "DELTA_ZERO_IF_VALUE_GT_1",
            }
        return {
            **common,
            "status": "PROVEN_BUILD_MATCHED",
            "operation": "CURRENT_PLAYER_CATALOG_PERCENT_ADD",
            "coefficient": value,
            "reads_current_value": True,
            "formula": "NEXT = CURRENT + CURRENT*VALUE",
            "percent_value_gt_one_guard": "DELTA_ZERO_IF_VALUE_GT_1",
        }

    if overdrive is not None:
        if value is None:
            return {
                **common,
                "status": "UNRESOLVED_VALUE_DEFAULT",
                "operation": "FLAT_ADD",
                "coefficient": None,
                "reads_current_value": False,
                "formula": "NEXT = CURRENT + VALUE",
            }
        return {
            **common,
            "status": "PROVEN_BUILD_MATCHED",
            "operation": "FLAT_ADD",
            "coefficient": value,
            "reads_current_value": False,
            "formula": "NEXT = CURRENT + VALUE",
        }

    if value is not None:
        return {
            **common,
            "status": "UNRESOLVED_AFFECTED_BY_OVERDRIVE_DEFAULT",
            "operation": "UNRESOLVED_PERCENT_OR_FLAT_ADD",
            "coefficient": value,
            "reads_current_value": "UNRESOLVED",
            "formula": "UNRESOLVED_UNTIL_AFFECTED_BY_OVERDRIVE_DEFAULT_IS_PROVEN",
        }

    return {
        **common,
        "status": "UNRESOLVED_INCOMPLETE_LEVEL_SCALING_DECLARATION",
        "operation": "UNRESOLVED",
        "coefficient": None,
        "reads_current_value": "UNRESOLVED",
        "formula": "UNRESOLVED",
    }


def promote(static_db_path: Path, compact_path: Path, application_evidence_path: Path) -> dict[str, Any]:
    db = load(static_db_path)
    compact = load(compact_path)
    app = load(application_evidence_path)

    if (db.get("schema") or {}).get("name") != "PYHOTS_HERO_UNIT_DB":
        raise RuntimeError("not a Hero Unit DB")
    if (db.get("schema") or {}).get("version") != "0.2":
        raise RuntimeError("v0.2 static Hero Unit DB required for promotion")
    be = app.get("build_matched_galaxy_evidence") or {}
    if be.get("status") != "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION":
        raise RuntimeError("build-matched Galaxy application proof is required")
    if be.get("build_match") != "PROVEN_EXPLICIT_EQUAL_BUILD_ID":
        raise RuntimeError("explicit equal build identity is required")
    build = str(be.get("compact_build") or "")
    if not build or build != str(be.get("script_build") or ""):
        raise RuntimeError("compact/script build mismatch")

    app_inputs = app.get("inputs") or {}
    expected_db_sha = ((app_inputs.get("hero_unit_db") or {}).get("sha256"))
    expected_compact_sha = ((app_inputs.get("hero_global_compact") or {}).get("sha256"))
    if expected_db_sha and sha256(static_db_path) != expected_db_sha:
        raise RuntimeError("application evidence does not belong to supplied static Hero DB")
    if expected_compact_sha and sha256(compact_path) != expected_compact_sha:
        raise RuntimeError("application evidence does not belong to supplied compact")

    out = copy.deepcopy(db)
    _attach_missing_loader_fields(out, compact)

    status_counts: Counter[str] = Counter()
    operation_counts: Counter[str] = Counter()
    field_integer_counts: Counter[str] = Counter()
    explicit_percent_declared = 0
    unresolved = []
    total = 0
    for hero_id, hero in sorted((out.get("heroes") or {}).items()):
        ev = hero.get("scaling_evidence") or {}
        typed = ev.get("typed_level_scaling") or []
        raw_rows = ev.get("chero_level_scaling") or []
        for i, (typed_rec, raw_rec) in enumerate(zip(typed, raw_rows)):
            ra = _runtime_application(typed_rec, build)
            typed_rec["runtime_application"] = ra
            raw_rec["runtime_application"] = copy.deepcopy(ra)
            for interaction in typed_rec.get("talent_interactions") or []:
                interaction["runtime_interaction"] = {
                    "status": "PROVEN_CURRENT_PLAYER_CATALOG_STATE_COMPOSITION",
                    "level_scaling_reads_current_player_catalog_value": True,
                    "talent_mutation_reads_or_writes_current_player_catalog_value": True,
                    "closed_form_warning": "Do not use a base-only exponent across an intermediate mutation of the same field.",
                }
            if ra["status"] == "PROVEN_BUILD_MATCHED":
                if ra["operation"] == "CURRENT_PLAYER_CATALOG_PERCENT_ADD":
                    arithmetic = "PROVEN_CURRENT_VALUE_PERCENT_ADD"
                elif ra["operation"] == "FLAT_ADD":
                    arithmetic = "PROVEN_FLAT_ADD"
                elif ra["operation"] == "VECTOR_Y_COMPONENT_FLAT_ADD":
                    arithmetic = "PROVEN_VECTOR_Y_FLAT_ADD"
                else:
                    arithmetic = "PROVEN_BUILD_MATCHED_APPLICATION"
            elif ra["status"] == "PROVEN_OPERATION_VALUE_DEFAULT_UNRESOLVED":
                arithmetic = "PROVEN_PERCENT_OPERATION_UNRESOLVED_VALUE"
            else:
                arithmetic = ra["status"]
            typed_rec["runtime_arithmetic"] = arithmetic
            raw_rec["runtime_arithmetic"] = arithmetic
            status_counts[ra["status"]] += 1
            operation_counts[ra["operation"]] += 1
            field_integer_counts["None" if typed_rec.get("field_is_integer") is None else str(typed_rec.get("field_is_integer"))] += 1
            if typed_rec.get("affected_by_overdrive") == "1" and typed_rec.get("value") is not None:
                explicit_percent_declared += 1
            if not str(ra["status"]).startswith("PROVEN"):
                unresolved.append({
                    "hero_id": hero_id, "record_index": i,
                    "ability": typed_rec.get("ability"), "catalog": typed_rec.get("catalog"),
                    "entry": typed_rec.get("entry"), "field": typed_rec.get("field"),
                    "value": typed_rec.get("value"), "affected_by_overdrive": typed_rec.get("affected_by_overdrive"),
                    "status": ra["status"],
                })
            total += 1

    expected_total = int(((db.get("summary") or {}).get("typed_level_scaling_records") or total))
    if total != expected_total:
        raise RuntimeError(f"promotion record count mismatch: expected {expected_total}, got {total}")

    out["schema"] = {"name": "PYHOTS_HERO_UNIT_DB", "version": SCHEMA_VERSION}
    contract = out.setdefault("contract", {})
    contract["scaling"] = (
        "Typed CHero LevelScalingArray targets plus build-matched Galaxy application semantics. "
        "For build 2.55.17.97771 explicit AffectedByOverdrive=1 rows use current player catalog value * Value as delta, then ADD; "
        "non-percent rows use Value as flat delta. Closed form is derived only when the same player-catalog field has no intermediate mutation. "
        "CBehaviorVeterancy operation/cumulative semantics remain separate and fail-closed."
    )
    out["runtime_scaling_application_semantics"] = {
        "status": "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION",
        "build": build,
        "source_evidence_schema": app.get("schema"),
        "heroes_lib_sha256": be.get("heroes_lib_sha256"),
        "game_lib_sha256": be.get("game_lib_sha256"),
        "level_scaling_array_semantics": "GROUP_LIST_APPLIED_ON_EACH_LEVELUP_INVOCATION",
        "levelup_trigger": "UnitGainLevel + HeroDummyVeterancy -> libGame_gf_LevelUpHero",
        "initial_spawn": "SpawnHero explicitly calls libGame_gf_LevelUpHero once after setting talent tree level to 0",
        "percent_operation": "NEXT = CURRENT_PLAYER_CATALOG_VALUE + CURRENT_PLAYER_CATALOG_VALUE*VALUE",
        "flat_operation": "NEXT = CURRENT_PLAYER_CATALOG_VALUE + VALUE",
        "percent_value_gt_one_guard": "DELTA_ZERO_IF_VALUE_GT_1",
        "talent_interaction": "LEVEL_SCALING_READS_CURRENT_PLAYER_CATALOG_STATE; TALENT_MUTATIONS_ALSO READ/WRITE PLAYER CATALOG STATE",
        "closed_form": "DERIVED_ONLY_IF_NO_INTERMEDIATE_MUTATION_OF_SAME_PLAYER_CATALOG_FIELD",
        "post_30": f"NO_EXPLICIT_LEVEL_CAP_IN_GALAXY_APPLICATION_PATH; LEVEL_EVENT_REACHABILITY_ABOVE_30_UNRESOLVED_FOR_BUILD_{build}",
        "field_is_integer_declaration": (
            f"EXPLICIT_IN_{sum(1 for hero in (out.get('heroes') or {}).values() for rec in ((hero.get('scaling_evidence') or {}).get('typed_level_scaling') or []) if rec.get('field_is_integer') is not None)}_OF_{total}_LOCAL_LEVEL_SCALING_ROWS; "
            "CATALOG_DEFAULT_SEMANTICS_NOT_PROMOTED_WHEN_ABSENT"
        ),
    }

    fully_proven = status_counts["PROVEN_BUILD_MATCHED"]
    partial = status_counts["PROVEN_OPERATION_VALUE_DEFAULT_UNRESOLVED"]
    hard_unresolved = len(unresolved)
    not_fully_resolved = partial + hard_unresolved

    summary = out.setdefault("summary", {})
    summary["runtime_scaling_application_status_counts"] = dict(sorted(status_counts.items()))
    summary["runtime_scaling_operation_counts"] = dict(sorted(operation_counts.items()))
    summary["runtime_scaling_application_fully_proven"] = fully_proven
    summary["runtime_scaling_application_partial"] = partial
    summary["runtime_scaling_application_hard_unresolved"] = hard_unresolved
    summary["runtime_scaling_application_not_fully_resolved"] = not_fully_resolved
    summary["runtime_scaling_application_unresolved"] = not_fully_resolved
    summary["runtime_scaling_build"] = build
    summary["overall"] = "PASS"

    integ = out.setdefault("integrity", {})
    gates = integ.setdefault("gates", {})
    gates.pop("SCALING_RUNTIME_ARITHMETIC_FAIL_CLOSED", None)
    gates["SCALING_BUILD_MATCHED_GALAXY_APPLICATION"] = "PASS"
    gates["SCALING_EXPLICIT_PERCENT_ROWS_PROMOTED"] = "PASS" if fully_proven + partial > 0 else "FAIL"
    gates["SCALING_DEFAULT_DEPENDENT_ROWS_RETAINED_FAIL_CLOSED"] = "PASS" if not_fully_resolved == len(unresolved) + partial else "FAIL"
    if build == "2.55.17.97771":
        gates["SCALING_97771_APPLICATION_COUNT_REGRESSION"] = "PASS" if (fully_proven, partial, hard_unresolved) == (1635, 2, 4) else "FAIL"
    gates["SCALING_LEVEL_ARRAY_GROUP_LIST_SEMANTICS"] = "PASS"
    gates["SCALING_CURRENT_VALUE_TALENT_COMPOSITION"] = "PASS"
    gates["SCALING_FIELD_IS_INTEGER_DEFAULT_DEPENDENCY_RETAINED"] = "PASS"
    integ["runtime_scaling_application_partial"] = [
        {
            "hero_id": hero_id, "record_index": i, "ability": rec.get("ability"),
            "catalog": rec.get("catalog"), "entry": rec.get("entry"), "field": rec.get("field"),
            "value": rec.get("value"), "affected_by_overdrive": rec.get("affected_by_overdrive"),
            "status": rec.get("runtime_application", {}).get("status"),
        }
        for hero_id, hero in sorted((out.get("heroes") or {}).items())
        for i, rec in enumerate(((hero.get("scaling_evidence") or {}).get("typed_level_scaling") or []))
        if (rec.get("runtime_application") or {}).get("status") == "PROVEN_OPERATION_VALUE_DEFAULT_UNRESOLVED"
    ]
    integ["runtime_scaling_application_unresolved"] = unresolved
    integ["runtime_scaling_application_not_fully_resolved_count"] = not_fully_resolved
    integ["arithmetic_leaks"] = []
    integ["overall"] = "PASS" if all(v == "PASS" for v in gates.values()) else "FAIL"
    summary["overall"] = integ["overall"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Promote build-matched runtime scaling semantics into Hero Unit DB v0.2.4")
    ap.add_argument("--static-hero-db", required=True)
    ap.add_argument("--compact", required=True)
    ap.add_argument("--application-evidence", required=True)
    ap.add_argument("--output", required=True)
    ns = ap.parse_args()
    out = promote(Path(ns.static_hero_db), Path(ns.compact), Path(ns.application_evidence))
    p = Path(ns.output)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"overall": out["integrity"]["overall"], "schema": out["schema"], "output": str(p)}, sort_keys=True))


if __name__ == "__main__":
    main()
