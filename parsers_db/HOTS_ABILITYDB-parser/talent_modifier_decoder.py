from __future__ import annotations

from collections import Counter

from fact_utils import load, save


RUNTIME_KINDS = {
    "STATUS_EFFECT",
    "TEMPORAL_RULE",
    "SUMMON_RULE",
    "STACK_RULE",
    "MECHANIC",
}


def is_runtime_talent(row):
    return bool(
        row.get("triggers")
        or row.get("conditions")
        or row.get("internal_cooldown")
        or row.get("quest")
        or row.get("stacking")
        or row.get("temporal_rules")
        or row.get("counter_rules")
        or row.get("relations")
        or row.get("quest_v2")
    )


def normalize_modifier(effect):
    kind = effect.get("kind")
    value = effect.get("value")

    if kind == "MODIFIER" and isinstance(value, dict):
        return {
            "type": "MODIFIER",
            "metric": value.get("metric"),
            "operation": value.get("operation"),
            "value": value.get("value"),
            "from": value.get("from"),
            "to": value.get("to"),
            "target_filter": effect.get("target_filter") or [],
            "confidence": effect.get("confidence"),
            "provenance": effect.get("provenance"),
        }

    return {
        "type": kind,
        "value": value,
        "duration_seconds": effect.get("duration_seconds"),
        "amount_percent": effect.get("amount_percent"),
        "target_filter": effect.get("target_filter") or [],
        "confidence": effect.get("confidence"),
        "provenance": effect.get("provenance"),
    }


def run_talent_decoder(
    talent_modifier_facts_path,
    modifiers_output_path,
    runtime_output_path,
    report_path,
):
    data = load(talent_modifier_facts_path)

    modifiers_heroes = {}
    runtime_heroes = {}

    counts = Counter()
    metric_counts = Counter()
    operation_counts = Counter()
    runtime_trigger_counts = Counter()

    for hero, rows in data.get("heroes", {}).items():
        mods = []
        rules = []

        for row in rows:
            tid = row.get("talent_id")
            targets = row.get("targets") or []
            effects = row.get("effects") or []

            persistent_effects = []
            runtime_effects = []

            runtime = is_runtime_talent(row)

            for e in effects:
                ne = normalize_modifier(e)
                if runtime or e.get("kind") in RUNTIME_KINDS:
                    runtime_effects.append(ne)
                else:
                    persistent_effects.append(ne)

                if ne.get("type") == "MODIFIER":
                    metric_counts[str(ne.get("metric"))] += 1
                    operation_counts[str(ne.get("operation"))] += 1

            if persistent_effects:
                counts["talents_with_persistent_modifiers"] += 1
                mods.append({
                    "talent_id": tid,
                    "name": row.get("name"),
                    "level": row.get("level"),
                    "classification": row.get("final_classification"),
                    "targets": targets,
                    "modifiers": persistent_effects,
                    "source_completeness": row.get("completeness"),
                    "numeric_coverage": row.get("numeric_coverage"),
                })

            if runtime or runtime_effects:
                counts["talents_with_runtime_rules"] += 1
                for trig in row.get("triggers") or []:
                    runtime_trigger_counts[str(trig.get("value"))] += 1

                rules.append({
                    "talent_id": tid,
                    "name": row.get("name"),
                    "level": row.get("level"),
                    "classification": row.get("final_classification"),
                    "targets": targets,
                    "triggers": row.get("triggers") or [],
                    "conditions": row.get("conditions") or [],
                    "effects": runtime_effects,
                    "temporal_rules": row.get("temporal_rules") or [],
                    "counter_rules": row.get("counter_rules") or [],
                    "relations": row.get("relations") or [],
                    "quest_v2": row.get("quest_v2"),
                    "stacking": row.get("stacking") or [],
                    "internal_cooldown": row.get("internal_cooldown") or [],
                    "quest": row.get("quest") or [],
                    "source_completeness": row.get("completeness"),
                    "numeric_coverage": row.get("numeric_coverage"),
                })

            counts["talents_total"] += 1

        modifiers_heroes[hero] = mods
        runtime_heroes[hero] = rules

    modifiers = {
        "metadata": {
            "builder": "talent_modifier_decoder_v1",
            "purpose": "Persistent talent operations consumable by modifier_engine.",
        },
        "heroes": modifiers_heroes,
    }

    runtime = {
        "metadata": {
            "builder": "talent_runtime_decoder_v1",
            "purpose": "Conditional/triggered talent rules consumable at replay runtime.",
        },
        "heroes": runtime_heroes,
    }

    report = {
        "summary": dict(counts),
        "modifier_metrics": dict(metric_counts),
        "modifier_operations": dict(operation_counts),
        "runtime_triggers": dict(runtime_trigger_counts),
    }

    save(modifiers_output_path, modifiers)
    save(runtime_output_path, runtime)
    save(report_path, report)
    return report
