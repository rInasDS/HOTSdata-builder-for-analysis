from __future__ import annotations

from fact_utils import load, save


def metric(report, key, default=0):
    return (report.get("summary") or {}).get(key, default)


def run_baseline_guard(
    herodata_path,
    baseline_manifest_path,
    ability_catalog_report_path,
    talent_modifier_report_path,
    rule_miner_report_path,
    spatial_report_path,
    area_report_path,
    projectile_report_path,
    combat_report_path,
    validation_report_path,
    output_path,
):
    hd = load(herodata_path)
    baseline = load(baseline_manifest_path)

    current_version = (hd.get("metadata") or {}).get("heroes_version")
    baseline_version = (baseline.get("metadata") or {}).get("heroes_version")
    same_build = current_version == baseline_version

    ability = load(ability_catalog_report_path)
    talent = load(talent_modifier_report_path)
    miner = load(rule_miner_report_path)
    spatial = load(spatial_report_path)
    area = load(area_report_path)
    projectile = load(projectile_report_path)
    combat = load(combat_report_path)
    validation = load(validation_report_path)

    failures = []
    checks = []

    def check(name, ok, current=None, expected=None, blocking=True):
        row = {
            "name": name,
            "passed": bool(ok),
            "current": current,
            "expected": expected,
            "blocking": blocking,
        }
        checks.append(row)
        if blocking and not ok:
            failures.append(row)

    # Universal structural invariants: always apply.
    a_total = metric(ability, "abilities_total")
    t_total = metric(talent, "talents_total")
    check("abilities_total_positive", a_total > 0, a_total, ">0")
    check("talents_total_positive", t_total > 0, t_total, ">0")
    check(
        "catalog_partition",
        metric(ability, "matched") + metric(ability, "unmatched") == a_total,
        metric(ability, "matched") + metric(ability, "unmatched"),
        a_total,
    )
    check(
        "talent_partition",
        metric(talent, "complete") + metric(talent, "partial") + metric(talent, "unresolved") == t_total,
        metric(talent, "complete") + metric(talent, "partial") + metric(talent, "unresolved"),
        t_total,
    )
    check(
        "spatial_partition",
        metric(spatial, "spatial_complete")
        + metric(spatial, "spatial_partial")
        + metric(spatial, "spatial_unresolved")
        + metric(spatial, "non_spatial")
        == metric(spatial, "abilities_total"),
        metric(spatial, "spatial_complete")
        + metric(spatial, "spatial_partial")
        + metric(spatial, "spatial_unresolved")
        + metric(spatial, "non_spatial"),
        metric(spatial, "abilities_total"),
    )

    # Same-build regression thresholds from verified v3.8.
    if same_build:
        m = baseline.get("metrics") or {}
        exact = [
            ("abilities_total", a_total, m["abilities_total"]),
            ("talents_total", t_total, m["talents_total"]),
            ("catalog_matched", metric(ability, "matched"), m["catalog_matched"]),
        ]
        for name, cur, exp in exact:
            check(name, cur >= exp, cur, f">={exp}")

        check(
            "talent_complete_no_regression",
            metric(talent, "complete") >= m["talent_complete_min"],
            metric(talent, "complete"),
            f">={m['talent_complete_min']}",
        )
        check(
            "talent_unresolved_no_regression",
            metric(talent, "unresolved") <= m["talent_unresolved_max"],
            metric(talent, "unresolved"),
            f"<={m['talent_unresolved_max']}",
        )
        check(
            "missing_numeric_tokens_no_regression",
            metric(miner, "missing_numeric_tokens") <= m["missing_numeric_tokens_max"],
            metric(miner, "missing_numeric_tokens"),
            f"<={m['missing_numeric_tokens_max']}",
        )
        check(
            "unknown_numeric_contexts_no_regression",
            metric(miner, "unknown_numeric_contexts") <= m["unknown_numeric_contexts_max"],
            metric(miner, "unknown_numeric_contexts"),
            f"<={m['unknown_numeric_contexts_max']}",
        )
        check(
            "spatial_complete_no_regression",
            metric(spatial, "spatial_complete") >= m["spatial_complete_min"],
            metric(spatial, "spatial_complete"),
            f">={m['spatial_complete_min']}",
        )
        check(
            "spatial_unresolved_no_regression",
            metric(spatial, "spatial_unresolved") <= m["spatial_unresolved_max"],
            metric(spatial, "spatial_unresolved"),
            f"<={m['spatial_unresolved_max']}",
        )
        check(
            "radius_coverage_no_regression",
            metric(area, "with_radius") >= m["with_radius_min"],
            metric(area, "with_radius"),
            f">={m['with_radius_min']}",
        )
        check(
            "projectile_coverage_no_regression",
            metric(projectile, "with_projectile") >= m["with_projectile_min"],
            metric(projectile, "with_projectile"),
            f">={m['with_projectile_min']}",
        )
        # Combat engine v1.13 changed the metric semantics: base and talent-gated
        # mechanics are separated and broad reachability false-positives (notably
        # Hearthstone/Mount) are no longer counted. Do not compare typed heal
        # coverage against the legacy v3.8 reachability count.
        if "with_any_heal" in (combat.get("summary") or {}):
            check(
                "typed_damage_coverage",
                metric(combat, "with_any_damage") >= m.get("typed_with_any_damage_min", 300),
                metric(combat, "with_any_damage"),
                f">={m.get('typed_with_any_damage_min', 300)}",
            )
            check(
                "typed_heal_coverage",
                metric(combat, "with_any_heal") >= m.get("typed_with_any_heal_min", 150),
                metric(combat, "with_any_heal"),
                f">={m.get('typed_with_any_heal_min', 150)}",
            )
            check(
                "typed_shield_coverage",
                metric(combat, "with_any_shield") >= m.get("typed_with_any_shield_min", 15),
                metric(combat, "with_any_shield"),
                f">={m.get('typed_with_any_shield_min', 15)}",
            )
        else:
            check(
                "damage_coverage_no_regression",
                metric(combat, "with_damage") >= m["with_damage_min"],
                metric(combat, "with_damage"),
                f">={m['with_damage_min']}",
            )
            check(
                "heal_coverage_no_regression",
                metric(combat, "with_heal") >= m["with_heal_min"],
                metric(combat, "with_heal"),
                f">={m['with_heal_min']}",
            )

    report = {
        "summary": {
            "same_build_baseline": same_build,
            "current_heroes_version": current_version,
            "baseline_heroes_version": baseline_version,
            "checks_total": len(checks),
            "passed": sum(1 for c in checks if c["passed"]),
            "failed": len(failures),
        },
        "checks": checks,
        "failures": failures,
    }
    save(output_path, report)

    if failures:
        raise RuntimeError(
            f"Baseline guard failed: {len(failures)} blocking regression(s)"
        )
    return report
