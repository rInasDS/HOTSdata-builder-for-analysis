from __future__ import annotations

import re
from collections import Counter, defaultdict

from fact_utils import load, save
from combat_formula_model import ability_damage_models
from heal_model import infer_heal_models
from shield_model import infer_shield_models


CONTROL_TERMS = {
    "STUN": (r"\bstun(?:s|ned|ning)?\b",),
    "SLOW": (r"\bslow(?:s|ed|ing)?\b(?!\s+time)",),
    "ROOT": (r"\broot(?:s|ed|ing)?\b",),
    "SILENCE": (r"\bsilenc(?:e|es|ed|ing)\b",),
    "FEAR": (r"\bfear(?:s|ed|ing)?\b",),
    "TAUNT": (r"\btaunt(?:s|ed|ing)?\b",),
    "SLEEP": (r"\bsleep(?:s|ing)?\b", r"\bput .* to sleep\b"),
    "KNOCKBACK": (r"\bknock(?:s|ed|ing)?(?:\s+\w+){0,5}?\s+(?:back|away)\b", r"\bknockback\b"),
    "KNOCKUP": (r"\bknock(?:s|ed|ing)? up\b", r"\bknockup\b"),
    "PULL": (r"\bpull(?:s|ed|ing)?\b",),
}

PROTECTION_TERMS = {
    "UNSTOPPABLE": (r"\bunstoppable\b",),
    "INVULNERABLE": (r"\binvulnerable\b",),
    "STASIS": (r"\bstasis\b",),
    "PROTECTED": (r"\bprotected\b",),
    "CLEANSE": (r"\bcleanse\b", r"\bremove all (?:roots|slows|stuns|silences|disables)\b"),
}

DAMAGE_WORDS = (
    " deal ", " deals ", " dealing ", " damage", "damage ", "damages ",
    "burn ", "burns ", "explode", "explodes",
)
NON_DAMAGE_ACTION_HINTS=("cancel ","cancelled","toggle off","deactivate","state ")
HEAL_WORDS = (" heal ", " heals ", " healing ", "restore health", "restores health")
SHIELD_WORDS = (" shield", "shields ", "gain a shield", "grant a shield")

CONDITIONAL_PATTERNS = (
    r"\bif\b", r"\bwhile\b", r"\bwhen\b", r"\bafter\b", r"\bbefore\b",
    r"\bagainst\b", r"\bbelow\b", r"\babove\b", r"\bunder\b", r"\bover\b",
    r"\bat max\b", r"\bat least\b", r"\bfor each\b", r"\bevery time\b",
    r"\bonce\b", r"\buntil\b", r"\bafter hitting\b", r"\bafter damaging\b",
)

DAMAGE_METRICS = {
    "DAMAGE","DAMAGE_BONUS","BASIC_ATTACK_DAMAGE","SPELL_DAMAGE",
    "HERO_DAMAGE","PERCENT_MAX_HEALTH_DAMAGE","PERCENT_CURRENT_HEALTH_DAMAGE",
    "PERCENT_MISSING_HEALTH_DAMAGE","DAMAGE_OVER_TIME","PERIODIC_DAMAGE",
    "DAMAGE_TAKEN","ENEMY_DAMAGE_DEALT","SPELL_POWER","ARMOR","SPELL_ARMOR","DAMAGE_REDUCTION",
    "DAMAGE_TO_HEAL",
}
SURVIVABILITY_METRICS = {
    "ARMOR","SPELL_ARMOR","PHYSICAL_ARMOR","DAMAGE_REDUCTION",
    "HEALTH","MAX_HEALTH","HEAL","HEALING","SHIELD","SHIELD_AMOUNT",
}
RESOURCE_METRICS = {
    "MANA","MANA_COST","MANA_REGEN_RATE","HEALTH_REGEN_RATE","ENERGY_REGEN_RATE",
    "ENERGY","RAGE","FURY","RESOURCE",
}
SPATIAL_METRICS = {"RANGE","RADIUS","WIDTH","LENGTH","DURATION","SPEED","MOVE_SPEED"}
COOLDOWN_METRICS = {"COOLDOWN","COOLDOWN_REDUCTION","CHARGES","CHARGE","RECAST_COOLDOWN"}

INTERRUPT_CAPABLE = {"STUN","SILENCE","FEAR","TAUNT","SLEEP","KNOCKBACK","KNOCKUP","PULL","DISPLACEMENT"}


