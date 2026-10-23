from __future__ import annotations

from collections import Counter
from fact_utils import load, save


def run_validation(final_path, validation_path, review_path):
    db = load(final_path)

    reviews = []
    summary = Counter()
    priorities = Counter()
    talent_classes = Counter()
    spatial_status = Counter()

    for hero, h in db.get("heroes", {}).items():
        ids = Counter(a.get("id") for a in h.get("abilities", []) if a.get("id"))
        for aid, c in ids.items():
            if c > 1:
                summary["duplicate_canonical_ids"] += 1
                priorities["INFO_ONLY"] += 1
                reviews.append({
                    "priority": "INFO_ONLY",
                    "hero": hero,
                    "ability_id": aid,
                    "reason": "DUPLICATE_CANONICAL_ID",
                    "count": c,
                })

        for talent in h.get("talent_facts", []):
            summary["talents_total"] += 1
            m = talent.get("modifier_facts") or {}
            cls = m.get("final_classification", "UNRESOLVED")
            comp = m.get("completeness", "UNRESOLVED")
            reason = m.get("completeness_reason", "UNKNOWN")
            priority = m.get("review_priority", "INFO_ONLY")

            talent_classes[cls] += 1
            summary[f"talent_{comp.lower()}"] += 1

            cov = m.get("numeric_coverage") or {}
            if cov.get("tokens_total", 0) > 0 and cov.get("coverage_ratio", 0) < 0.5:
                summary["talent_low_numeric_coverage"] += 1

            if m.get("semantic_corrections"):
                summary["talent_semantic_corrected"] += 1
            if m.get("internal_cooldown"):
                summary["talent_with_internal_cooldown"] += 1

            if comp != "COMPLETE":
                priorities[priority] += 1
                reviews.append({
                    "priority": priority,
                    "hero": hero,
                    "talent_id": talent.get("talent_id"),
                    "name": talent.get("name"),
                    "reason": reason,
                    "missing_numbers": (cov.get("missing") or [])[:12],
                })

        for ability in h.get("abilities", []):
            summary["abilities_total"] += 1
            if not ability.get("id"):
                summary["missing_id"] += 1
                priorities["ERROR"] += 1
                reviews.append({
                    "priority": "ERROR",
                    "hero": hero,
                    "record_key": ability.get("record_key"),
                    "reason": "MISSING_ID",
                })
                continue

            spatial = ability.get("spatial") or {}
            subeffects = ability.get("subeffects") or {}
            if not subeffects:
                summary["missing_subeffect_graph"] += 1
                priorities["ERROR"] += 1
                reviews.append({"priority":"ERROR","hero":hero,"ability_id":ability.get("id"),"reason":"MISSING_SUBEFFECT_GRAPH"})
            status = str(spatial.get("spatial_status") or "SPATIAL_UNRESOLVED")
            spatial_status[status] += 1

            if status == "NON_SPATIAL":
                summary["non_spatial"] += 1
            elif status == "SPATIAL_UNRESOLVED":
                summary["spatial_unresolved"] += 1
                priorities["NEEDS_RULE"] += 1
                reviews.append({
                    "priority": "NEEDS_RULE",
                    "hero": hero,
                    "record_key": ability.get("record_key"),
                    "ability_id": ability.get("id"),
                    "reason": spatial.get("status_reason") or "SPATIAL_UNRESOLVED",
                })

    summary["review_queue"] = len(reviews)

    report = {
        "summary": dict(summary),
        "review_priorities": dict(priorities),
        "talent_classes": dict(talent_classes),
        "spatial_status": dict(spatial_status),
    }

    save(validation_path, report)
    save(review_path, {
        "metadata": {"builder": "validator_v3_9"},
        "entries": reviews,
    })
    return report
