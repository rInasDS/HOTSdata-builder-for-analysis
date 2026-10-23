from __future__ import annotations
from talent_modifier_parser import collect_explained_numbers
from talent_modifier_parser import tooltip_numeric_tokens
from talent_modifier_parser import match_numeric_coverage

import json
from collections import Counter

from fact_utils import load, save
from subeffect_graph_builder import base_text_subeffects
from shield_model import infer_shield_models
from heal_model import infer_heal_models
from combat_effect_engine import is_base_shield_candidate
from combat_formula_model import ability_damage_models
from talent_modifier_parser import normalize_tooltip, infer_effects, infer_counter_rules, infer_quest_v2, infer_temporal_rules, infer_conditions, collect_explained_numbers, tooltip_numeric_tokens, match_numeric_coverage


def find_talent(data, talent_id):
    for hero, rows in data.get("heroes", {}).items():
        for row in rows:
            if row.get("talent_id") == talent_id:
                return hero, row
    return None, None


def effect_matches(row, kind=None, value=None, metric=None, operation=None, number=None):
    matches = []
    for e in row.get("effects") or []:
        if kind and e.get("kind") != kind:
            continue
        if value and e.get("value") != value:
            continue

        val = e.get("value")
        if metric and (not isinstance(val, dict) or val.get("metric") != metric):
            continue
        if operation and (not isinstance(val, dict) or val.get("operation") != operation):
            continue
        if number is not None and (not isinstance(val, dict) or val.get("value") != number):
            continue

        matches.append(e)
    return matches