def _txt(row):
    return " ".join(str(row.get(k) or "") for k in ("name","short_text","full_text")).lower()


def _mechanics_txt(row):
    return " ".join(str(row.get(k) or "") for k in ("short_text","full_text")).lower()



def _plain(text):
    x=re.sub(r"<[^>]+>"," ",str(text or ""))
    return re.sub(r"\s+"," ",x).strip().lower()

def _strong_damage_modifier_language(text):
    t=_plain(text)
    pats=(
        r"(?:damage|spell power|attack damage)[^.;]{0,45}?(?:increase|increased|bonus|additional|reduced|decreased)[^.;]{0,35}?\d+(?:\.\d+)?%",
        r"(?:increase|increases|increased|reduce|reduces|reduced)[^.;]{0,70}?(?:damage|spell power|attack damage)[^.;]{0,30}?\d+(?:\.\d+)?%",
        r"(?:deal|deals|dealing|does|splashes?)[^.;]{0,55}?\d+(?:\.\d+)?%[^.;]{0,35}?(?:damage|maximum health)",
        r"\d+(?:\.\d+)?%[^.;]{0,30}?(?:more|additional|bonus|increased|less)\s+(?:basic attack |spell |ability )?damage",
        r"damage equal to\s+\d+(?:\.\d+)?%",
        r"damage[^.;]{0,35}?to\s+\d+(?:\.\d+)?%\s+of",
        r"damage[^.;]{0,35}?from\s+\d+(?:\.\d+)?%\s+to\s+\d+(?:\.\d+)?%",
        r"\d+(?:\.\d+)?%\s+maximum health\s+as damage",
    )
    return any(re.search(p,t,re.I) for p in pats)

def _qualitative_or_nonoffensive_damage_text(text):
    t=_plain(text)
    # These describe damage as a trigger/resource/defensive concept, not an offensive modifier.
    if re.search(r"(?:taking|take|takes) damage|damage received|damage blocked|dealing damage (?:with|to) .*? grants|gained by .*? dealing",t,re.I) and not _strong_damage_modifier_language(t):
        return True
    if re.search(r"causes? .*? to damage all enemies|creates? .*? on the target|deals increased damage",t,re.I) and not re.search(r"\d+(?:\.\d+)?%[^.;]{0,35}damage|damage[^.;]{0,35}\d+(?:\.\d+)?%",t,re.I):
        return True
    return False

def _contains_any(text, words):
    return any(w in text for w in words)


def _explicit_damage_action_text(text):
    t=" "+text.lower()+" "
    if any(x in t for x in NON_DAMAGE_ACTION_HINTS) and not re.search(r"\b(?:deal|deals|dealing|burn|burns|explode|explodes)\b",t):
        return False
    return bool(re.search(r"\b(?:deal|deals|dealing)\b[^.;]{0,80}\bdamage\b|\b(?:burn|burns|explode|explodes)\b",t,re.I))


def _talent_status_references_as_condition(text):
    return bool(re.search(r"\b(?:against|while|if|when|being|that is|that are|who is|who are|enemies that are|heroes that are)\b[^.;]{0,120}\b(?:stunned|rooted|slowed|silenced|feared|taunted|asleep)\b",text,re.I))

def _regex_labels(text, mapping):
    out=[]
    for label, pats in mapping.items():
        if any(re.search(p,text,re.I) for p in pats):
            out.append(label)
    return sorted(set(out))


def _combat_damage(combat):
    return bool((combat or {}).get("damage"))


def _combat_heal(combat):
    return bool((combat or {}).get("heal"))


def _combat_any_heal(combat):
    c=combat or {}
    return bool(c.get("heal") or c.get("conditional_heal"))


def _combat_shield(combat):
    return bool((combat or {}).get("shield_candidates"))


def _combat_any_shield(combat):
    c=combat or {}
    return bool(c.get("shield_candidates") or c.get("conditional_shield_candidates"))


