from __future__ import annotations

from collections import Counter
from fact_utils import load, save, ability_records
from xml_semantics import ability_starts


PASSIVE_SENTINELS = {":PASSIVE:", "PASSIVE"}

STATE_WORDS = (
    "primed",
    "passive",
    "active state",
    "active form",
    "armed",
)

RECAST_WORDS = (
    "cancel",
    "recast",
    "reactivate",
    "return",
    "detonate",
    "release",
)

CONTROL_WORDS = (
    "select ",
    "select all",
    "select lost",
)


def classify_role(ability: dict, catalog_matches: list[dict]) -> tuple[str, str]:
    aid = str(ability.get("id") or "")
    name = str(ability.get("name") or "").lower()
    source = str(ability.get("source") or "")
    category = str(ability.get("category") or "")
    parent = ability.get("parent_link_id")

    if aid.upper() in PASSIVE_SENTINELS:
        if source == "HERODATA_SUBABILITY":
            return "STATE", "PASSIVE_SUBABILITY_SENTINEL"
        return "PASSIVE", "PASSIVE_SENTINEL"

    if any(w in name for w in CONTROL_WORDS):
        return "CONTROL", "CONTROL_NAME_PATTERN"

    if source == "HERODATA_SUBABILITY":
        if any(w in name for w in RECAST_WORDS):
            return "RECAST", "SUBABILITY_RECAST_PATTERN"

        if any(w in name for w in STATE_WORDS):
            return "STATE", "SUBABILITY_STATE_PATTERN"

    if catalog_matches:
        return "CASTABLE", "CATALOG_ABILITY_MATCH"

    # Hearth/Mount/Active are expected to be castable even when a direct
    # catalog match is missing because aliases can differ.
    if category in {"ACTIVE", "MOUNT", "HEARTH", "BASIC", "HEROIC"}:
        return "UNRESOLVED_CASTABLE", "CASTABLE_CATEGORY_NO_MATCH"

    if parent:
        return "STATE", "UNMATCHED_SUBABILITY"

    return "UNRESOLVED", "NO_ROLE_EVIDENCE"


def run_ability_catalog(catalog_path, herodata_path, output_path, report_path):
    catalog = load(catalog_path)
    hd = load(herodata_path)
    objects = catalog.get("objects", {})

    heroes = {}
    total = matched = unmatched = multi = 0
    type_counts = Counter()
    role_counts = Counter()
    review = []

    for hero, a in ability_records(hd):
        total += 1
        starts = ability_starts(objects, a)

        rows = []
        for d in starts:
            rows.append({
                "object_id": d.get("object_id"),
                "xml_type": d.get("xml_type"),
                "file": d.get("file"),
                "mod": d.get("mod"),
                "attrs": d.get("attrs", {}),
            })
            type_counts[str(d.get("xml_type"))] += 1

        if rows:
            matched += 1
        else:
            unmatched += 1

        if len(rows) > 1:
            multi += 1

        role, role_reason = classify_role(a, rows)
        role_counts[role] += 1

        row = {
            "record_key": a.get("record_key"),
            "ability_id": a.get("id"),
            "name": a.get("name"),
            "slot": a.get("slot"),
            "category": a.get("category"),
            "source": a.get("source"),
            "parent_link_id": a.get("parent_link_id"),
            "catalog_matches": rows,
            "match_count": len(rows),
            "ability_role": role,
            "role_reason": role_reason,
        }

        heroes.setdefault(hero, []).append(row)

        # Review only roles that should probably have a catalog ability.
        if not rows and role in {"CASTABLE", "UNRESOLVED_CASTABLE", "UNRESOLVED"}:
            review.append({
                "hero": hero,
                "record_key": a.get("record_key"),
                "ability_id": a.get("id"),
                "name": a.get("name"),
                "role": role,
                "reason": "NO_CATALOG_ABILITY_MATCH",
            })

    data = {
        "metadata": {"builder": "ability_catalog_parser_v2_role_classifier"},
        "heroes": heroes,
    }

    report = {
        "summary": {
            "abilities_total": total,
            "matched": matched,
            "unmatched": unmatched,
            "multiple_catalog_matches": multi,
        },
        "ability_roles": dict(role_counts),
        "top_xml_types": type_counts.most_common(50),
        "review": review,
    }

    save(output_path, data)
    save(report_path, report)
    return report