def run_regressions(talent_modifier_facts_path, spatial_path, subeffect_path, report_path):
    data = load(talent_modifier_facts_path)
    spatial_data = load(spatial_path)
    subeffect_data = load(subeffect_path)

    tests = []
    failures = []

    def check(name, condition, detail=None):
        row = {"name": name, "passed": bool(condition)}
        if detail is not None:
            row["detail"] = detail
        tests.append(row)
        if not condition:
            failures.append(row)

    # --------------------------------------------------------
    # Slapathur hard regression case
    # --------------------------------------------------------
    hero, slap = find_talent(data, "AbathurSlapathur")
    check("Slapathur exists", slap is not None)

    if slap:
        check(
            "Slapathur classification",
            slap.get("final_classification") == "BASIC_ATTACK_MODIFIER",
            slap.get("final_classification"),
        )

        dmg = effect_matches(
            slap, kind="MODIFIER",
            metric="BASIC_ATTACK_DAMAGE",
            operation="ADD_PERCENT",
            number=350.0,
        )
        check("Slapathur +350% BA damage", bool(dmg))

        stuns = effect_matches(slap, kind="STATUS_EFFECT", value="STUN")
        correct_stun = any(
            "NON_ELITE" in (x.get("target_filter") or [])
            and "ELITE" not in (x.get("target_filter") or [])
            and "STRUCTURE" not in (x.get("target_filter") or [])
            for x in stuns
        )
        check("Slapathur STUN scoped to NON_ELITE", correct_stun, stuns)

        disables = effect_matches(slap, kind="STATUS_EFFECT", value="STRUCTURE_DISABLE")
        correct_disable = any(
            x.get("duration_seconds") == 1.5
            and x.get("target_filter") == ["STRUCTURE"]
            for x in disables
        )
        check("Slapathur structure disable 1.5s", correct_disable, disables)

        icd = slap.get("internal_cooldown") or []
        correct_icd = any(
            x.get("value") == 6.0
            and x.get("per_target") is True
            and {"HERO", "STRUCTURE"}.issubset(set(x.get("target_filter") or []))
            for x in icd
        )
        check("Slapathur 6s per-target ICD HERO|STRUCTURE", correct_icd, icd)

        check(
            "Slapathur numeric coverage 100%",
            (slap.get("numeric_coverage") or {}).get("coverage_ratio") == 1.0,
            slap.get("numeric_coverage"),
        )

        suspect = [
            x for x in slap.get("xml_evidence") or []
            if x.get("object_id") == "AbathurSymbioteCarapace"
        ]
        check(
            "Slapathur Carapace is suspect alias",
            bool(suspect) and all(x.get("relation") == "SUSPECT_ALIAS" for x in suspect),
            suspect,
        )

    combo_text=normalize_tooltip("Hit a Hero with 3 of Alarak's Abilities within 2 seconds to deal an additional 61 damage.")
    combo=infer_counter_rules(combo_text); combo_fx=infer_effects(combo_text)
    check("Ability combo 3 within 2s", any(isinstance(x.get("value"),dict) and x["value"].get("type")=="ABILITY_COMBO" and x["value"].get("required_count")==3 and x["value"].get("window_seconds")==2.0 for x in combo), combo)
    check("Additional flat damage 61", any(isinstance(x.get("value"),dict) and x["value"].get("operation")=="DEAL_FLAT" and x["value"].get("value")==61.0 for x in combo_fx), combo_fx)
    hp_fx=infer_effects(normalize_tooltip("Basic Attacks deal 1% of the target's maximum Health."))
    check("Percent max health damage", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="PERCENT_MAX_HEALTH_DAMAGE" and x["value"].get("value")==1.0 for x in hp_fx), hp_fx)
    rate_fx=infer_effects(normalize_tooltip("Basic Ability cooldowns recharge 10% faster."))
    check("Cooldown recharge rate", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="COOLDOWN_RECHARGE_RATE" and x["value"].get("value")==10.0 for x in rate_fx), rate_fx)
    target_fx=infer_effects(normalize_tooltip("The projectile can hit 2 additional targets."))
    check("Additional targets count", any(x.get("kind")=="COUNT_MODIFIER" and isinstance(x.get("value"),dict) and x["value"].get("metric")=="ADDITIONAL_TARGETS" and x["value"].get("value")==2 for x in target_fx), target_fx)
    regen_fx=infer_effects(normalize_tooltip("While Assimilation is active, Kerrigan's Health and Mana regeneration is increased by 150%."))
    check("Joint Health regen +150%", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="HEALTH_REGEN_RATE" and x["value"].get("operation")=="ADD_PERCENT" and x["value"].get("value")==150.0 for x in regen_fx), regen_fx)
    check("Joint Mana regen +150%", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="MANA_REGEN_RATE" and x["value"].get("operation")=="ADD_PERCENT" and x["value"].get("value")==150.0 for x in regen_fx), regen_fx)
    medivh_regen_fx=infer_effects(normalize_tooltip("Raven Form increases Medivh's Mana and Health Regeneration by 200%."))
    check("Joint Mana regen +200% reverse order", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="MANA_REGEN_RATE" and x["value"].get("value")==200.0 for x in medivh_regen_fx), medivh_regen_fx)
    check("Joint Health regen +200% reverse order", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="HEALTH_REGEN_RATE" and x["value"].get("value")==200.0 for x in medivh_regen_fx), medivh_regen_fx)

    # v3.12.3 damage/control semantics for replay resolver
    gambit_fx=infer_effects(normalize_tooltip("Gambit: Gain 60% bonus Give 'Em Some Pepper damage. Every death reduces this bonus by 15%."))
    check("Named damage bonus +60%", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE_BONUS" and x["value"].get("operation")=="ADD_PERCENT" and x["value"].get("value")==60.0 for x in gambit_fx), gambit_fx)
    periodic_fx=infer_effects(normalize_tooltip("Permanently increase periodic damage by 0.25."))
    check("Periodic damage +0.25 flat", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE" and x["value"].get("operation")=="ADD_FLAT" and x["value"].get("value")==0.25 for x in periodic_fx), periodic_fx)
    cc_fx=infer_effects(normalize_tooltip("Knock enemies back, Stunning and Slowing them. Become Unstoppable."))
    for status in ("KNOCKBACK","STUN","SLOW","UNSTOPPABLE"):
        check("Status semantic "+status, any(x.get("kind")=="STATUS_EFFECT" and x.get("value")==status for x in cc_fx), cc_fx)

    anub_nodes=base_text_subeffects({
        "short_text":"Charge underground",
        "full_text":"Burrow in a direction, dealing 91 damage, briefly Stunning enemies and Slowing them by 25% for 2.5 seconds."
    }, {"damage":[{"amount":91}],"heal":[],"shield_candidates":[]})
    check("Base ability DAMAGE node", any(x.get("kind")=="DAMAGE" for x in anub_nodes), anub_nodes)
    check("Base ability STUN semantic", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="STUN" for x in anub_nodes), anub_nodes)
    check("Base ability SLOW semantic", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="SLOW" and x.get("amount_percent")==25.0 for x in anub_nodes), anub_nodes)

    # --------------------------------------------------------
    # Global invariants
    # --------------------------------------------------------
    scaling_leaks = []
    duplicate_effects = []
    duplicate_xml = []

    for hero, rows in data.get("heroes", {}).items():
        for row in rows:
            text = (row.get("raw_text") or {}).get("normalized") or ""
            if "~~" in text:
                scaling_leaks.append((hero, row.get("talent_id")))

            seen_effect = set()
            for e in row.get("effects") or []:
                key = json.dumps(
                    {k: v for k, v in e.items() if k != "provenance"},
                    sort_keys=True, ensure_ascii=False,
                )
                if key in seen_effect:
                    duplicate_effects.append((hero, row.get("talent_id"), key))
                seen_effect.add(key)

            seen_xml = set()
            for x in row.get("xml_evidence") or []:
                key = (x.get("object_id"), x.get("xml_type"), x.get("file"))
                if key in seen_xml:
                    duplicate_xml.append((hero, row.get("talent_id"), key))
                seen_xml.add(key)

    check("No scaling markers after normalization", not scaling_leaks, scaling_leaks[:20])
    check("No exact duplicate effects", not duplicate_effects, duplicate_effects[:20])
    check("XML evidence object-level dedup", not duplicate_xml, duplicate_xml[:20])



    # --------------------------------------------------------
    # v3.10 real-report regression phrases
    # --------------------------------------------------------
    heat_fx = infer_effects(
        normalize_tooltip("Increase Flame Buffet's Slow from 40% to 50% and cause it to no longer decay.")
    )
    check(
        "SET_FROM_TO 40 to 50 percent",
        any(
            isinstance(x.get("value"), dict)
            and x["value"].get("operation") == "SET_FROM_TO"
            and x["value"].get("from") == 40.0
            and x["value"].get("to") == 50.0
            for x in heat_fx
        ),
        heat_fx,
    )

    anti_fx = infer_effects(
        normalize_tooltip(
            "Increase the area of Biotic Grenade by 25% and the duration of the healing reduction by 0.75 seconds."
        )
    )
    check(
        "Spatial area +25%",
        any(
            isinstance(x.get("value"), dict)
            and x["value"].get("metric") == "AREA"
            and x["value"].get("value") == 25.0
            for x in anti_fx
        ),
        anti_fx,
    )
    check(
        "Duration +0.75 sec",
        any(
            isinstance(x.get("value"), dict)
            and x["value"].get("metric") == "DURATION"
            and x["value"].get("value") == 0.75
            for x in anti_fx
        ),
        anti_fx,
    )

    eye_fx = infer_effects(
        normalize_tooltip(
            "Allies are healed for 225 and enemies are damaged for 135. Deals 50% less damage to Structures."
        )
    )
    check(
        "Target-scoped ally heal 225",
        any(
            isinstance(x.get("value"), dict)
            and x["value"].get("metric") == "HEALING"
            and x["value"].get("value") == 225.0
            and x.get("target_filter") == ["ALLY"]
            for x in eye_fx
        ),
        eye_fx,
    )
    check(
        "Target-scoped enemy damage 135",
        any(
            isinstance(x.get("value"), dict)
            and x["value"].get("metric") == "DAMAGE"
            and x["value"].get("value") == 135.0
            and x.get("target_filter") == ["ENEMY"]
            for x in eye_fx
        ),
        eye_fx,
    )
    check(
        "Structure damage reduction 50%",
        any(
            isinstance(x.get("value"), dict)
            and x["value"].get("metric") == "DAMAGE"
            and x["value"].get("operation") == "REDUCE_PERCENT"
            and x["value"].get("value") == 50.0
            and "STRUCTURE" in (x.get("target_filter") or [])
            for x in eye_fx
        ),
        eye_fx,
    )


    pure_text = normalize_tooltip(
        "Increase Sadism by 10% any time an allied Hero dies, up to 40%. Sadism gained from Pure Malice is lost on death."
    )
    pure_fx = infer_effects(pure_text)
    check(
        "Custom stat SADISM +10%",
        any(isinstance(x.get("value"),dict)
            and x["value"].get("metric")=="SADISM"
            and x["value"].get("value")==10.0
            for x in pure_fx),
        pure_fx,
    )


    # --------------------------------------------------------
    # v3.11 Quest Engine v2 report-derived tests
    # --------------------------------------------------------
    q = infer_quest_v2(
        normalize_tooltip(
            "Black Harvest Quest: Apply Reaper's Mark to Heroes for a total of 150 seconds. "
            "Reward: Permanently increase Reaper's Mark's duration by 2 seconds."
        ),
        infer_effects(normalize_tooltip(
            "Black Harvest Quest: Apply Reaper's Mark to Heroes for a total of 150 seconds. "
            "Reward: Permanently increase Reaper's Mark's duration by 2 seconds."
        )), [], []
    )
    check(
        "Quest total duration target 150",
        any(x.get("threshold") == 150.0 and x.get("unit") == "SECONDS"
            for x in (q or {}).get("milestones", [])),
        q,
    )

    q = infer_quest_v2(
        normalize_tooltip(
            "Regeneration Master Quest: Gathering a Regeneration Globe increases your Health Regeneration "
            "by 1 per second, up to 30. Reward: After gathering 25 Regeneration Globes, you also gain 500 Health."
        ),
        infer_effects(normalize_tooltip(
            "Regeneration Master Quest: Gathering a Regeneration Globe increases your Health Regeneration "
            "by 1 per second, up to 30. Reward: After gathering 25 Regeneration Globes, you also gain 500 Health."
        )), [], []
    )
    check("Quest cap 30", (q or {}).get("cap", {}).get("value") == 30.0, q)
    check(
        "Quest milestone 25 globes",
        any(x.get("threshold") == 25.0 for x in (q or {}).get("milestones", [])),
        q,
    )

    q = infer_quest_v2(
        normalize_tooltip(
            "Crash Lightning Quest: Hit at least 2 Heroes with a single use of Chain Lightning. "
            "Upon dying, lose 4 stacks. Reward: After hitting multiple Heroes 15 times, "
            "Chain Lightning deals 270 additional damage on its bounces. "
            "Mythic Reward: After hitting multiple Heroes 30 times, Chain Lightning deals 325 additional damage."
        ),
        infer_effects(normalize_tooltip(
            "Crash Lightning Quest: Hit at least 2 Heroes with a single use of Chain Lightning. "
            "Upon dying, lose 4 stacks. Reward: After hitting multiple Heroes 15 times, "
            "Chain Lightning deals 270 additional damage on its bounces. "
            "Mythic Reward: After hitting multiple Heroes 30 times, Chain Lightning deals 325 additional damage."
        )), [], []
    )
    check(
        "Quest same-cast threshold",
        any(x.get("threshold") == 2 and x.get("kind") == "SINGLE_CAST_REQUIREMENT"
            for x in (q or {}).get("milestones", [])),
        q,
    )
    check(
        "Quest death penalty",
        (q or {}).get("death_penalty", {}).get("value") == 4,
        q,
    )

    q = infer_quest_v2(
        normalize_tooltip(
            "Pin Down Quest: Hit 3 enemy Heroes with a single Fan of Knives. "
            "Reward: Increase Fan of Knives damage by 35%. "
            "Reward: Each additional time 3 enemy Heroes are hit by a single Fan of Knives, "
            "gain an additional 5% damage, up to 50%. "
            "Reward: Hit 4 enemy Heroes with a single Fan of Knives to instantly gain all Rewards."
        ),
        infer_effects(normalize_tooltip(
            "Pin Down Quest: Hit 3 enemy Heroes with a single Fan of Knives. "
            "Reward: Increase Fan of Knives damage by 35%. "
            "Reward: Each additional time 3 enemy Heroes are hit by a single Fan of Knives, "
            "gain an additional 5% damage, up to 50%. "
            "Reward: Hit 4 enemy Heroes with a single Fan of Knives to instantly gain all Rewards."
        )), [], []
    )
    check("Quest repeat cap 50", (q or {}).get("cap", {}).get("value") == 50.0, q)
    check(
        "Quest completion shortcut 4",
        any(x.get("threshold") == 4 and x.get("operation") == "GRANT_ALL_REWARDS"
            for x in (q or {}).get("completion_shortcuts", [])),
        q,
    )


    # --------------------------------------------------------
    # v3.11.1 Quest numeric-coverage wiring regression
    # --------------------------------------------------------
    qtext = normalize_tooltip(
        "Black Harvest Quest: Apply Reaper's Mark to Heroes for a total of 150 seconds. "
        "Reward: Permanently increase Reaper's Mark's duration by 2 seconds."
    )
    qeffects = infer_effects(qtext)
    qmodel = infer_quest_v2(qtext, qeffects, [], [])
    qtokens = tooltip_numeric_tokens(qtext)
    qexplained = collect_explained_numbers(
        qeffects, [], [], [], [], [], [], [], [], qmodel
    )
    qcoverage = match_numeric_coverage(qtokens, qexplained)
    check(
        "Quest v2 contributes to numeric coverage",
        any(
            x.get("token", {}).get("value") == 150.0
            and x.get("explained_by", {}).get("reason") == "QUEST_V2:MILESTONE"
            for x in qcoverage.get("matched", [])
        ),
        qcoverage,
    )


    # v3.12 Numeric Coverage Engine v2
    press_text=normalize_tooltip("Increases the range of Symbiote's Spike Burst by 25% and decreases the cooldown by 1 second.")
    press_fx=infer_effects(press_text)
    press_cov=match_numeric_coverage(tooltip_numeric_tokens(press_text),collect_explained_numbers(press_fx,[],[],[],[],[],[],[],[],None),text=press_text)
    check("Pressurized Glands numeric coverage 100%",press_cov.get("coverage_ratio")==1.0,press_cov)
    check("Pressurized Glands canonical RANGE",any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="RANGE" and x["value"].get("value")==25.0 for x in press_fx),press_fx)
    check("No verbose fake RANGE custom stat",not any(isinstance(x.get("value"),dict) and str(x["value"].get("metric","")).startswith("THE_RANGE_OF_") for x in press_fx),press_fx)

    nest_text=normalize_tooltip("Toxic Nests deal 75% more damage over 3 seconds and reduce the Armor of enemy Heroes hit by 15 for 4 seconds.")
    nest_fx=infer_effects(nest_text); nest_temporal=infer_temporal_rules(nest_text,[])
    nest_cov=match_numeric_coverage(tooltip_numeric_tokens(nest_text),collect_explained_numbers(nest_fx,[],[],[],[],[],nest_temporal,[],[],None),text=nest_text)
    check("Envenomed Nest four-number coverage 100%",nest_cov.get("coverage_ratio")==1.0,nest_cov)
    check("Envenomed Nest canonical ARMOR",any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="ARMOR" and x["value"].get("value")==15.0 for x in nest_fx),nest_fx)

    dur_text=normalize_tooltip("Increase the duration by 0.75 seconds.");dur_fx=infer_effects(dur_text)
    dur_cov=match_numeric_coverage(tooltip_numeric_tokens(dur_text),collect_explained_numbers(dur_fx,[],[],[],[],[],[],[],[],None),text=dur_text)
    check("Duration ADD_SECONDS unit coverage",dur_cov.get("coverage_ratio")==1.0,dur_cov)

    # --------------------------------------------------------
    # v3.12.4 Shield Model regressions
    # --------------------------------------------------------
    sm=infer_shield_models("Activate to gain 25% of your maximum Health as a Shield for 3 seconds.")
    check("Shield model max-health percent", bool(sm) and sm[0].get("amount_model")=="MAX_HEALTH_PERCENT" and sm[0].get("value")==25.0 and sm[0].get("duration_seconds")==3.0, sm)

    sm=infer_shield_models("Activate to gain a Shield equal to 30% of Deathwing's missing Health for 8 seconds.")
    check("Shield model missing-health percent", bool(sm) and sm[0].get("amount_model")=="MISSING_HEALTH_PERCENT" and sm[0].get("value")==30.0, sm)

    sm=infer_shield_models("Gain a Shield equal to 100% of Kael'thas's maximum Mana for 4 seconds.")
    check("Shield model max-mana percent", bool(sm) and sm[0].get("amount_model")=="MAX_MANA_PERCENT" and sm[0].get("value")==100.0, sm)

    sm=infer_shield_models("When Deflect ends, Genji gains a Shield equal to 100% of the damage blocked for 4 seconds.")
    check("Shield model damage-blocked percent", bool(sm) and sm[0].get("amount_model")=="DAMAGE_BLOCKED_PERCENT" and sm[0].get("value")==100.0, sm)

    sm=infer_shield_models("Upon taking fatal damage, Johanna gains a Shield equal to her maximum Health for 4 seconds.")
    check("Shield model fatal-damage trigger", bool(sm) and sm[0].get("amount_model")=="MAX_HEALTH_PERCENT" and sm[0].get("value")==100.0 and sm[0].get("trigger")=="ON_FATAL_DAMAGE", sm)

    check("Talent shield branch excluded from base shield semantics",
          not is_base_shield_candidate(
              "AnubarakBurrowChargeApplySubterraneanShieldTalentShield",
              ["AnubarakBurrowCharge","AnubarakBurrowChargePersistent","AnubarakBurrowChargeApplySubterraneanShieldTalentShield"]
          ))
    check("Direct base shield branch remains eligible",
          is_base_shield_candidate("JohannaIronSkinShield",["JohannaIronSkin","JohannaIronSkinShield"]))


    # --------------------------------------------------------
    # v3.12.6 Talent combat/control resolver regressions
    # --------------------------------------------------------
    txt=normalize_tooltip("Deal 25% increased damage to enemies while they are marked or Slowed by Singularity Spike.")
    fx=infer_effects(txt)
    check("Conditional increased damage parsed", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE" and x["value"].get("operation")=="ADD_PERCENT" and x["value"].get("value")==25.0 for x in fx), fx)
    check("Target-state Slow not falsely applied", not any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="SLOW" for x in fx), fx)

    txt=normalize_tooltip("While Zul'jin is below 75% Health, he gains 10% Spell Power. While he is below 50% Health, his Basic Attack damage is increased by 10%.")
    fx=infer_effects(txt); cond=infer_conditions(txt)
    check("Spell Power conditional modifier", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="SPELL_POWER" and x["value"].get("value")==10.0 for x in fx), fx)
    check("Basic Attack conditional damage modifier", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="BASIC_ATTACK_DAMAGE" and x["value"].get("value")==10.0 for x in fx), fx)
    check("Multiple low-health thresholds retained", sorted(x.get("threshold_percent") for x in cond if x.get("kind")=="CONDITION" and x.get("value")=="LOW_HEALTH") == [50.0,75.0], cond)

    txt=normalize_tooltip("Increase the Slow amount of Twin Cleave by 10% and its duration by 0.5 seconds.")
    fx=infer_effects(txt)
    check("Talent Slow overlay parsed", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="SLOW" for x in fx), fx)

    txt=normalize_tooltip("Enemy Heroes hit are Silenced for 1.5 seconds.")
    fx=infer_effects(txt)
    check("Talent Silence overlay parsed", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="SILENCE" for x in fx), fx)

    txt=normalize_tooltip("Activate to remove all Roots and Slows from the target allied Hero.")
    fx=infer_effects(txt)
    check("Status removal exported as remove semantics", any(x.get("kind")=="STATUS_REMOVE" and x.get("value") in {"ROOT","SLOW"} for x in fx), fx)

    # --------------------------------------------------------
    # v3.12.6 combat/control grammar regressions
    # --------------------------------------------------------
    fx=infer_effects(normalize_tooltip("Deal 25% increased damage to enemies while they are marked or Slowed."))
    check("Conditional +25% damage extracted", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE" and x["value"].get("operation")=="ADD_PERCENT" and x["value"].get("value")==25.0 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Cleave deals an additional 40% damage over 3 seconds."))
    check("DoT percent damage extracted", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE_OVER_TIME" and x["value"].get("value")==40.0 and x.get("duration_seconds")==3.0 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Enemy Heroes hit by the Ability deal 40% less damage for 3 seconds."))
    check("Enemy damage dealt reduction extracted", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="ENEMY_DAMAGE_DEALT" and x["value"].get("value")==40.0 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Charge deals bonus damage to Heroes equal to 4% of their maximum Health."))
    check("Percent max-health damage extracted", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="PERCENT_MAX_HEALTH_DAMAGE" and x["value"].get("value")==4.0 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Activate to remove Roots and Slows."))
    check("Cleanse exports status removals", {x.get("value") for x in fx if x.get("kind")=="STATUS_REMOVE"}>={"ROOT","SLOW"}, fx)
    check("Cleanse semantic emitted", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="CLEANSE" for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Attacking a Hero that is Slowed, Rooted, or Stunned increases Basic Attack damage by 30%."))
    check("Target-state conditions do not apply CC", not any(x.get("kind")=="STATUS_EFFECT" and x.get("value") in {"SLOW","ROOT","STUN"} for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Slows time in an area to a near standstill."))
    check("Slows time is not movement Slow", not any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="SLOW" for x in fx), fx)

    fx=infer_effects(normalize_tooltip("The blast deals damage, silencing enemies for 2 seconds."))
    check("Silencing grammar recognized", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="SILENCE" for x in fx), fx)

    fx=infer_effects(normalize_tooltip("The grenade deals damage, knocking enemies away."))
    check("Knocking away classified as knockback", any(x.get("kind")=="STATUS_EFFECT" and x.get("value")=="KNOCKBACK" for x in fx), fx)


    # --------------------------------------------------------
    # v3.12.7 damage source/formula regressions
    # --------------------------------------------------------
    fx=infer_effects(normalize_tooltip("Guillotine continues forward, dealing 75% damage to enemies hit."))
    check("Percent-of-base secondary damage parsed", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE" and x["value"].get("operation")=="SET_PERCENT_OF_BASE" and x["value"].get("value")==75.0 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Basic Attacks deal 33% damage to 3 nearby targets."))
    check("Secondary basic-attack damage parsed", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="BASIC_ATTACK_DAMAGE" and x["value"].get("operation")=="SET_PERCENT_OF_BASE" and x.get("secondary_target_count")==3 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Nearby ranged Minions deal an additional 100% damage."))
    check("Summon/minion damage percent parsed", any(isinstance(x.get("value"),dict) and x["value"].get("metric")=="DAMAGE" and x["value"].get("operation")=="ADD_PERCENT" and x["value"].get("value")==100.0 for x in fx), fx)

    fx=infer_effects(normalize_tooltip("Critical Strikes deal bonus spell damage equal to 50% of their Basic Attack damage."))
    check("Damage ratio to basic attack parsed", any(isinstance(x.get("value"),dict) and x["value"].get("operation")=="RATIO_TO_BASIC_ATTACK" and x["value"].get("value")==50.0 for x in fx), fx)

    dm=ability_damage_models({"short_text":"Launch an orb", "full_text":"Fire an orb that deals 135 damage."},{"damage":[]})
    check("Ability tooltip flat damage model", any(x.get("formula_model")=="FLAT" and x.get("value")==135.0 for x in dm), dm)

    dm=ability_damage_models({"short_text":"Damage based on health", "full_text":"Deal 20% of the target's maximum Health as damage."},{"damage":[]})
    check("Ability percent max-health model", any(x.get("formula_model")=="PERCENT_MAX_HEALTH" and x.get("value_percent")==20.0 for x in dm), dm)

    dm=ability_damage_models({"short_text":"Reduce damage", "full_text":"Activate to reduce enemy Hero damage by 40%."},{"damage":[]})
    check("Damage reduction text is not direct damage", not dm, dm)

    # --------------------------------------------------------
    # v3.12.9 heal/shield model regressions
    # --------------------------------------------------------
    hm=infer_heal_models("Healing Totem heals nearby allies for 2% of their maximum Health every 1 second.")
    check("Max-health heal per tick parsed", any(x.get("amount_model")=="PERCENT_MAX_HEALTH_PER_TICK" and x.get("value")==2.0 and x.get("tick_interval_seconds")==1.0 for x in hm), hm)

    hm=infer_heal_models("Basic Attacks heal the target allied Hero for 175% of the damage dealt.")
    check("Basic attack damage-to-heal parsed", any(x.get("amount_model")=="BASIC_ATTACK_DAMAGE_PERCENT" and x.get("value")==175.0 for x in hm), hm)

    hm=infer_heal_models("Lose 50% of current Health and regain that amount over 10 seconds.")
    check("Self-damage snapshot return parsed", any(x.get("amount_model")=="SELF_DAMAGE_SNAPSHOT_RETURN" and x.get("duration_seconds")==10.0 for x in hm), hm)

    sm=infer_shield_models("Gain a Shield equal to 0.8% of maximum Health per point of Energy.")
    check("Resource-scaled shield parsed", any(x.get("amount_model")=="MAX_HEALTH_PERCENT_PER_RESOURCE" and x.get("resource")=="ENERGY" and x.get("value")==0.8 for x in sm), sm)

    sm=infer_shield_models("Gain a 1296 Shield that decays over 6 seconds.")
    check("Decaying flat shield parsed", any(x.get("amount_model")=="FLAT" and x.get("decay_duration_seconds")==6.0 for x in sm), sm)

    sm=infer_shield_models("The Shield now decays over 4 seconds.")
    check("Inherited shield decay modifier parsed", any(x.get("amount_model")=="INHERIT_BASE_SHIELD" and x.get("decay_duration_seconds")==4.0 for x in sm), sm)

    sm=infer_shield_models('Gain a Shield that absorbs <c val="#TooltipNumbers">0.8%</c> of maximum Health per Energy consumed and lasts for <c val="#TooltipNumbers">3</c> seconds.')
    check("Tagged resource shield parsed", any(x.get("amount_model")=="MAX_HEALTH_PERCENT_PER_RESOURCE" and x.get("value")==0.8 for x in sm), sm)

    # --------------------------------------------------------
    # Universal spatial/subeffect invariants
    # --------------------------------------------------------
    spatial_total = 0
    spatial_unresolved = 0
    invalid_aoe = []
    for hero, rows in spatial_data.get("heroes", {}).items():
        for row in rows:
            spatial_total += 1
            if row.get("spatial_status") == "SPATIAL_UNRESOLVED":
                spatial_unresolved += 1
            if row.get("semantic_type") == "AOE" and row.get("radius") is None:
                invalid_aoe.append((hero, row.get("ability_id")))

    check("Spatial model non-empty", spatial_total > 0, spatial_total)
    check("AOE always has strong radius", not invalid_aoe, invalid_aoe[:30])

    sub_total = 0
    missing_primary = []
    for hero, rows in subeffect_data.get("heroes", {}).items():
        for row in rows:
            sub_total += 1
            base = row.get("base_subeffects") or []
            if not base or not any(x.get("node_id") == "primary" for x in base):
                missing_primary.append((hero, row.get("ability_id")))

    check("Subeffect graph covers all spatial records", sub_total == spatial_total, sub_total)
    check("Every ability has primary subeffect", not missing_primary, missing_primary[:30])

    # --------------------------------------------------------
    # v1.16 zero-manual spatial regressions
    # --------------------------------------------------------
    def spatial_row(hero_name, ability_id):
        for _row in spatial_data.get("heroes", {}).get(hero_name, []):
            if _row.get("ability_id") == ability_id:
                return _row
        return None

    etc = spatial_row("E.T.C.", "L90ETCPoweroftheHorde")
    check(
        "v1.16 ETC self-origin spatial derived",
        bool(etc) and etc.get("cast_range") == 0.0 and etc.get("radius") == 8.0
        and any(x.get("rule") == "SELF_INSTANT_ORIGIN" for x in (etc.get("derived_spatial_evidence") or [])),
        etc,
    )

    xul = spatial_row("Xul", "NecromancerPoisonNovaInstant")
    check(
        "v1.16 Xul Poison Nova radial geometry derived",
        bool(xul) and xul.get("shape") == "RADIAL_PROJECTILES"
        and xul.get("travel_range") == 16.0 and xul.get("projectile_radius") == 0.625
        and xul.get("radial_projectile_count") == 12,
        xul,
    )

    orb = spatial_row("Li-Ming", "WizardArcaneOrb")
    check(
        "v1.16 Li-Ming Arcane Orb range derived",
        bool(orb) and orb.get("range_model") == "CURSOR_GUIDE_ACCUMULATOR_RANGE"
        and orb.get("cast_range") == 13.5 and orb.get("travel_range") == 13.5
        and orb.get("max_damage_distance") == 9.0 and orb.get("damage_step_distance") == 0.5625
        and orb.get("radius") is None,
        orb,
    )

    manual_spatial = []
    for _hero, _rows in spatial_data.get("heroes", {}).items():
        for _row in _rows:
            if _row.get("override_source") == "MANUAL_VERIFIED":
                manual_spatial.append((_hero, _row.get("ability_id")))
    check("v1.16 no manual spatial records", not manual_spatial, manual_spatial[:30])


    report = {
        "summary": {
            "tests_total": len(tests),
            "passed": sum(1 for x in tests if x["passed"]),
            "failed": len(failures),
        },
        "tests": tests,
        "failures": failures,
    }

    save(report_path, report)

    if failures:
        raise RuntimeError(
            f"Regression checks failed: {len(failures)} / {len(tests)}"
        )

    return report