def _heal_text_role(text):
    t=_plain(text)
    models=infer_heal_models(text,"AUDIT_TEXT")
    if models:
        model_kinds={str(m.get("amount_model") or "") for m in models}
        if model_kinds & {"DAMAGE_DEALT_PERCENT","BASIC_ATTACK_DAMAGE_PERCENT","DAMAGE_TAKEN_PERCENT","DAMAGE_ABSORBED_PERCENT","DAMAGE_TAKEN_CONVERSION_PERCENT"}:
            return "DAMAGE_TO_HEAL",models
        if "RESOURCE_SPENT_1_TO_1" in model_kinds:
            return "RESOURCE_TO_HEAL",models
        return "CREATES_HEAL",models
    if re.search(r"\b(?:reduce|reduces|reduced|decrease|decreases|increase|increases|increased|more|less)\b[^.;]{0,80}\b(?:self-?healing|healing(?: received| done)?)\b|\b(?:self-?healing|healing received)\b[^.;]{0,60}\b(?:reduced|increased)\b",t,re.I):
        return "HEAL_MODIFIER",[]
    if re.search(r"\bhealing\s+(?:a|an|the)\b[^.;]{0,100}\b(?:reduces|increases|grants|causes)\b",t,re.I):
        return "HEAL_TRIGGER",[]
    if re.search(r"\bcan be healed by\b|\bhealing (?:abilities|ability)\b|\bhealing boost\b|\bhealing fountain\b",t,re.I):
        return "HEAL_STATE_OR_REFERENCE",[]
    if re.search(r"\bcancel\b|\bstop channeling\b|\bcharges? of healing|\breset the cooldown of .*?heal|\bnext cast heals?[^.;]{0,40}% more",t,re.I):
        return "HEAL_CONTROL_OR_REFERENCE",[]
    # Restore/regain without an explicit Life/Health object is usually Mana/Energy/resource recovery.
    if re.search(r"\b(?:heal|heals|healed|healing|life steal)\b",t,re.I):
        return "POSITIVE_HEAL_UNRESOLVED",[]
    # Unqualified resource restore (Mana/Energy) is not healing. Explicit numeric
    # Health/Life restoration is handled by infer_heal_models above.
    return "NONE",[]


def _shield_text_role(text):
    t=_plain(text)
    models=infer_shield_models(text,"AUDIT_TEXT")
    if models:
        kinds={str(m.get("effect_class") or "") for m in models}
        if "SHIELD_RESTORE" in kinds or "SHIELD_REGEN" in kinds:
            return "SHIELD_RESTORE_OR_REGEN",models
        if any(m.get("source_event")=="DAMAGE_DEALT" for m in models):
            return "DAMAGE_TO_SHIELD",models
        return "CREATES_SHIELD",models
    if re.search(r"\b(?:reset|reduce|reduces|lower|lowers)\b[^.;]{0,60}\bshield\b[^.;]{0,40}\bcooldown|\bcooldown\b[^.;]{0,40}\bshield",t,re.I):
        return "SHIELD_COOLDOWN_MODIFIER",[]
    if re.search(r"\b(?:damage|destroy|shatter|remove|break)[^.;]{0,60}\b(?:enemy )?shields?\b|\bshields?\b[^.;]{0,40}\b(?:destroyed|damage)\b",t,re.I):
        return "SHIELD_DAMAGE_OR_REMOVE",[]
    if re.search(r"\b(?:spell|greater spell|spirit) shield\b[^.;]{0,80}\b(?:disabled|damage reduction|ability damage)\b|\bshield is disabled\b",t,re.I):
        return "DAMAGE_REDUCTION_TOGGLE",[]
    if re.search(r"\bdamage absorbed by .*?shields?\b|\bshields? absorb",t,re.I):
        return "SHIELD_TRIGGER_RESOURCE",[]
    if re.search(r"\b(?:lose|sacrifice|consume)\b[^.;]{0,50}\bshields?\b",t,re.I):
        return "SHIELD_COST_OR_MODIFIER",[]
    if re.search(r"\bcancel\b",t,re.I):
        return "SHIELD_CONTROL_OR_REFERENCE",[]
    if re.search(r"\bcan\s+(?:grant|gain|create|provide)\b[^.;]{0,80}\bshields?\b",t,re.I):
        return "SHIELD_SUBABILITY_REFERENCE",[]
    # Names such as Shield Glare / Lightning Shield are not evidence of an absorb shield.
    if re.search(r"\b(?:gain|grant|grants|receive|receives|create|creates|regain|regains|restore|restores|recharge|recharges|has a permanent)\b[^.;]{0,80}\bshields?\b",t,re.I):
        return "POSITIVE_SHIELD_UNRESOLVED",[]
    return "MENTION_ONLY",[]


def _severity(issue_type):
    if issue_type in {
        "DAMAGE_SEMANTIC_MISSING","DAMAGE_CONTRADICTION",
        "CONTROL_SEMANTIC_MISSING","INTERRUPT_CONTRADICTION",
        "SHIELD_CONTRADICTION","HEAL_CONTRADICTION",
        "TALENT_DAMAGE_MODIFIER_MISSING","TALENT_RUNTIME_CONDITION_MISSING",
    }:
        return "CRITICAL"
    if issue_type in {
        "PROTECTION_SEMANTIC_MISSING","HEAL_SEMANTIC_MISSING",
        "SHIELD_SEMANTIC_MISSING","SHIELD_MODEL_MISSING","HEAL_MODEL_MISSING",
        "TALENT_CONTROL_OVERLAY_MISSING",
    }:
        return "HIGH"
    return "MEDIUM"


def _priority(sev):
    return {"CRITICAL":900,"HIGH":700,"MEDIUM":500,"LOW":200}.get(sev,100)


def _issue(hero, ability, issue_type, details=None):
    sev=_severity(issue_type)
    return {
        "hero":hero,
        "ability_id":ability.get("id") or ability.get("ability_id"),
        "ability_name":ability.get("name"),
        "slot":ability.get("slot"),
        "issue_type":issue_type,
        "severity":sev,
        "priority":_priority(sev),
        "details":details or {},
    }


def _modifier_metric(mod):
    v=mod.get("value")
    return str(v.get("metric") or "").upper() if isinstance(v,dict) else ""


def _all_talent_overlays(ability):
    sem=ability.get("resolver_semantics") or {}
    return sem.get("talent_combat_overlays") or []


def _ability_audit(hero, ab, peer_caps=None):
    issues=[]
    sem=ab.get("resolver_semantics") or {}
    combat=ab.get("combat") or {}
    sub=ab.get("subeffects") or {}
    text=_txt(ab)

    base_effects=set(sem.get("base_effects") or [])
    statuses=set(sem.get("status_effects") or [])
    protection=set(sem.get("protection") or [])
    interrupt=set(sem.get("interrupt_capable") or [])

    has_damage=bool(sem.get("has_damage"))
    damage_models=sem.get("damage_models") or []
    has_heal=bool(sem.get("has_heal"))
    has_shield=bool(sem.get("has_shield"))

    direct_damage=_combat_damage(combat)
    direct_heal=_combat_heal(combat)
    any_typed_heal=_combat_any_heal(combat)
    direct_shield=_combat_shield(combat)
    any_typed_shield=_combat_any_shield(combat)
    peer_caps=peer_caps or {}
    shield_models=sem.get("shield_models") or []
    heal_models=sem.get("heal_models") or []

    if direct_damage and not has_damage:
        issues.append(_issue(hero,ab,"DAMAGE_CONTRADICTION",{"combat_damage_records":len(combat.get("damage") or [])}))
    if has_damage and not (direct_damage or "DAMAGE" in base_effects):
        issues.append(_issue(hero,ab,"DAMAGE_CONTRADICTION",{"reason":"resolver_has_damage_without_direct_or_base_damage"}))
    if _explicit_damage_action_text(text) and not has_damage:
        # Text-only damage is useful as a review signal, not proof.
        issues.append(_issue(hero,ab,"DAMAGE_SEMANTIC_MISSING",{"text_excerpt":text[:300]}))

    if direct_heal and not has_heal:
        issues.append(_issue(hero,ab,"HEAL_CONTRADICTION",{"combat_heal_records":len((combat.get("heal") or []))+len((combat.get("conditional_heal") or []))}))
    heal_role,heal_candidates=_heal_text_role(text)
    peer_has_heal=bool(peer_caps.get("has_heal"))
    if heal_role in {"CREATES_HEAL","DAMAGE_TO_HEAL","RESOURCE_TO_HEAL","POSITIVE_HEAL_UNRESOLVED"} and not has_heal and not any_typed_heal and not peer_has_heal:
        issues.append(_issue(hero,ab,"HEAL_SEMANTIC_MISSING",{
            "semantic_role":heal_role,"candidate_models":heal_candidates,"text_excerpt":text[:300]}))
    if has_heal and not heal_models and not any_typed_heal:
        issues.append(_issue(hero,ab,"HEAL_MODEL_MISSING",{"reason":"heal_without_formula_or_direct_xml","semantic_role":heal_role}))

    if has_shield and not (shield_models or direct_shield):
        issues.append(_issue(hero,ab,"SHIELD_CONTRADICTION",{"reason":"has_shield_without_model_or_direct_evidence"}))
    if shield_models and not has_shield:
        issues.append(_issue(hero,ab,"SHIELD_CONTRADICTION",{"reason":"shield_models_exist_but_has_shield_false"}))
    shield_role,shield_candidates=_shield_text_role(text)
    peer_has_shield=bool(peer_caps.get("has_shield"))
    if shield_role in {"CREATES_SHIELD","DAMAGE_TO_SHIELD","SHIELD_RESTORE_OR_REGEN","POSITIVE_SHIELD_UNRESOLVED"} and not has_shield and not any_typed_shield and not peer_has_shield:
        issues.append(_issue(hero,ab,"SHIELD_SEMANTIC_MISSING",{
            "semantic_role":shield_role,"candidate_models":shield_candidates,"text_excerpt":text[:300]}))
    if has_shield and not shield_models and not any_typed_shield:
        issues.append(_issue(hero,ab,"SHIELD_MODEL_MISSING",{"direct_shield_evidence":direct_shield,"semantic_role":shield_role}))

    expected_status=set(_regex_labels(_mechanics_txt(ab),CONTROL_TERMS))
    missing_status=sorted(expected_status-statuses)
    if missing_status:
        issues.append(_issue(hero,ab,"CONTROL_SEMANTIC_MISSING",{
            "expected_from_text":missing_status,
            "resolver_status_effects":sorted(statuses),
            "text_excerpt":text[:350],
        }))

    expected_protection=set(_regex_labels(text,PROTECTION_TERMS))
    missing_protection=sorted(expected_protection-protection)
    if missing_protection:
        issues.append(_issue(hero,ab,"PROTECTION_SEMANTIC_MISSING",{
            "expected_from_text":missing_protection,
            "resolver_protection":sorted(protection),
            "text_excerpt":text[:350],
        }))

    bad_interrupt=sorted(interrupt-statuses)
    if bad_interrupt:
        issues.append(_issue(hero,ab,"INTERRUPT_CONTRADICTION",{
            "interrupt_capable_not_in_status_effects":bad_interrupt,
        }))
    non_interrupt=sorted(interrupt-INTERRUPT_CAPABLE)
    if non_interrupt:
        issues.append(_issue(hero,ab,"INTERRUPT_CONTRADICTION",{
            "unsupported_interrupt_categories":non_interrupt,
        }))

    # Talent overlays already bound to this ability.
    for ov in _all_talent_overlays(ab):
        mods=ov.get("modifiers") or []
        rules=ov.get("runtime_rules") or {}
        phrase=" ".join(
            str((m.get("provenance") or {}).get("phrase") or "") for m in mods
        ).lower()
        metrics={_modifier_metric(m) for m in mods if _modifier_metric(m)}
        has_runtime=any(rules.get(k) for k in (
            "triggers","conditions","temporal_rules","counter_rules","internal_cooldown","quest"
        ))
        conditional=any(re.search(p,phrase,re.I) for p in CONDITIONAL_PATTERNS)
        if conditional and metrics and not has_runtime:
            issues.append(_issue(hero,ab,"TALENT_RUNTIME_CONDITION_MISSING",{
                "talent_id":ov.get("talent_id"),
                "talent_name":ov.get("name"),
                "metrics":sorted(metrics),
                "phrase":phrase[:450],
            }))

    return issues


def _talent_text_audit(hero, hero_row):
    """
    Finds talent text that clearly describes combat/control semantics but is not
    represented by any resolver overlay attached to this hero's abilities.
    """
    issues=[]
    ability_overlays=defaultdict(list)
    for ab in hero_row.get("abilities",[]) or []:
        for ov in _all_talent_overlays(ab):
            if ov.get("talent_id"):
                ability_overlays[str(ov["talent_id"])].append((ab,ov))

    for t in (hero_row.get("talent_facts") or hero_row.get("talents") or []):
        tid=str(t.get("talent_id") or "")
        text=_txt(t)
        ovs=ability_overlays.get(tid,[])
        own_sem=t.get("resolver_semantics") or {}
        overlay_mods=[m for _,ov in ovs for m in (ov.get("modifiers") or [])]
        overlay_mods += list(own_sem.get("combat_modifiers") or [])
        metrics={_modifier_metric(m) for m in overlay_mods if _modifier_metric(m)}
        status_words=set(_regex_labels(_mechanics_txt(t),CONTROL_TERMS))

        # Strong numeric damage-modifier language only. Nearby range/duration numbers
        # must not create false DAMAGE gaps.
        damage_modifier_language=_strong_damage_modifier_language(text) and not _qualitative_or_nonoffensive_damage_text(text)
        damage_formulas=own_sem.get("damage_formulas") or []
        heal_ratio_only=bool(re.search(r"heal(?:s|ing)?[^.;]{0,90}?%\s+of(?: the)? damage dealt",text,re.I)) and not re.search(r"(?:increase|bonus|additional|more)[^.;]{0,50}?damage",text,re.I)
        if damage_modifier_language and not (metrics & DAMAGE_METRICS) and not damage_formulas and not heal_ratio_only:
            fake_ab={"id":t.get("ability_id"),"name":t.get("name"),"slot":t.get("ability_type")}
            issues.append(_issue(hero,fake_ab,"TALENT_DAMAGE_MODIFIER_MISSING",{
                "talent_id":tid,
                "talent_name":t.get("name"),
                "overlay_metrics":sorted(metrics),
                "text_excerpt":text[:450],
            }))

        # Talent grants/modifies/removes control. Check TALENT overlays, not
        # unconditional base status. Pure target-state conditions are not granted CC.
        status_trigger_only=bool(re.search(r"\b(?:pulling|rooted|slowed|stunned|silenced)\b[^.;]{0,80}?\b(?:reduces|increases|grants|causes|take|takes)\b",text,re.I))
        if status_words and not _talent_status_references_as_condition(text) and not status_trigger_only and not re.search(r"\b(?:no longer|not)\s+(?:slowed|rooted|stunned|silenced)\b",text,re.I):
            represented=set(); removed=set()
            for so in own_sem.get("status_overlays") or []:
                st=str(so.get("status") or "").upper()
                if so.get("operation")=="REMOVE": removed.add(st)
                else: represented.add(st)
            for ab in hero_row.get("abilities",[]) or []:
                sem=ab.get("resolver_semantics") or {}
                for so in sem.get("talent_status_overlays") or []:
                    if str(so.get("talent_id"))!=tid: continue
                    st=str(so.get("status") or "").upper()
                    if so.get("operation")=="REMOVE": removed.add(st)
                    else: represented.add(st)
            if not (status_words & (represented|removed)):
                fake_ab={"id":t.get("ability_id"),"name":t.get("name"),"slot":t.get("ability_type")}
                issues.append(_issue(hero,fake_ab,"TALENT_CONTROL_OVERLAY_MISSING",{
                    "talent_id":tid,
                    "talent_name":t.get("name"),
                    "expected_status":sorted(status_words),
                    "represented_status":sorted(represented),
                    "removed_status":sorted(removed),
                    "text_excerpt":text[:450],
                }))
    return issues


def classify_existing_review(entry):
    blob=str(entry).upper()
    if any(x in blob for x in ("DAMAGE","SPELL_POWER","ATTACK_DAMAGE","PERCENT_MAX_HEALTH")):
        return "DAMAGE_CRITICAL"
    if any(x in blob for x in ("STUN","SILENCE","ROOT","FEAR","TAUNT","SLEEP","KNOCK","PULL")):
        return "CONTROL_CRITICAL"
    if any(x in blob for x in ("SHIELD","ARMOR","INVULNERABLE","UNSTOPPABLE","PROTECTED","HEAL")):
        return "SURVIVABILITY"
    if any(x in blob for x in ("MANA","ENERGY","RAGE","RESOURCE","REGEN")):
        return "RESOURCE"
    if any(x in blob for x in ("COOLDOWN","CHARGE","RESET")):
        return "COOLDOWN"
    if any(x in blob for x in ("QUEST","STACK","COUNTER","REQUIRED_HITS","THRESHOLD")):
        return "QUEST_RUNTIME"
    if any(x in blob for x in ("RANGE","RADIUS","WIDTH","DURATION","SPEED")):
        return "SPATIAL"
    return "OTHER"


def run_combat_semantics_audit(final_db_path, review_queue_path, report_path, triage_path):
    db=load(final_db_path)
    issues=[]
    ability_count=0
    talent_count=0

    semantic_counts=Counter()
    status_counts=Counter()
    shield_models=Counter()
    damage_formula_models=Counter()
    damage_sources=Counter()
    heal_text_roles=Counter(); shield_text_roles=Counter()

    for hero, hr in (db.get("heroes") or {}).items():
        peer_by_id=defaultdict(lambda:{"has_heal":False,"has_shield":False})
        for _ab in hr.get("abilities",[]) or []:
            _aid=str(_ab.get("ability_id") or _ab.get("id") or "")
            _sem=_ab.get("resolver_semantics") or {}; _combat=_ab.get("combat") or {}
            peer_by_id[_aid]["has_heal"] = peer_by_id[_aid]["has_heal"] or bool(_sem.get("has_heal") or _combat_any_heal(_combat))
            peer_by_id[_aid]["has_shield"] = peer_by_id[_aid]["has_shield"] or bool(_sem.get("has_shield") or _combat_any_shield(_combat))
        for ab in hr.get("abilities",[]) or []:
            ability_count+=1
            sem=ab.get("resolver_semantics") or {}
            semantic_counts["has_damage"]+=int(bool(sem.get("has_damage")))
            semantic_counts["has_heal"]+=int(bool(sem.get("has_heal")))
            semantic_counts["has_shield"]+=int(bool(sem.get("has_shield")))
            for s in sem.get("status_effects") or []:
                status_counts[s]+=1
            for sm in sem.get("shield_models") or []:
                shield_models[str(sm.get("amount_model") or "UNKNOWN")]+=1
            for dm in sem.get("damage_models") or []:
                damage_formula_models[str(dm.get("formula_model") or "UNKNOWN")]+=1
                damage_sources[str(dm.get("source_type") or "UNKNOWN")]+=1
            _hr,_=_heal_text_role(_txt(ab)); _sr,_=_shield_text_role(_txt(ab))
            heal_text_roles[_hr]+=1; shield_text_roles[_sr]+=1
            _aid=str(ab.get("ability_id") or ab.get("id") or "")
            issues.extend(_ability_audit(hero,ab,peer_by_id.get(_aid)))

        talent_count+=len(hr.get("talents",[]) or [])
        issues.extend(_talent_text_audit(hero,hr))

    # Deduplicate stable key.
    seen=set(); unique=[]
    for x in issues:
        key=(x.get("hero"),x.get("ability_id"),x.get("issue_type"),str(x.get("details")))
        if key in seen: continue
        seen.add(key); unique.append(x)
    issues=unique
    issues.sort(key=lambda x:(x["priority"],x["hero"],str(x.get("ability_id"))),reverse=True)

    issue_counts=Counter(x["issue_type"] for x in issues)
    severity_counts=Counter(x["severity"] for x in issues)

    report={
        "summary":{
            "heroes":len(db.get("heroes") or {}),
            "abilities":ability_count,
            "talents":talent_count,
            "issues":len(issues),
            "critical":severity_counts.get("CRITICAL",0),
            "high":severity_counts.get("HIGH",0),
            "medium":severity_counts.get("MEDIUM",0),
            "semantic_counts":dict(semantic_counts),
            "status_counts":dict(status_counts),
            "shield_amount_models":dict(shield_models),
            "damage_formula_models":dict(damage_formula_models),
            "damage_sources":dict(damage_sources),
            "heal_text_roles":dict(heal_text_roles),
            "shield_text_roles":dict(shield_text_roles),
            "issue_counts":dict(issue_counts),
        },
        "critical_for_replay_resolver":[x for x in issues if x["severity"]=="CRITICAL"][:2000],
        "high_priority":[x for x in issues if x["severity"]=="HIGH"][:2000],
        "all_issues":issues[:10000],
    }
    save(report_path,report)

    triage={"summary":{},"categories":{}}
    try:
        rq=load(review_queue_path)
        rows=rq if isinstance(rq,list) else rq.get("items") or rq.get("review_queue") or rq.get("rows") or []
        cats=defaultdict(list)
        for row in rows:
            cats[classify_existing_review(row)].append(row)
        triage["summary"]={k:len(v) for k,v in sorted(cats.items())}
        triage["categories"]={k:v[:2000] for k,v in cats.items()}
    except Exception as e:
        triage["error"]=repr(e)
    save(triage_path,triage)

    return report
