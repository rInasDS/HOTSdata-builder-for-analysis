from __future__ import annotations

import html
import json
import re
from collections import Counter, defaultdict

from fact_utils import load, save
from xml_semantics import descendants, const_value
from combat_formula_model import enrich_modifier_damage_semantics


# ============================================================
# TEXT NORMALIZATION
# ============================================================

TAG_RE = re.compile(r"<[^>]+>")
COLOR_TAG_RE = re.compile(r"</?c(?:\s+[^>]*)?>", re.I)
NEWLINE_TAG_RE = re.compile(r"<n\s*/?>", re.I)
BR_TAG_RE = re.compile(r"<br\s*/?>", re.I)
WS_RE = re.compile(r"\s+")
SCALING_MARKER_RE = re.compile(r"(?:\s*)~~\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*~~")

NUM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])([0-9]+(?:\.[0-9]+)?)(%|s|sec|secs|second|seconds)?",
    re.I,
)

EFFECT_AND_SPLIT_RE = re.compile(
    r"\s+and\s+(?=(?:"
    r"stun|disable|deal|heal|grant|gain|increase|reduce|slow|root|silence|blind|"
    r"restore|refund|reset|spawn|summon|apply|cause|deal|basic attacks?|attacks?"
    r")\b)",
    re.I,
)


def normalize_tooltip(text):
    s = html.unescape(str(text or ""))
    s = NEWLINE_TAG_RE.sub(". ", s)
    s = BR_TAG_RE.sub(". ", s)
    s = COLOR_TAG_RE.sub("", s)
    s = TAG_RE.sub("", s)
    s = SCALING_MARKER_RE.sub("", s)
    s = WS_RE.sub(" ", s)
    s = re.sub(r"(?:\.\s*){2,}", ". ", s)
    return s.strip()


def dedupe_text_parts(parts):
    out = []
    seen = set()

    for part in parts:
        p = normalize_tooltip(part)
        key = re.sub(r"\s+", " ", p.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(p)

    return out


def merged_talent_text(talent):
    # Full text normally subsumes short text. Keep short text only if it adds
    # materially different words.
    full = normalize_tooltip(talent.get("full_text"))
    short = normalize_tooltip(talent.get("short_text"))
    name = normalize_tooltip(talent.get("name"))

    parts = [name]
    if short and short.lower() not in full.lower():
        parts.append(short)
    if full:
        parts.append(full)

    return " ".join(dedupe_text_parts(parts))


def sentence_phrases(text):
    """
    Split text into mechanic-sized phrases. Do not globally split every "and";
    only split when the next token begins another known effect verb.
    """
    out = []
    for sentence in re.split(r"(?<=[.!?;])\s+", normalize_tooltip(text)):
        sentence = sentence.strip(" .;")
        if not sentence:
            continue

        pieces = EFFECT_AND_SPLIT_RE.split(sentence)
        for piece in pieces:
            piece = piece.strip(" ,.;")
            if piece:
                out.append(piece)

    return out


# ============================================================
# FACT HELPERS
# ============================================================

def fact(kind, value=None, confidence="MEDIUM", provenance=None, **extra):
    out = {
        "kind": kind,
        "confidence": confidence,
        "provenance": provenance or {},
    }
    if value is not None:
        out["value"] = value
    out.update(extra)
    return out


def prov_text(phrase=None):
    p = {
        "source_type": "HERODATA_TEXT_INFERRED",
        "field": "full_text",
    }
    if phrase:
        p["phrase"] = phrase
    return p


def semantic_key(item):
    kind = item.get("kind")
    value = item.get("value")

    if kind == "STATUS_EFFECT":
        return (
            kind,
            value,
            item.get("duration_seconds"),
            item.get("amount_percent"),
            tuple(sorted(item.get("target_filter") or [])),
        )

    if kind == "MODIFIER" and isinstance(value, dict):
        return (
            kind,
            value.get("metric"),
            value.get("operation"),
            value.get("value"),
            value.get("from"),
            value.get("to"),
            tuple(sorted(item.get("target_filter") or [])),
        )

    if kind == "TEMPORAL_RULE":
        return (
            kind,
            value,
            item.get("duration_seconds"),
            item.get("period_seconds"),
            tuple(sorted(item.get("target_filter") or [])),
        )

    if kind == "INTERNAL_COOLDOWN":
        return (
            kind,
            item.get("value"),
            item.get("per_target"),
            tuple(sorted(item.get("target_filter") or [])),
        )

    return json.dumps(
        {k: v for k, v in item.items() if k != "provenance"},
        sort_keys=True,
        ensure_ascii=False,
    )


def semantic_dedupe(items):
    seen = set()
    out = []
    for item in items:
        k = semantic_key(item)
        if k not in seen:
            seen.add(k)
            out.append(item)
    return out


# ============================================================
# TALENT TARGET CLASSIFICATION
# ============================================================

RE_BASIC_ATTACK = re.compile(r"\bbasic attacks?\b|\bauto[- ]?attacks?\b", re.I)
RE_SUMMON = re.compile(r"\bsummoned units?\b|\bsummons?\b|\bminions?\b|\blocusts?\b|\bbeetles?\b", re.I)
RE_NEW_ACTIVE = re.compile(r"\bactivate to\b|\bactive:\b|\bcan be activated\b", re.I)
RE_RESOURCE = re.compile(r"\bmana\b|\benergy\b|\bfury\b|\bresource\b", re.I)
RE_HERO_STAT = re.compile(
    r"\b(?:maximum health|max health|health regeneration|mana regeneration|armor|spell power|movement speed|attack speed)\b",
    re.I,
)


def base_targets(talent):
    status = talent.get("status")
    families = talent.get("canonical_ability_families") or []

    if status == "LINKED" and len(families) == 1:
        return [{"type": "ABILITY", "id": families[0], "confidence": "HIGH",
                 "provenance": {"source_type": "TALENT_FAMILY_RESOLVER"}}]

    if status == "MULTI_ABILITY":
        return [{"type": "ABILITY", "id": fam, "confidence": "HIGH",
                 "provenance": {"source_type": "TALENT_FAMILY_RESOLVER"}}
                for fam in families]

    if status == "GRANTS_ACTIVE":
        return [{"type": "NEW_ACTIVE", "id": talent.get("talent_id"), "confidence": "HIGH",
                 "provenance": {"source_type": "TALENT_FAMILY_RESOLVER"}}]

    if status == "PASSIVE_MODIFIER":
        return [{"type": "HERO_GLOBAL", "id": None, "confidence": "MEDIUM",
                 "provenance": {"source_type": "TALENT_FAMILY_RESOLVER"}}]

    return [{"type": "UNKNOWN", "id": None, "confidence": "LOW",
             "provenance": {"source_type": "TALENT_FAMILY_RESOLVER"}}]


def semantic_correct_targets(talent, targets, text):
    corrections = []

    def replace(target_type, reason, confidence="HIGH"):
        corrections.append({"from": targets, "to": target_type, "reason": reason})
        return [{
            "type": target_type,
            "id": talent.get("talent_id") if target_type == "NEW_ACTIVE" else None,
            "confidence": confidence,
            "provenance": prov_text(),
        }], corrections

    can_override = talent.get("status") in {"ORPHAN", "PASSIVE_MODIFIER"} or all(
        t.get("type") in {"HERO_GLOBAL", "UNKNOWN"} for t in targets
    )

    if can_override and RE_BASIC_ATTACK.search(text):
        return replace("BASIC_ATTACK", "TOOLTIP_EXPLICIT_BASIC_ATTACK")

    if can_override and RE_SUMMON.search(text):
        return replace("SUMMONED_UNIT", "TOOLTIP_EXPLICIT_SUMMON", "MEDIUM")

    if talent.get("status") in {"ORPHAN", "PASSIVE_MODIFIER"} and RE_NEW_ACTIVE.search(text):
        return replace("NEW_ACTIVE", "TOOLTIP_EXPLICIT_ACTIVE", "MEDIUM")

    if can_override and RE_RESOURCE.search(text):
        return replace("HERO_RESOURCE_SYSTEM", "TOOLTIP_RESOURCE_SYSTEM", "MEDIUM")

    if can_override and RE_HERO_STAT.search(text):
        return replace("HERO_STATS", "TOOLTIP_HERO_STATS", "MEDIUM")

    return targets, corrections


def final_classification(targets):
    types = {t.get("type") for t in targets}

    mapping = {
        frozenset({"BASIC_ATTACK"}): "BASIC_ATTACK_MODIFIER",
        frozenset({"SUMMONED_UNIT"}): "SUMMON_MODIFIER",
        frozenset({"NEW_ACTIVE"}): "NEW_ACTIVE",
        frozenset({"HERO_RESOURCE_SYSTEM"}): "RESOURCE_MODIFIER",
        frozenset({"HERO_STATS"}): "STAT_MODIFIER",
        frozenset({"HERO_GLOBAL"}): "SYSTEM_MODIFIER",
    }

    if frozenset(types) in mapping:
        return mapping[frozenset(types)]

    if "UNKNOWN" in types:
        return "UNRESOLVED"

    if types == {"ABILITY"} and len(targets) == 1:
        return "ABILITY_LINKED"

    if "ABILITY" in types and len(targets) > 1:
        return "MULTI_ABILITY"

    return "SYSTEM_MODIFIER"


# ============================================================
# EFFECT-SCOPED TARGET FILTERS
# ============================================================

FILTER_PATTERNS = [
    (re.compile(r"\bhero(?:es)?\b", re.I), "HERO"),
    (re.compile(r"\bstructures?\b", re.I), "STRUCTURE"),
    (re.compile(r"\bnon-heroes?\b", re.I), "NON_HERO"),
    (re.compile(r"\bnon[- ]elite(?: enemies?)?\b", re.I), "NON_ELITE"),
    (re.compile(r"(?<!non[- ])\belite enemies?\b", re.I), "ELITE"),
    (re.compile(r"\bminions?\b", re.I), "MINION"),
    (re.compile(r"\bmercenar(?:y|ies)\b", re.I), "MERCENARY"),
    (re.compile(r"\bsummoned units?\b|\bsummons?\b", re.I), "SUMMONED_UNIT"),
    (re.compile(r"\bmonsters?\b", re.I), "MONSTER"),
    (re.compile(r"\ballied hero(?:es)?\b|\ballies\b|\ballied\b", re.I), "ALLY"),
    (re.compile(r"\benemy hero(?:es)?\b|\benemies\b|\benemy\b", re.I), "ENEMY"),
]


def target_filters_for_phrase(phrase):
    found = []
    for rx, name in FILTER_PATTERNS:
        if rx.search(phrase):
            found.append(name)

    # NON_ELITE implies not ELITE.
    if "NON_ELITE" in found and "ELITE" in found:
        found.remove("ELITE")

    return sorted(set(found))


# ============================================================
# TRIGGERS / CONDITIONS / TEMPORAL
# ============================================================

EVERY_N_ATTACKS_RE = re.compile(r"\bevery\s+(\d+)(?:st|nd|rd|th)?\s+(?:basic\s+)?attacks?\b", re.I)
EVERY_N_HITS_RE = re.compile(r"\bevery\s+(\d+)(?:st|nd|rd|th)?\s+(?:hero\s+)?hits?\b", re.I)
LOW_HEALTH_RE = re.compile(r"\b(?:below|under|at or below)\s+(\d+(?:\.\d+)?)%\s+(?:health|maximum health|max health)\b", re.I)
WITHOUT_DYING_RE = re.compile(r"\bwithout dying\b", re.I)

PER_TARGET_CD_RES = [
    re.compile(r"\b(\d+(?:\.\d+)?)\s+seconds?\s+cooldown\s+per\s+target\b", re.I),
    re.compile(r"\bhas a\s+(\d+(?:\.\d+)?)\s+second\s+cooldown\s+against\s+each\s+target\b", re.I),
    re.compile(r"\bcan only occur once every\s+(\d+(?:\.\d+)?)\s+seconds?\s+per\s+target\b", re.I),
    re.compile(r"\bthe same (?:hero|structure|target).{0,100}?once every\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I),
    re.compile(r"\beach (?:hero|structure|target).{0,100}?once every\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I),
]
GENERIC_INTERNAL_CD_RES = [
    re.compile(r"\bcan only (?:trigger|occur|activate) once every\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I),
    re.compile(r"\bonce every\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I),
    re.compile(r"\bthis effect has a\s+(\d+(?:\.\d+)?)\s+second\s+cooldown\b", re.I),
]
PERIOD_RE = re.compile(r"\bevery\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
DURATION_RE = re.compile(r"\b(?:for|over)\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
DELAY_RE = re.compile(r"\bafter\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)


def infer_triggers(text):
    out = []

    m = EVERY_N_ATTACKS_RE.search(text)
    if m:
        out.append(fact("TRIGGER", "ON_NTH_BASIC_ATTACK", "HIGH", prov_text(), n=int(m.group(1))))
    elif RE_BASIC_ATTACK.search(text):
        out.append(fact("TRIGGER", "ON_BASIC_ATTACK", "HIGH", prov_text()))

    m = EVERY_N_HITS_RE.search(text)
    if m:
        out.append(fact("TRIGGER", "ON_NTH_HIT", "MEDIUM", prov_text(), n=int(m.group(1))))
    elif re.search(r"\bhit(?:s|ting)?\b", text, re.I):
        out.append(fact("TRIGGER", "ON_HIT", "MEDIUM", prov_text()))

    if re.search(r"\btakedowns?\b", text, re.I):
        out.append(fact("TRIGGER", "ON_TAKEDOWN", "HIGH", prov_text()))

    if re.search(r"\bkill(?:s|ed|ing)?\b", text, re.I):
        out.append(fact("TRIGGER", "ON_KILL", "MEDIUM", prov_text()))

    if re.search(r"\bafter casting\b|\bwhen cast\b|\busing\b|\buse\b", text, re.I):
        out.append(fact("TRIGGER", "ON_CAST_OR_USE", "LOW", prov_text()))
    if re.search(r"\bafter .*? blocks? \d+(?:\.\d+)? damage\b", text, re.I):
        out.append(fact("TRIGGER", "ON_DAMAGE_BLOCKED_THRESHOLD", "HIGH", prov_text()))
    if re.search(r"\b(?:minion|hero|enemy|target)s? that die\b|\bupon .*? death\b|\bon death\b", text, re.I):
        out.append(fact("TRIGGER", "ON_DEATH", "MEDIUM", prov_text()))

    return semantic_dedupe(out)


def infer_conditions(text):
    out = []

    for m in LOW_HEALTH_RE.finditer(text):
        out.append(fact(
            "CONDITION", "LOW_HEALTH", "HIGH", prov_text(phrase=m.group(0)),
            threshold_percent=float(m.group(1)),
        ))

    if WITHOUT_DYING_RE.search(text):
        out.append(fact("CONDITION", "WITHOUT_DYING", "HIGH", prov_text()))
    for m in re.finditer(r"\bif at least\s+(\d+)\s+([^.;]+?)(?:\s+are|\s+is|[.;]|$)", text, re.I):
        out.append(fact("CONDITION", {"type":"MIN_COUNT","count":int(m.group(1)),"subject":m.group(2).strip()}, "HIGH", prov_text(phrase=m.group(0))))
    for m in re.finditer(r"\bfor each\s+([^.;,]+)", text, re.I):
        out.append(fact("CONDITION", {"type":"PER_ENTITY","subject":m.group(1).strip()}, "MEDIUM", prov_text(phrase=m.group(0))))
    for m in re.finditer(r"\buntil\s+([^.;]+)", text, re.I):
        out.append(fact("CONDITION_TEXT", {"type":"UNTIL","text":m.group(1).strip()}, "MEDIUM", prov_text(phrase=m.group(0))))

    condition_patterns = [
        (r"\bwhile (.+?)(?:[.;]|$)", "WHILE"),
        (r"\bif (.+?)(?:[.;]|$)", "IF"),
        (r"\bwhen (.+?)(?:[.;]|$)", "WHEN"),
        (r"\bagainst (.+?)(?:[.;]|$)", "AGAINST"),
    ]
    for pat, typ in condition_patterns:
        for m in re.finditer(pat, text, re.I):
            clause = m.group(1).strip()
            if len(clause) <= 160:
                out.append(fact(
                    "CONDITION_TEXT",
                    {"type": typ, "text": clause},
                    "LOW",
                    prov_text(phrase=m.group(0)),
                ))

    return semantic_dedupe(out)


def infer_internal_cooldown(text):
    # Search complete sentences because "same Hero or Structure" must remain intact.
    sentences = [x.strip() for x in re.split(r"(?<=[.!?;])\s+", text) if x.strip()]

    for sentence in sentences:
        for rx in PER_TARGET_CD_RES:
            m = rx.search(sentence)
            if m:
                return [fact(
                    "INTERNAL_COOLDOWN",
                    float(m.group(1)),
                    "HIGH",
                    prov_text(phrase=sentence),
                    unit="seconds",
                    per_target=True,
                    target_filter=target_filters_for_phrase(sentence),
                )]

    for sentence in sentences:
        for rx in GENERIC_INTERNAL_CD_RES:
            m = rx.search(sentence)
            if m:
                # If this sentence also contains "same Hero/Structure", treat as per-target.
                per_target = bool(re.search(r"\b(?:same|each)\s+(?:hero|structure|target)\b", sentence, re.I))
                return [fact(
                    "INTERNAL_COOLDOWN",
                    float(m.group(1)),
                    "HIGH",
                    prov_text(phrase=sentence),
                    unit="seconds",
                    per_target=per_target,
                    target_filter=target_filters_for_phrase(sentence),
                )]

    return []


def infer_temporal_rules(text, internal_cd):
    out = []

    # Do not duplicate an internal cooldown as PERIODIC.
    icd_values = {float(x.get("value")) for x in internal_cd if isinstance(x.get("value"), (int, float))}

    for phrase in sentence_phrases(text):
        filters = target_filters_for_phrase(phrase)

        for m in PERIOD_RE.finditer(phrase):
            value = float(m.group(1))
            if value in icd_values and re.search(r"\bonce every\b|\bcan only\b", phrase, re.I):
                continue
            out.append(fact(
                "TEMPORAL_RULE",
                "PERIODIC",
                "HIGH",
                prov_text(phrase=phrase),
                period_seconds=value,
                target_filter=filters,
            ))

        for m in DURATION_RE.finditer(phrase):
            out.append(fact(
                "TEMPORAL_RULE",
                "DURATION",
                "MEDIUM",
                prov_text(phrase=phrase),
                duration_seconds=float(m.group(1)),
                target_filter=filters,
            ))

        for m in DELAY_RE.finditer(phrase):
            out.append(fact(
                "TEMPORAL_RULE",
                "DELAY",
                "MEDIUM",
                prov_text(phrase=phrase),
                delay_seconds=float(m.group(1)),
                target_filter=filters,
            ))

    return semantic_dedupe(out)


# ============================================================
# EFFECT EXTRACTION
# ============================================================

FLAT_DAMAGE_RE = re.compile(r"\bdeal(?:s|ing)?\s+(\d+(?:\.\d+)?)\s+damage\b", re.I)
FLAT_HEAL_RE = re.compile(r"\bheal(?:s|ed|ing)?(?:.{0,70}?)\b(?:for\s+)?(\d+(?:\.\d+)?)\s+(?:health|hp)\b", re.I)
RESTORE_RESOURCE_RE = re.compile(r"\b(?:restore|restores|restoring|gain|gains)\s+(\d+(?:\.\d+)?)\s+(mana|energy|fury)\b", re.I)
ARMOR_VALUE_RE = re.compile(r"\b(?:gain|gains|grant|grants|reduce|reduces|lower|lowers|increase|increases)(?:.{0,60}?)\b(\d+(?:\.\d+)?)\s+(spell armor|armor)\b", re.I)
ARMOR_BY_RE = re.compile(r"\b(?:reduce|reduces|increase|increases)(?:.{0,60}?)\b(?:spell )?armor\b(?:.{0,30}?)\bby\s+(\d+(?:\.\d+)?)\b", re.I)

BONUS_DAMAGE_PCT_RE = re.compile(r"\bdeal(?:s)?\s+(\d+(?:\.\d+)?)%\s+(?:additional|bonus|more)\s+damage\b", re.I)
HEAL_FROM_DAMAGE_RE = re.compile(r"\bheal(?:s|ing)?(?:.{0,80}?)\bfor\s+(\d+(?:\.\d+)?)%\s+of(?: the)? damage dealt\b", re.I)

GENERIC_PERCENT_RE = re.compile(
    r"\b(increase|increases|increased|reduce|reduces|reduced|gain|gains|grants?)\b(.{0,80}?)\b(?:by\s+)?(\d+(?:\.\d+)?)%\b",
    re.I,
)
PREFIX_PERCENT_METRIC_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)%\s+(?:bonus\s+|increased\s+|more\s+)?"
    r"(spell power|attack damage|basic attack damage|movement speed|attack speed|damage reduction|healing)\b",
    re.I,
)

FROM_TO_RE = re.compile(r"\b(?:from\s+)?(\d+(?:\.\d+)?)\s+(?:to|down to|up to)\s+(\d+(?:\.\d+)?)\b", re.I)
COOLDOWN_BY_SECONDS_RE = re.compile(r"\b(?:reduce|reduces|decrease|decreases)(?:.{0,50}?)\bcooldown\b(?:.{0,30}?)\bby\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
EXTRA_CHARGES_RE = re.compile(r"\b(\d+)\s+(?:extra|additional)\s+charges?\b", re.I)
EXTRA_CHARGE_RE = re.compile(r"\b(?:an?\s+)?(?:extra|additional)\s+charge\b", re.I)
MAX_CHARGES_RE = re.compile(r"\b(?:stores?|maximum|max|up to)\s+(\d+(?:\.\d+)?)\s+charges?\b", re.I)
CAP_STACKS_RE = re.compile(r"\b(?:stacking\s+up to|up to|maximum|max)\s+(\d+(?:\.\d+)?)\s+(?:stacks?|times?)\b", re.I)
NO_MANA_RE = re.compile(r"\bno longer costs mana\b|\bremove(?:s)? the mana cost\b", re.I)
RESET_CD_RE = re.compile(r"\breset(?:s|ting)?(?:.{0,40}?)\bcooldown\b|\bcooldown is reset\b", re.I)
NOT_CONSUMED_RE = re.compile(r"\bdoes not consume\b|\bnot consumed\b|\bwithout consuming\b", re.I)

STUN_RE = re.compile(r"\bstun(?:s|ned|ning)?\b(?:.{0,35}?\bfor\s+(\d+(?:\.\d+)?)\s+seconds?)?", re.I)
SLOW_RE = re.compile(r"\bslow(?:s|ed|ing)?\b(?!\s+time)(?:.{0,45}?\bby\s+(\d+(?:\.\d+)?)%)?", re.I)
ROOT_RE = re.compile(r"\broot(?:s|ed|ing)?\b", re.I)
SILENCE_RE = re.compile(r"\bsilenc(?:e|es|ed|ing)\b", re.I)
BLIND_RE = re.compile(r"\bblind(?:s|ed)?\b", re.I)
DISABLE_STRUCTURE_RE = re.compile(r"\b(?:disable|disables|disabled)\s+(?:enemy\s+)?structures?\b(?:.{0,40}?\bfor\s+(\d+(?:\.\d+)?)\s+seconds?)?", re.I)
UNSTOPPABLE_RE = re.compile(r"\bunstoppable\b", re.I)
PROTECTED_RE = re.compile(r"\bprotected\b", re.I)
STEALTH_RE = re.compile(r"\bstealth\b", re.I)
FEAR_RE = re.compile(r"\b(?:fear|fears|feared|terrify|terrifies|terrified)\b", re.I)
TAUNT_RE = re.compile(r"\btaunt(?:s|ed)?\b", re.I)
SLEEP_RE = re.compile(r"\bsleep(?:s|ing)?\b|\bput(?:s)? .{0,30}? to sleep\b", re.I)
KNOCKBACK_RE = re.compile(r"\bknock(?:s|ed|ing)?(?:\s+\w+){0,5}?\s+(?:back|away)\b|\bknockback\b", re.I)
KNOCKUP_RE = re.compile(r"\bknock(?:s|ed)?\s+up\b|\bknockup\b", re.I)
PULL_RE = re.compile(r"\bpull(?:s|ed)?\b", re.I)
INVULNERABLE_RE = re.compile(r"\binvulnerable\b", re.I)
STASIS_RE = re.compile(r"\bstasis\b", re.I)
CLEANSE_RE = re.compile(r"\bcleanse\b|\b(?:remove|removes|removed|break|breaks)\b[^.;]{0,90}?(?:stuns?|roots?|slows?|silences?|disables?|crowd control)\b", re.I)

# Damage modifier patterns missed by v3.12.2.
GAIN_BONUS_NAMED_DAMAGE_RE = re.compile(r"\bgain(?:s)?\s+(\d+(?:\.\d+)?)%\s+bonus\s+(.{0,70}?)damage\b", re.I)
INCREASE_NAMED_DAMAGE_FLAT_RE = re.compile(r"\bincrease(?:s|d)?\s+(.{0,70}?(?:damage|periodic damage|center damage|bonus damage))\s+by\s+(\d+(?:\.\d+)?)(?![\d.%])", re.I)
INCREASE_NAMED_DAMAGE_PERCENT_RE = re.compile(r"\bincrease(?:s|d)?\s+(.{0,70}?(?:damage|periodic damage|center damage|bonus damage))\s+by\s+(\d+(?:\.\d+)?)%", re.I)

SPAWN_COUNT_RE = re.compile(r"\b(?:spawn|spawns|summon|summons|create|creates)\s+(\d+)\s+([A-Za-z][A-Za-z -]{1,40})", re.I)


# v3.12.6 extended combat modifier grammar (hero-agnostic).
DEAL_INCREASED_DAMAGE_RE = re.compile(r"\bdeal(?:s|ing)?\s+(?:an?\s+)?(\d+(?:\.\d+)?)%\s+(?:increased|additional|bonus|more)\s+damage\b", re.I)
DAMAGE_DEALT_BY_RE = re.compile(r"\b(?:increase|increases|increased)\s+(?:all\s+)?damage(?:\s+dealt)?\s+by\s+(\d+(?:\.\d+)?)%", re.I)
ABILITY_DEALS_MORE_RE = re.compile(r"\b(?:it|this ability|[A-Za-z][A-Za-z' -]{1,50})\s+deal(?:s)?\s+(\d+(?:\.\d+)?)%\s+(?:more|additional|bonus|increased)\s+damage\b", re.I)
FLAT_MORE_DAMAGE_RE = re.compile(r"\bdeal(?:s|ing)?\s+(\d+(?:\.\d+)?)\s+(?:more|additional|bonus)\s+damage\b", re.I)
DAMAGE_OVER_TIME_PERCENT_RE = re.compile(r"\bdeal(?:s|ing)?\s+(?:an?\s+)?additional\s+(\d+(?:\.\d+)?)%\s+damage\s+over\s+(\d+(?:\.\d+)?)\s+seconds?", re.I)
TARGET_TAKES_INCREASED_DAMAGE_RE = re.compile(r"\b(?:target|enemy|enemies|hero|heroes)\b[^.;]{0,60}?\b(?:take|takes)\s+(\d+(?:\.\d+)?)%\s+(?:increased|more|additional)\s+damage", re.I)
ENEMY_DEALS_LESS_DAMAGE_RE = re.compile(r"\b(?:enemy|enemies|enemy hero|enemy heroes|hero|heroes)\b[^.;]{0,120}?\bdeal\s+(\d+(?:\.\d+)?)%\s+less\s+damage", re.I)
REDUCE_DAMAGE_DEALT_RE = re.compile(r"\breduc(?:e|es|ed|ing)\b[^.;]{0,100}?\bdamage\s+(?:they\s+)?deal\s+by\s+(\d+(?:\.\d+)?)%", re.I)
BASIC_ATTACK_MORE_RE = re.compile(r"\bbasic attacks?\b[^.;]{0,100}?\bdeal(?:s)?\s+(\d+(?:\.\d+)?)%\s+(?:more|additional|bonus|increased)\s+damage", re.I)
SPELL_POWER_GAIN_RE = re.compile(r"\bgain(?:s)?\s+(\d+(?:\.\d+)?)%\s+spell power\b", re.I)
PERCENT_MISSING_HEALTH_DAMAGE_RE = re.compile(r"\b(?:deal|deals)\s+(\d+(?:\.\d+)?)%\s+of\s+(?:the\s+)?(?:target(?:'s)?|enemy hero(?:'s)?)\s+missing health\b", re.I)
PERCENT_MAX_HEALTH_GENERIC_RE = re.compile(r"\b(?:bonus|additional)?\s*damage\s+(?:to\s+heroes?\s+)?equal\s+to\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their|the target(?:'s)?|an? enemy hero(?:'s)?)\s+maximum health", re.I)
STATUS_REMOVE_RE = re.compile(r"\b(?:remove|removes|removed|break|breaks|breaking|cleanse|cleanses)\b", re.I)
STATUS_CONDITION_PREFIX_RE = re.compile(r"\b(?:against|while|if|when|being|that is|that are|who is|who are)\b[^.;]{0,100}?\b(?:stunned|rooted|slowed|silenced|feared|asleep|taunted)\b", re.I)
IS_INCREASED_DAMAGE_RE = re.compile(r"\b((?:basic attack|attack|ability|spell|heroic|periodic|center|all)?\s*damage(?:\s+dealt)?)\s+is\s+(?:permanently\s+)?increased\s+by\s+(\d+(?:\.\d+)?)%?", re.I)
PERMANENT_DAMAGE_FLAT_RE = re.compile(r"\bdamage\s+is\s+permanently\s+increased\s+by\s+(\d+(?:\.\d+)?)(?!\s*%)", re.I)
MAX_HEALTH_AS_DAMAGE_RE = re.compile(r"\b(?:additional\s+)?(\d+(?:\.\d+)?)%\s+of\s+(?:their|the target(?:'s)?|an? enemy hero(?:'s)?|maximum)?\s*maximum health\s+as\s+damage", re.I)
DAMAGE_INCREASES_FLAT_RE = re.compile(r"\bdamage\s+(?:permanently\s+)?increases?\s+by\s+(\d+(?:\.\d+)?)(?!\s*%)", re.I)
DAMAGE_INCREASES_PERCENT_RE = re.compile(r"\bdamage\s+(?:against\s+[^.;]{0,50}?\s+)?is\s+increased\s+by\s+(\d+(?:\.\d+)?)%", re.I)
DOES_MORE_DAMAGE_RE = re.compile(r"\b(?:does|deal|deals)\s+(?:up\s+to\s+)?(\d+(?:\.\d+)?)%\s+(?:more|additional|bonus|increased)\s+damage", re.I)
SPLASH_DAMAGE_RE = re.compile(r"\bsplashes?\s+for\s+(\d+(?:\.\d+)?)%\s+increased\s+damage", re.I)
INCREASE_DAMAGE_AND_HEAL_RE = re.compile(r"\bincrease(?:s|d)?\s+(?:the\s+)?damage\s+and\s+(?:the\s+)?heal(?:ing)?\b[^.;]{0,60}?\bby\s+(\d+(?:\.\d+)?)%", re.I)
INCREASE_HEAL_AND_DAMAGE_RE = re.compile(r"\bincrease(?:s|d)?\s+(?:the\s+)?heal(?:ing)?\s+and\s+(?:the\s+)?damage\b[^.;]{0,60}?\bby\s+(\d+(?:\.\d+)?)%", re.I)
DAMAGE_BY_ADDITIONAL_PERCENT_RE = re.compile(r"\b(?:increase|increases|increased)\s+(.{0,70}?damage)\s+by\s+(?:an\s+)?additional\s+(\d+(?:\.\d+)?)%", re.I)
BONUS_DAMAGE_EQUAL_MAX_HEALTH_RE = re.compile(r"\b(?:deal|deals)?\s*(?:an?\s+)?(?:additional\s+|bonus\s+)?damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+(?:of\s+)?(?:the\s+)?(?:target(?:'s)?|hero(?:'s)?|heroes'|their)\s+maximum health", re.I)
BONUS_DAMAGE_EQUAL_MISSING_HEALTH_RE = re.compile(r"\b(?:deal|deals)?\s*(?:an?\s+)?(?:additional\s+|bonus\s+)?damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+(?:of\s+)?(?:the\s+)?(?:target(?:'s)?|hero(?:'s)?|heroes'|their)\s+missing health", re.I)
PERCENT_OF_MAX_HEALTH_AS_DAMAGE_RE = re.compile(r"\b(?:an?\s+)?additional\s+(\d+(?:\.\d+)?)%\s+of\s+(?:the\s+)?(?:target(?:'s)?|hero(?:'s)?|heroes'|their)\s+maximum health\s+as\s+damage", re.I)
BURNS_MAX_HEALTH_RE = re.compile(r"\bburn(?:s|ing)?\b[^.;]{0,80}?\bfor\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their|the target(?:'s)?|hero(?:'s)?)\s+maximum health", re.I)
DAMAGE_BONUS_TO_RE = re.compile(r"\b(?:increase|increases)\s+(?:the\s+)?damage bonus\s+to\s+(\d+(?:\.\d+)?)%", re.I)

# v3.12.7 damage source/formula grammar.

# v3.12.8 advanced source/formula modifiers.
BONUS_MAX_HEALTH_NO_OF_RE = re.compile(r"\b(?:deal|deals|dealing)\s+(?:bonus\s+)?damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+(?:of\s+)?(?:their\s+)?maximum health", re.I)
BASIC_ATTACK_BONUS_MAX_HEALTH_RE = re.compile(r"\bbasic attacks?\b[^.;]{0,100}?\b(?:deal|deals)\s+bonus damage equal to\s+(\d+(?:\.\d+)?)%\s+(?:of\s+)?(?:their\s+)?maximum health", re.I)
DAMAGE_TO_PERCENT_SOURCE_RE = re.compile(r"\b(?:increase|increases|increased)\s+(?:the\s+)?damage\b[^.;]{0,80}?\bto\s+(\d+(?:\.\d+)?)%\s+of\s+(.+?)\s+damage\b", re.I)
SPLASH_TO_PERCENT_ATTACK_RE = re.compile(r"\bsplash damage\s+to\s+(\d+(?:\.\d+)?)%\s+of\s+basic attack damage", re.I)
DAMAGE_AND_OTHER_INCREASE_RE = re.compile(r"\b(?:the\s+)?damage\s+and\s+[^.;]{1,80}?\s+(?:are|is)\s+increased\s+by\s+(\d+(?:\.\d+)?)%", re.I)
SPELL_POWER_BONUS_PROVIDED_RE = re.compile(r"\bincreases?\s+(?:the\s+)?spell power bonus[^.;]{0,100}?\s+by\s+(\d+(?:\.\d+)?)%", re.I)
UP_TO_ADDITIONAL_DAMAGE_RE = re.compile(r"\bup to\s+(\d+(?:\.\d+)?)%\s+additional damage", re.I)
TARGETED_INCREASED_DAMAGE_RE = re.compile(r"\btargeted\s+by\s+[^.;]{1,80}?\s+for\s+(\d+(?:\.\d+)?)%\s+increased damage", re.I)
DAMAGE_PER_COUNTER_RE = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)%\s+(?:bonus|additional) damage per\s+(mark|stack|charge)", re.I)
NEXT_N_BASIC_ATTACKS_BONUS_RE = re.compile(r"\bnext\s+(\d+)\s+basic attacks?\b[^.;]{0,100}?\b(?:deal|deals)\s+(?:an?\s+)?additional\s+(\d+(?:\.\d+)?)%\s+damage", re.I)
DAMAGE_INCREASE_AFTER_EACH_RE = re.compile(r"\bincrease(?:s)?\s+(?:the\s+)?damage[^.;]{0,80}?\s+by\s+(\d+(?:\.\d+)?)%\s+after each\s+([^.;]+)", re.I)
TOTAL_DAMAGE_OVER_TIME_RE = re.compile(r"\bdeals?\s+(?:a\s+)?total\s+of\s+(\d+(?:\.\d+)?)\s+damage\s+over\s+(\d+(?:\.\d+)?)\s+seconds?", re.I)
PERCENT_MAX_HEALTH_AS_DAMAGE_NO_OF_RE = re.compile(r"\b(?:deal|deals|dealing|impact deals)\s+(\d+(?:\.\d+)?)%\s+maximum health\s+as damage", re.I)
DAMAGE_INCREASE_FOR_DISTANCE_RE = re.compile(r"\bdamage increase for distance traveled by\s+(\d+(?:\.\d+)?)%", re.I)
MAX_DISTANCE_DAMAGE_FROM_TO_RE = re.compile(r"\bmaximum distance damage bonus\s+from\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)", re.I)

DEAL_PERCENT_OF_BASE_RE = re.compile(r"\b(?:deal|deals|dealing)\s+(\d+(?:\.\d+)?)%\s+damage\b", re.I)
DEAL_ADDITIONAL_PERCENT_DAMAGE_RE = re.compile(r"\b(?:deal|deals|dealing)\s+(?:an?\s+)?additional\s+(\d+(?:\.\d+)?)%\s+damage\b", re.I)
SUBJECT_DEALS_MORE_DAMAGE_RE = re.compile(r"\b(.{1,80}?)\b(?:deal|deals)\s+(?:an?\s+)?(?:additional\s+)?(\d+(?:\.\d+)?)%\s+(?:more\s+|bonus\s+|additional\s+|increased\s+)damage\b", re.I)
SUBJECT_DAMAGE_INCREASE_RE = re.compile(r"\b(?:increase|increases|increased)\s+(.{1,90}?)\s+damage\s+(?:to\s+[^.;]{1,60}?\s+)?by\s+(\d+(?:\.\d+)?)%", re.I)
DAMAGE_REDUCED_BY_RE = re.compile(r"\b(?:damage|splash damage)\b[^.;]{0,80}?\b(?:reduced|decreased)\s+by\s+(\d+(?:\.\d+)?)%", re.I)
DAMAGE_AS_PERCENT_ATTACK_RE = re.compile(r"\b(?:deal|deals|dealing)\s+(?:bonus\s+|additional\s+)?(?:spell\s+)?damage\s+equal\s+to\s+(\d+(?:\.\d+)?)%\s+of\s+(?:their\s+|his\s+|her\s+|the\s+)?basic attack damage", re.I)
DAMAGE_TO_NEARBY_TARGETS_RE = re.compile(r"\bbasic attacks?\s+(?:also\s+)?deal\s+(\d+(?:\.\d+)?)%\s+damage\s+to\s+(\d+)\s+(?:nearby|additional)\s+targets?", re.I)
DAMAGE_AGAINST_TARGET_BY_RE = re.compile(r"\bbasic attack damage\s+against\s+(.{1,80}?)\s+by\s+(\d+(?:\.\d+)?)%", re.I)
DAMAGE_FROM_TO_PERCENT_RE = re.compile(r"\b(?:damage|hero damage|ability damage|basic attack damage)\b[^.;]{0,60}?\bfrom\s+(\d+(?:\.\d+)?)%\s+to\s+(\d+(?:\.\d+)?)%", re.I)
FLAT_DELAYED_DAMAGE_RE = re.compile(r"\b(?:explode|explodes|exploding)[^.;]{0,100}?\bfor\s+(\d+(?:\.\d+)?)\s+damage\b", re.I)


def metric_from_text(text):
    t = text.lower()
    mapping = [
        ("basic attack damage", "BASIC_ATTACK_DAMAGE"),
        ("attack damage", "BASIC_ATTACK_DAMAGE"),
        ("attack speed", "ATTACK_SPEED"),
        ("movement speed", "MOVEMENT_SPEED"),
        ("spell power", "SPELL_POWER"),
        ("damage reduction", "DAMAGE_REDUCTION"),
        ("spell armor", "SPELL_ARMOR"),
        ("armor", "ARMOR"),
        ("healing", "HEALING"),
        ("range", "RANGE"),
        ("width", "WIDTH"),
        ("radius", "RADIUS"),
        ("cooldown", "COOLDOWN"),
        ("mana cost", "MANA_COST"),
        ("health regeneration", "HEALTH_REGEN_RATE"),
        ("health regen", "HEALTH_REGEN_RATE"),
        ("mana regeneration", "MANA_REGEN_RATE"),
        ("mana regen", "MANA_REGEN_RATE"),
        ("energy regeneration", "ENERGY_REGEN_RATE"),
        ("energy regen", "ENERGY_REGEN_RATE"),
        ("mana", "MANA"),
        ("energy", "ENERGY"),
        ("fury", "FURY"),
        ("health", "HEALTH"),
        ("shield", "SHIELD"),
        ("damage", "DAMAGE"),
        ("charges", "CHARGES"),
    ]
    for token, metric in mapping:
        if token in t:
            return metric
    return "UNKNOWN_STAT"



# ============================================================
# v3.8 RELATION / COUNTER / CONDITIONAL MECHANICS
# ============================================================
ADDITIONAL_FLAT_DAMAGE_RE = re.compile(r"\b(?:take|takes|deal|deals)\s+(?:an?\s+)?additional\s+(\d+(?:\.\d+)?)\s+damage\b", re.I)
PERCENT_MAX_HEALTH_DAMAGE_RE = re.compile(r"\b(?:deal|deals)\s+(\d+(?:\.\d+)?)%\s+of\s+(?:the\s+)?(?:target(?:'s)?|enemy hero(?:'s)?)\s+maximum health\b", re.I)
PERCENT_CURRENT_HEALTH_DAMAGE_RE = re.compile(r"\b(?:deal|deals)\s+(\d+(?:\.\d+)?)%\s+of\s+(?:the\s+)?(?:target(?:'s)?|enemy hero(?:'s)?)\s+current health\b", re.I)
FLAT_STAT_BY_RE = re.compile(r"\b(?:increase|increases|gain|gains|grant|grants|reduce|reduces)\b(.{0,65}?)\bby\s+(\d+(?:\.\d+)?)(?![\d.%])", re.I)
STAT_VALUE_RE = re.compile(r"\b(?:gain|gains|grant|grants)\s+(\d+(?:\.\d+)?)\s+(spell power|attack damage|basic attack damage|attack speed|movement speed|armor|spell armor|health regeneration|mana regeneration)\b", re.I)
REGEN_PER_SECOND_RE = re.compile(r"\b(?:gain|gains|restore|restores|regenerate|regenerates)\s+(\d+(?:\.\d+)?)\s+(health|mana|energy)\s+per\s+second\b", re.I)
REGEN_PERCENT_RE = re.compile(
    r"\b(health|mana|energy)\s+(?:regeneration|regen)\s+(?:is\s+)?(?:increased|reduced)\s+by\s+(\d+(?:\.\d+)?)%", re.I
)
JOINT_REGEN_PERCENT_RE = re.compile(
    r"\b(health|mana|energy)\s+and\s+(health|mana|energy)\s+(?:regeneration|regen)\s+(?:is\s+)?(?:increased|reduced)\s+by\s+(\d+(?:\.\d+)?)%", re.I
)
REGEN_PERCENT_PREFIX_RE = re.compile(
    r"\b(?:increase|increases|increased|reduce|reduces|reduced)\s+(health|mana|energy)\s+(?:regeneration|regen)\s+by\s+(\d+(?:\.\d+)?)%", re.I
)
REGEN_JOINT_OBJECT_RE = re.compile(
    r"\b(increase|increases|increased|reduce|reduces|reduced)\b.{0,45}?\b(health|mana|energy)\s+and\s+(health|mana|energy)\s+(?:regeneration|regen)\s+by\s+(\d+(?:\.\d+)?)%", re.I
)
RECHARGE_RATE_RE = re.compile(r"\b(?:cooldowns?|charges?)\s+(?:recharge|recharges|recover|recovers)\s+(\d+(?:\.\d+)?)%\s+faster\b", re.I)
SLOWER_RATE_RE = re.compile(r"\b(.{0,45}?)\s+(\d+(?:\.\d+)?)%\s+(?:more\s+)?slowly\b", re.I)
ADDITIONAL_TARGETS_RE = re.compile(r"\b(?:hit|hits|target|targets|affect|affects|pierce|pierces)\s+(\d+)\s+additional\s+(?:enemy|enemies|target|targets|hero|heroes)\b", re.I)
ADDITIONAL_PROJECTILES_RE = re.compile(r"\b(?:fire|fires|shoot|shoots|launch|launches|throw|throws|create|creates)\s+(\d+)\s+additional\s+(?:projectiles?|missiles?|bolts?|vines?|shots?|arrows?)\b", re.I)
COUNT_MODIFIER_RE = re.compile(r"\b(?:the\s+)?(\d+)\s+(?:closest\s+)?(?:enemies|heroes|targets|units)\b", re.I)
PROC_LIMIT_RE = re.compile(r"\b(?:up to|maximum of|max of|a maximum of)\s+(\d+)\s+(?:times?|procs?|activations?|uses?)\b", re.I)
AFTER_TAKING_DAMAGE_RE = re.compile(r"\bafter\s+taking\s+(\d+(?:\.\d+)?)\s+damage\b", re.I)
AFTER_HEALING_RE = re.compile(r"\bafter\s+(?:healing|restoring)\s+(\d+(?:\.\d+)?)\s+(?:health|hp)\b", re.I)
DISTINCT_ABILITY_COMBO_RE = re.compile(r"\b(?:hit|hits|hitting)\s+(?:a\s+)?hero\s+with\s+(\d+)\s+of\s+[^.]{0,70}?\babilities?\s+within\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
GENERIC_HITS_WITHIN_RE = re.compile(r"\b(?:hit|hits|hitting)\s+(?:a\s+)?hero\s+(\d+)\s+times?\s+within\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
USE_N_ABILITIES_WITHIN_RE = re.compile(r"\b(?:use|using|cast|casting)\s+(\d+)\s+(?:basic\s+)?abilities?\s+within\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
REQUIRED_HITS_RE = re.compile(r"\b(?:after|upon|when)\s+(?:hitting|damaging)\s+(\d+)\s+(?:enemy\s+)?heroes?\b", re.I)
SAME_TARGET_RE = re.compile(r"\b(?:same|a single)\s+(?:hero|target|enemy)\b", re.I)
FAIL_CONDITION_RE = re.compile(r"\bif\s+[^.]{0,100}?(?:fails?|does not|doesn't|without)\b", re.I)
MAX_CHANNEL_RE = re.compile(r"\b(?:up to|maximum of|max of)\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
WITHIN_WINDOW_RE = re.compile(r"\bwithin\s+(\d+(?:\.\d+)?)\s+seconds?\b", re.I)
NEXT_N_RE = re.compile(r"\bnext\s+(\d+)\s+(?:attacks?|abilities?|hits?|instances?|uses?)\b", re.I)
FADES_AFTER_N_RE = re.compile(r"\b(?:fades?|expires?|ends?)\s+after\s+(\d+)\s+(?:heals?|hits?|uses?|activations?|procs?)\b", re.I)


def infer_counter_rules(text):
    out=[]
    for phrase in sentence_phrases(text):
        filters=target_filters_for_phrase(phrase)
        m=DISTINCT_ABILITY_COMBO_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'ABILITY_COMBO','required_count':int(m.group(1)),'distinct_abilities':True,'same_target':True,'window_seconds':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        m=GENERIC_HITS_WITHIN_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'HITS_WITHIN_WINDOW','required_count':int(m.group(1)),'same_target':True,'window_seconds':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        m=USE_N_ABILITIES_WITHIN_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'ABILITIES_WITHIN_WINDOW','required_count':int(m.group(1)),'window_seconds':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters))
        m=REQUIRED_HITS_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'REQUIRED_HITS','required_count':int(m.group(1)),'same_target':bool(SAME_TARGET_RE.search(phrase))}, 'MEDIUM', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        m=AFTER_TAKING_DAMAGE_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'DAMAGE_TAKEN_THRESHOLD','threshold':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        m=AFTER_HEALING_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'HEALING_DONE_THRESHOLD','threshold':float(m.group(1))}, 'MEDIUM', prov_text(phrase), target_filter=filters))
        m=PROC_LIMIT_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'PROC_LIMIT','max_procs':int(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        m=FADES_AFTER_N_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'MAX_PROCS','max_procs':int(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        m=NEXT_N_RE.search(phrase)
        if m: out.append(fact('COUNTER_RULE', {'type':'NEXT_N_EVENTS','count':int(m.group(1))}, 'MEDIUM', prov_text(phrase), target_filter=filters))
    return semantic_dedupe(out)


def infer_windows_and_failures(text):
    out=[]
    for phrase in sentence_phrases(text):
        filters=target_filters_for_phrase(phrase)
        m=MAX_CHANNEL_RE.search(phrase)
        if m and 'channel' in phrase.lower(): out.append(fact('TEMPORAL_RULE','CHANNEL_MAX_DURATION','HIGH',prov_text(phrase),duration_seconds=float(m.group(1)),target_filter=filters))
        m=WITHIN_WINDOW_RE.search(phrase)
        if m and not (DISTINCT_ABILITY_COMBO_RE.search(phrase) or GENERIC_HITS_WITHIN_RE.search(phrase) or USE_N_ABILITIES_WITHIN_RE.search(phrase)):
            out.append(fact('TEMPORAL_RULE','ACTIVATION_WINDOW','MEDIUM',prov_text(phrase),duration_seconds=float(m.group(1)),target_filter=filters))
        if FAIL_CONDITION_RE.search(phrase): out.append(fact('CONDITION','FAIL_CONDITION','MEDIUM',prov_text(phrase),text=phrase,target_filter=filters))
    return semantic_dedupe(out)


def infer_effects(text):
    out = []

    for phrase in sentence_phrases(text):
        filters = target_filters_for_phrase(phrase)


        # v3.12.6 extended combat modifiers. Specific formulas run before generic parsing.
        # v3.12.7 source-aware damage formulas.
        # v3.12.8 advanced source/formula modifiers.
        for m in BASIC_ATTACK_BONUS_MAX_HEALTH_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MAX_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO'])), source_subject='BASIC_ATTACK'))
        for m in BONUS_MAX_HEALTH_NO_OF_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MAX_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        for m in DAMAGE_TO_PERCENT_SOURCE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'SET_PERCENT_OF_SOURCE','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters, source_subject=m.group(2).strip()))
        for m in SPLASH_TO_PERCENT_ATTACK_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'BASIC_ATTACK_DAMAGE','operation':'SET_PERCENT_OF_BASE','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters, source_subject='SPLASH'))
        for m in DAMAGE_AND_OTHER_INCREASE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in SPELL_POWER_BONUS_PROVIDED_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'SPELL_POWER','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in UP_TO_ADDITIONAL_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT_CAP','value':float(m.group(1))}, 'MEDIUM', prov_text(phrase), target_filter=filters))
        for m in TARGETED_INCREASED_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE_TAKEN','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        for m in DAMAGE_PER_COUNTER_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT_PER_COUNTER','value':float(m.group(1)),'counter':m.group(2).upper()}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in NEXT_N_BASIC_ATTACKS_BONUS_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'BASIC_ATTACK_DAMAGE','operation':'ADD_PERCENT','value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters, event_count=int(m.group(1))))
        for m in DAMAGE_INCREASE_AFTER_EACH_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT_PER_EVENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters, event_text=m.group(2).strip()))
        for m in TOTAL_DAMAGE_OVER_TIME_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE_OVER_TIME','operation':'DEAL_FLAT_TOTAL','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters, duration_seconds=float(m.group(2))))
        for m in PERCENT_MAX_HEALTH_AS_DAMAGE_NO_OF_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MAX_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DAMAGE_INCREASE_FOR_DISTANCE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT_DISTANCE_SCALING','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in MAX_DISTANCE_DAMAGE_FROM_TO_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'SET_FROM_TO','from':float(m.group(1)),'to':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters, scaling_axis='DISTANCE'))
        for m in DAMAGE_TO_NEARBY_TARGETS_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'BASIC_ATTACK_DAMAGE','operation':'SET_PERCENT_OF_BASE','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters, secondary_target_count=int(m.group(2))))
        for m in DEAL_ADDITIONAL_PERCENT_DAMAGE_RE.finditer(phrase):
            metric='BASIC_ATTACK_DAMAGE' if re.search(r'\bbasic attacks?\b',phrase,re.I) else 'DAMAGE'
            out.append(fact('MODIFIER', {'metric':metric,'operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DEAL_PERCENT_OF_BASE_RE.finditer(phrase):
            # "dealing 75% damage" means a copy/secondary instance at a fraction of base damage.
            metric='BASIC_ATTACK_DAMAGE' if re.search(r'\bbasic attacks?\b',phrase,re.I) else 'DAMAGE'
            out.append(fact('MODIFIER', {'metric':metric,'operation':'SET_PERCENT_OF_BASE','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in SUBJECT_DEALS_MORE_DAMAGE_RE.finditer(phrase):
            subject=m.group(1).strip()
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT','value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters, source_subject=subject))
        for m in SUBJECT_DAMAGE_INCREASE_RE.finditer(phrase):
            subject=m.group(1).strip()
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT','value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters, source_subject=subject))
        for m in DAMAGE_REDUCED_BY_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'REDUCE_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DAMAGE_AS_PERCENT_ATTACK_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'RATIO_TO_BASIC_ATTACK','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DAMAGE_AGAINST_TARGET_BY_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'BASIC_ATTACK_DAMAGE','operation':'ADD_PERCENT','value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=target_filters_for_phrase(m.group(1))))
        for m in DAMAGE_FROM_TO_PERCENT_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'SET_FROM_TO','from':float(m.group(1)),'to':float(m.group(2)),'unit':'percent'}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in FLAT_DELAYED_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'DEAL_FLAT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DAMAGE_OVER_TIME_PERCENT_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE_OVER_TIME','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), duration_seconds=float(m.group(2)), target_filter=filters))
        for rx in (DEAL_INCREASED_DAMAGE_RE, DAMAGE_DEALT_BY_RE, ABILITY_DEALS_MORE_RE):
            for m in rx.finditer(phrase):
                out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in BASIC_ATTACK_MORE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'BASIC_ATTACK_DAMAGE','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in FLAT_MORE_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_FLAT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in SPELL_POWER_GAIN_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'SPELL_POWER','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in IS_INCREASED_DAMAGE_RE.finditer(phrase):
            metric=metric_from_text(m.group(1))
            if metric=='UNKNOWN_STAT': metric='DAMAGE'
            out.append(fact('MODIFIER', {'metric':metric,'operation':'ADD_PERCENT','value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in PERMANENT_DAMAGE_FLAT_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_FLAT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in MAX_HEALTH_AS_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MAX_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        for m in DAMAGE_INCREASES_FLAT_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_FLAT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DAMAGE_INCREASES_PERCENT_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for rx in (DOES_MORE_DAMAGE_RE,SPLASH_DAMAGE_RE):
            for m in rx.finditer(phrase):
                metric='BASIC_ATTACK_DAMAGE' if 'basic attack' in phrase.lower() else 'DAMAGE'
                out.append(fact('MODIFIER', {'metric':metric,'operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for rx in (INCREASE_DAMAGE_AND_HEAL_RE,INCREASE_HEAL_AND_DAMAGE_RE):
            for m in rx.finditer(phrase):
                val=float(m.group(1))
                out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'ADD_PERCENT','value':val}, 'HIGH', prov_text(phrase), target_filter=filters))
                out.append(fact('MODIFIER', {'metric':'HEALING','operation':'ADD_PERCENT','value':val}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in DAMAGE_BY_ADDITIONAL_PERCENT_RE.finditer(phrase):
            metric=metric_from_text(m.group(1)); metric='DAMAGE' if metric=='UNKNOWN_STAT' else metric
            out.append(fact('MODIFIER', {'metric':metric,'operation':'ADD_PERCENT','value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for rx,metric in ((BONUS_DAMAGE_EQUAL_MAX_HEALTH_RE,'PERCENT_MAX_HEALTH_DAMAGE'),(PERCENT_OF_MAX_HEALTH_AS_DAMAGE_RE,'PERCENT_MAX_HEALTH_DAMAGE'),(BURNS_MAX_HEALTH_RE,'PERCENT_MAX_HEALTH_DAMAGE'),(BONUS_DAMAGE_EQUAL_MISSING_HEALTH_RE,'PERCENT_MISSING_HEALTH_DAMAGE')):
            for m in rx.finditer(phrase):
                out.append(fact('MODIFIER', {'metric':metric,'operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        for m in DAMAGE_BONUS_TO_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE_BONUS','operation':'SET_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in TARGET_TAKES_INCREASED_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE_TAKEN','operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for rx in (ENEMY_DEALS_LESS_DAMAGE_RE, REDUCE_DAMAGE_DEALT_RE):
            for m in rx.finditer(phrase):
                out.append(fact('MODIFIER', {'metric':'ENEMY_DAMAGE_DEALT','operation':'REDUCE_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in PERCENT_MISSING_HEALTH_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MISSING_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        for m in PERCENT_MAX_HEALTH_GENERIC_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MAX_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))

        # v3.8 relation-aware mechanics
        for m in ADDITIONAL_FLAT_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'DAMAGE','operation':'DEAL_FLAT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in PERCENT_MAX_HEALTH_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_MAX_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=sorted(set(filters+['HERO']))))
        for m in PERCENT_CURRENT_HEALTH_DAMAGE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':'PERCENT_CURRENT_HEALTH_DAMAGE','operation':'DEAL_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in STAT_VALUE_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':metric_from_text(m.group(2)),'operation':'ADD_FLAT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in REGEN_PER_SECOND_RE.finditer(phrase):
            out.append(fact('MODIFIER', {'metric':m.group(2).upper()+'_REGEN_RATE','operation':'ADD_PER_SECOND','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        joint_regen_matched=False
        for m in JOINT_REGEN_PERCENT_RE.finditer(phrase):
            joint_regen_matched=True
            op='REDUCE_PERCENT' if 'reduc' in m.group(0).lower() else 'ADD_PERCENT'
            value=float(m.group(3))
            for resource in (m.group(1),m.group(2)):
                out.append(fact('MODIFIER', {'metric':resource.upper()+'_REGEN_RATE','operation':op,'value':value}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in REGEN_JOINT_OBJECT_RE.finditer(phrase):
            joint_regen_matched=True
            op='REDUCE_PERCENT' if m.group(1).lower().startswith('reduc') else 'ADD_PERCENT'
            value=float(m.group(4))
            for resource in (m.group(2),m.group(3)):
                out.append(fact('MODIFIER', {'metric':resource.upper()+'_REGEN_RATE','operation':op,'value':value}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in REGEN_PERCENT_RE.finditer(phrase):
            # Skip occurrences already covered by a joint Health+Mana expression.
            if joint_regen_matched:
                continue
            op='REDUCE_PERCENT' if 'reduc' in m.group(0).lower() else 'ADD_PERCENT'
            out.append(fact('MODIFIER', {'metric':m.group(1).upper()+'_REGEN_RATE','operation':op,'value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in REGEN_PERCENT_PREFIX_RE.finditer(phrase):
            op='REDUCE_PERCENT' if 'reduc' in m.group(0).lower() else 'ADD_PERCENT'
            out.append(fact('MODIFIER', {'metric':m.group(1).upper()+'_REGEN_RATE','operation':op,'value':float(m.group(2))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in RECHARGE_RATE_RE.finditer(phrase):
            metric='COOLDOWN_RECHARGE_RATE' if 'cooldown' in m.group(0).lower() else 'CHARGE_RECHARGE_RATE'
            out.append(fact('MODIFIER', {'metric':metric,'operation':'ADD_PERCENT','value':float(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in SLOWER_RATE_RE.finditer(phrase):
            metric='HEALTH_DECAY_RATE' if 'health' in m.group(1).lower() else 'RATE'
            out.append(fact('MODIFIER', {'metric':metric,'operation':'SLOW_RATE_PERCENT','value':float(m.group(2))}, 'MEDIUM', prov_text(phrase), target_filter=filters))
        for m in ADDITIONAL_TARGETS_RE.finditer(phrase):
            out.append(fact('COUNT_MODIFIER', {'metric':'ADDITIONAL_TARGETS','operation':'ADD_FLAT','value':int(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        for m in ADDITIONAL_PROJECTILES_RE.finditer(phrase):
            out.append(fact('COUNT_MODIFIER', {'metric':'ADDITIONAL_PROJECTILES','operation':'ADD_FLAT','value':int(m.group(1))}, 'HIGH', prov_text(phrase), target_filter=filters))
        m=COUNT_MODIFIER_RE.search(phrase)
        if m and any(x in phrase.lower() for x in ('closest','throw','target','hit','affect')):
            out.append(fact('COUNT_MODIFIER', {'metric':'TARGET_COUNT','operation':'SET_OR_SELECT','value':int(m.group(1))}, 'MEDIUM', prov_text(phrase), target_filter=filters))
        for m in FLAT_STAT_BY_RE.finditer(phrase):
            metric=metric_from_text(m.group(1)+' '+phrase)
            if metric not in {'UNKNOWN_STAT','DAMAGE'}:
                op='REDUCE_FLAT' if m.group(0).lower().startswith('reduce') else 'ADD_FLAT'
                out.append(fact('MODIFIER', {'metric':metric,'operation':op,'value':float(m.group(2))}, 'MEDIUM', prov_text(phrase), target_filter=filters))

        # Flat damage/heal/resource.
        for m in FLAT_DAMAGE_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "DAMAGE", "operation": "DEAL_FLAT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in FLAT_HEAL_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "HEALING", "operation": "HEAL_FLAT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in RESTORE_RESOURCE_RE.finditer(phrase):
            if "per second" in phrase.lower():
                continue
            out.append(fact(
                "MODIFIER",
                {"metric": m.group(2).upper(), "operation": "RESTORE_FLAT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Armor.
        for m in ARMOR_VALUE_RE.finditer(phrase):
            verb = m.group(0).lower()
            val = float(m.group(1))
            metric = "SPELL_ARMOR" if "spell armor" in m.group(2).lower() else "ARMOR"
            op = "ADD_FLAT" if not any(x in verb for x in ("reduce", "lower")) else "REDUCE_FLAT"
            out.append(fact(
                "MODIFIER", {"metric": metric, "operation": op, "value": val},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in ARMOR_BY_RE.finditer(phrase):
            op = "REDUCE_FLAT" if "reduce" in m.group(0).lower() else "ADD_FLAT"
            metric = "SPELL_ARMOR" if "spell armor" in m.group(0).lower() else "ARMOR"
            out.append(fact(
                "MODIFIER", {"metric": metric, "operation": op, "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Bonus damage / lifesteal.
        for m in BONUS_DAMAGE_PCT_RE.finditer(phrase):
            metric = "BASIC_ATTACK_DAMAGE" if "basic attack" in phrase.lower() else "DAMAGE"
            out.append(fact(
                "MODIFIER", {"metric": metric, "operation": "ADD_PERCENT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in HEAL_FROM_DAMAGE_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "DAMAGE_TO_HEAL", "operation": "RATIO_PERCENT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Prefix percent metric pairs: handles multiple values in one phrase.
        for m in PREFIX_PERCENT_METRIC_RE.finditer(phrase):
            metric = metric_from_text(m.group(2))
            out.append(fact(
                "MODIFIER",
                {"metric": metric, "operation": "ADD_PERCENT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Generic "increase X by N%".
        for m in GENERIC_PERCENT_RE.finditer(phrase):
            verb = m.group(1).lower()
            context = m.group(2)
            value = float(m.group(3))
            metric = metric_from_text(context + " " + phrase)

            if verb.startswith("reduce"):
                operation = "REDUCE_PERCENT"
            else:
                operation = "ADD_PERCENT"

            out.append(fact(
                "MODIFIER",
                {"metric": metric, "operation": operation, "value": value},
                "MEDIUM", prov_text(phrase), target_filter=filters,
            ))

        # from -> to
        m = FROM_TO_RE.search(phrase)
        if m and any(k in phrase.lower() for k in ("mana", "cooldown", "range", "health", "damage", "charge", "armor")):
            out.append(fact(
                "MODIFIER",
                {"metric": metric_from_text(phrase), "operation": "SET_FROM_TO",
                 "from": float(m.group(1)), "to": float(m.group(2))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # cooldown, charges, caps
        for m in COOLDOWN_BY_SECONDS_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "COOLDOWN", "operation": "ADD_SECONDS", "value": -float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        m = EXTRA_CHARGES_RE.search(phrase)
        if m:
            out.append(fact(
                "MODIFIER", {"metric": "CHARGES", "operation": "ADD_FLAT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))
        elif EXTRA_CHARGE_RE.search(phrase):
            out.append(fact(
                "MODIFIER", {"metric": "CHARGES", "operation": "ADD_FLAT", "value": 1.0},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        m = MAX_CHARGES_RE.search(phrase)
        if m:
            out.append(fact(
                "MODIFIER", {"metric": "CHARGES", "operation": "SET_MAX", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        m = CAP_STACKS_RE.search(phrase)
        if m:
            out.append(fact(
                "STACK_RULE", {"operation": "CAP", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        if NO_MANA_RE.search(phrase):
            out.append(fact(
                "MODIFIER", {"metric": "MANA_COST", "operation": "SET", "value": 0.0},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        if RESET_CD_RE.search(phrase):
            out.append(fact(
                "MECHANIC", {"metric": "COOLDOWN", "operation": "RESET"},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        if NOT_CONSUMED_RE.search(phrase):
            out.append(fact(
                "MECHANIC", {"operation": "DO_NOT_CONSUME"},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Additional named damage modifiers. These run before generic metric
        # inference so phrases such as "periodic damage by 0.25" cannot become RANGE.
        for m in GAIN_BONUS_NAMED_DAMAGE_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric":"DAMAGE_BONUS","operation":"ADD_PERCENT","value":float(m.group(1)),
                 "subject_text":m.group(2).strip()},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))
        for m in INCREASE_NAMED_DAMAGE_PERCENT_RE.finditer(phrase):
            named_metric=metric_from_text(m.group(1))
            if named_metric=="UNKNOWN_STAT": named_metric="DAMAGE"
            out.append(fact(
                "MODIFIER",
                {"metric":named_metric,"operation":"ADD_PERCENT","value":float(m.group(2)),
                 "subject_text":m.group(1).strip()},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))
        for m in INCREASE_NAMED_DAMAGE_FLAT_RE.finditer(phrase):
            named_metric=metric_from_text(m.group(1))
            if named_metric=="UNKNOWN_STAT": named_metric="DAMAGE"
            out.append(fact(
                "MODIFIER",
                {"metric":named_metric,"operation":"ADD_FLAT","value":float(m.group(2)),
                 "subject_text":m.group(1).strip()},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # CC/status effects. Distinguish APPLY from REMOVE/cleanse and from
        # target-state conditions (e.g. "against Slowed Heroes").
        status_remove = bool(STATUS_REMOVE_RE.search(phrase))
        condition_reference = bool(STATUS_CONDITION_PREFIX_RE.search(phrase))
        explicit_apply_verb = bool(re.search(
            r"\b(?:slow|slows|stun|stuns|root|roots|silence|silences|fear|fears|taunt|taunts|pull|pulls|knock|puts?)\b",
            phrase, re.I
        ))
        condition_only = condition_reference and not explicit_apply_verb

        status_matches=[]
        m=STUN_RE.search(phrase)
        if m: status_matches.append(("STUN", {"duration_seconds":float(m.group(1)) if m.group(1) else None}))
        m=SLOW_RE.search(phrase)
        if m: status_matches.append(("SLOW", {"amount_percent":float(m.group(1)) if m.group(1) else None}))
        for label,rx in (("ROOT",ROOT_RE),("SILENCE",SILENCE_RE),("BLIND",BLIND_RE),
                         ("FEAR",FEAR_RE),("TAUNT",TAUNT_RE),("SLEEP",SLEEP_RE),
                         ("KNOCKBACK",KNOCKBACK_RE),("KNOCKUP",KNOCKUP_RE),("PULL",PULL_RE)):
            if rx.search(phrase): status_matches.append((label,{}))

        if status_remove:
            for label,extra in status_matches:
                out.append(fact("STATUS_REMOVE", label, "HIGH", prov_text(phrase), target_filter=filters, **extra))
            if status_matches:
                out.append(fact("STATUS_EFFECT", "CLEANSE", "HIGH", prov_text(phrase), target_filter=filters))
        elif not condition_only:
            for label,extra in status_matches:
                out.append(fact("STATUS_EFFECT", label, "HIGH", prov_text(phrase), target_filter=filters, **extra))

        m = DISABLE_STRUCTURE_RE.search(phrase)
        if m:
            out.append(fact(
                "STATUS_EFFECT", "STRUCTURE_DISABLE", "HIGH", prov_text(phrase),
                duration_seconds=float(m.group(1)) if m.group(1) else None,
                target_filter=["STRUCTURE"],
            ))

        if UNSTOPPABLE_RE.search(phrase):
            out.append(fact("STATUS_EFFECT", "UNSTOPPABLE", "HIGH", prov_text(phrase), target_filter=filters))
        if PROTECTED_RE.search(phrase):
            out.append(fact("STATUS_EFFECT", "PROTECTED", "HIGH", prov_text(phrase), target_filter=filters))
        if STEALTH_RE.search(phrase):
            out.append(fact("STATUS_EFFECT", "STEALTH", "HIGH", prov_text(phrase), target_filter=filters))
        if INVULNERABLE_RE.search(phrase):
            out.append(fact("STATUS_EFFECT", "INVULNERABLE", "HIGH", prov_text(phrase), target_filter=filters))
        if STASIS_RE.search(phrase):
            out.append(fact("STATUS_EFFECT", "STASIS", "HIGH", prov_text(phrase), target_filter=filters))
        if CLEANSE_RE.search(phrase) and not any(e.get("kind")=="STATUS_EFFECT" and e.get("value")=="CLEANSE" and (e.get("provenance") or {}).get("phrase")==phrase for e in out):
            out.append(fact("STATUS_EFFECT", "CLEANSE", "MEDIUM", prov_text(phrase), target_filter=filters))

        # Spawn/summon counts.
        m = SPAWN_COUNT_RE.search(phrase)
        if m:
            out.append(fact(
                "SUMMON_RULE",
                {"operation": "SPAWN_COUNT", "value": int(m.group(1)), "unit_text": m.group(2).strip()},
                "MEDIUM", prov_text(phrase), target_filter=filters,
            ))

    return semantic_dedupe(out)




# ============================================================
# v3.10 CLAUSE + RELATION ENGINE
# ============================================================

SET_FROM_TO_PERCENT_RE = re.compile(
    r"\b(?:increase|increases|increased|change|changes|changed)"
    r"(.{0,80}?)\bfrom\s+(\d+(?:\.\d+)?)%\s+to\s+(\d+(?:\.\d+)?)%",
    re.I,
)
SET_FROM_TO_NUMBER_RE = re.compile(
    r"\b(?:increase|increases|increased|reduce|reduces|reduced|change|changes|changed)"
    r"(.{0,80}?)\bfrom\s+(\d+(?:\.\d+)?)\s+to\s+(\d+(?:\.\d+)?)\b(?!%)",
    re.I,
)
DURATION_BY_RE = re.compile(
    r"\bduration\b.{0,60}?\bby\s+(\d+(?:\.\d+)?)\s+seconds?",
    re.I,
)
AREA_BY_PERCENT_RE = re.compile(
    r"\b(?:increase|increases|increased)"
    r"(.{0,70}?\barea\b.{0,20}?)\bby\s+(\d+(?:\.\d+)?)%",
    re.I,
)
RANGE_BY_PERCENT_RE = re.compile(
    r"\b(?:increase|increases|increased)"
    r"(.{0,70}?\brange\b.{0,20}?)\bby\s+(\d+(?:\.\d+)?)%",
    re.I,
)
CUSTOM_STAT_BY_PERCENT_RE = re.compile(
    r"\b(?:increase|increases|increased|reduce|reduces|reduced)"
    r"\s+([A-Za-z][A-Za-z' -]{2,35}?)\s+by\s+(\d+(?:\.\d+)?)%",
    re.I,
)
CUSTOM_STAT_BY_NUMBER_RE = re.compile(
    r"\b(?:increase|increases|increased|reduce|reduces|reduced)"
    r"\s+([A-Za-z][A-Za-z' -]{2,35}?)\s+by\s+(\d+(?:\.\d+)?)\b(?!%)",
    re.I,
)
DAMAGE_BONUS_BY_PERCENT_RE = re.compile(
    r"\b(?:increase|increases|increased)"
    r"(.{0,70}?\bdamage bonus\b.{0,20}?)\bby\s+(\d+(?:\.\d+)?)%",
    re.I,
)
HEAL_ALLY_FLAT_RE = re.compile(
    r"\b(?:allies|allied heroes?|an allied hero|the ally|target ally)"
    r".{0,50}?\b(?:are healed|is healed|heal(?:ed)?)\s+(?:for\s+)?(\d+(?:\.\d+)?)\b",
    re.I,
)
DAMAGE_ENEMY_FLAT_RE = re.compile(
    r"\b(?:enemies|enemy heroes?|an enemy hero|the enemy)"
    r".{0,50}?\b(?:are damaged|is damaged|take|takes)\s+(?:for\s+)?(\d+(?:\.\d+)?)\b",
    re.I,
)
LESS_DAMAGE_TARGET_RE = re.compile(
    r"\b(?:deal|deals)\s+(\d+(?:\.\d+)?)%\s+less\s+damage\s+to\s+(.+?)(?:[.;]|$)",
    re.I,
)
MORE_WHEN_RE = re.compile(
    r"\b(?:heal|heals|healing|damage|damages?)\s+(?:for\s+)?(\d+(?:\.\d+)?)%\s+more\s+when\s+(.+?)(?:[.;]|$)",
    re.I,
)
PER_EVENT_PERCENT_CAP_RE = re.compile(
    r"\b(?:healing|damage|armor|spell power|attack damage|movement speed|attack speed)"
    r".{0,45}?\b(?:increased|increase|gains?|gain)\s+by\s+(\d+(?:\.\d+)?)%"
    r".{0,100}?\b(?:up to|max(?:imum)?(?: of)?)\s+(\d+(?:\.\d+)?)%",
    re.I,
)
PER_EACH_PERCENT_RE = re.compile(
    r"\b(?:increase|increases|increased)?\s*(.+?)\s+by\s+(\d+(?:\.\d+)?)%"
    r"\s+(?:for|per)\s+(?:each|every)\s+(.+?)(?:[.;]|$)",
    re.I,
)
APPLIES_N_RE = re.compile(
    r"\b(?:apply|applies|grant|grants|add|adds)\s+(\d+)\s+"
    r"(doses?|stacks?|marks?|charges?)\b",
    re.I,
)
ADDITIONAL_DURATION_RE = re.compile(
    r"\bfor\s+an?\s+additional\s+(\d+(?:\.\d+)?)\s+seconds?\b",
    re.I,
)
RECAST_WITHIN_RE = re.compile(
    r"\b(?:cast again|recast|reactivate|use again)"
    r".{0,80}?\bwithin\s+(?:the\s+next\s+)?(\d+(?:\.\d+)?)\s+seconds?\b",
    re.I,
)
UP_TO_PERCENT_RE = re.compile(
    r"\bup to\s+(\d+(?:\.\d+)?)%",
    re.I,
)
LOST_ON_DEATH_RE = re.compile(
    r"\b(?:lost|lose|loses|reset|resets)\s+(?:on|upon)\s+death\b",
    re.I,
)
CUSTOM_STAT_STACK_CAP_RE = re.compile(
    r"\b(?:increase|increases|gain|gains)\s+([A-Za-z][A-Za-z' -]{2,35}?)\s+by\s+"
    r"(\d+(?:\.\d+)?)%.*?\bup to\s+(\d+(?:\.\d+)?)%",
    re.I,
)
ALLIED_HERO_DEATH_RE = re.compile(
    r"\b(?:any time|when|whenever)\s+an?\s+allied\s+hero\s+dies\b",
    re.I,
)


def _custom_metric_name(raw):
    name = re.sub(r"\s+", "_", str(raw or "").strip().upper())
    name = re.sub(r"[^A-Z0-9_]+", "", name)
    known_noise = {
        "THE", "ITS", "HIS", "HER", "THIS", "THAT", "DURATION",
        "AREA", "RANGE", "DAMAGE_BONUS",
    }
    return name if name and name not in known_noise else None



# ============================================================
# v3.12 METRIC CANONICALIZER
# ============================================================
CANONICAL_METRIC_HINTS=[
(re.compile(r"\bspell armor\b",re.I),"SPELL_ARMOR"),
(re.compile(r"\bcooldown\b",re.I),"COOLDOWN"),
(re.compile(r"\bduration\b",re.I),"DURATION"),
(re.compile(r"\b(?:cast )?range\b|\brange of\b|\brange\b",re.I),"RANGE"),
(re.compile(r"\barea\b",re.I),"AREA"),
(re.compile(r"\bradius\b",re.I),"RADIUS"),
(re.compile(r"\bwidth\b",re.I),"WIDTH"),
(re.compile(r"\barmor\b",re.I),"ARMOR"),
(re.compile(r"\battack speed\b",re.I),"ATTACK_SPEED"),
(re.compile(r"\bmovement speed\b|\bmove speed\b",re.I),"MOVEMENT_SPEED"),
(re.compile(r"\bspell power\b",re.I),"SPELL_POWER"),
(re.compile(r"\bbasic attack damage\b|\battack damage\b",re.I),"BASIC_ATTACK_DAMAGE"),
(re.compile(r"\bheal(?:ing)?\b",re.I),"HEALING"),
(re.compile(r"\bshield\b",re.I),"SHIELD"),
(re.compile(r"\bmana cost\b",re.I),"MANA_COST"),
(re.compile(r"\bhealth regeneration\b|\bhealth regen\b",re.I),"HEALTH_REGEN_RATE"),
(re.compile(r"\bmana regeneration\b|\bmana regen\b",re.I),"MANA_REGEN_RATE"),
(re.compile(r"\benergy regeneration\b|\benergy regen\b",re.I),"ENERGY_REGEN_RATE"),
(re.compile(r"\bmana\b",re.I),"MANA"),
(re.compile(r"\benergy\b",re.I),"ENERGY"),
(re.compile(r"\bfury\b",re.I),"FURY"),
(re.compile(r"\bmaximum health\b|\bmax health\b",re.I),"MAX_HEALTH"),
(re.compile(r"\bdamage bonus\b",re.I),"DAMAGE_BONUS"),
(re.compile(r"\bdamage\b",re.I),"DAMAGE"),
]
TRUE_CUSTOM_STATS={"SADISM","HATRED","ANNIHILATION","ESSENCE","DEVOTION","DESPERATION","RAGE","BREW","SOULS","SOUL","MEAT"}

def canonical_metric(metric,phrase=""):
    raw=str(metric or "").strip()
    upper=raw.upper()
    if raw in {"PERCENT_MAX_HEALTH_DAMAGE","PERCENT_CURRENT_HEALTH_DAMAGE","PERCENT_MISSING_HEALTH_DAMAGE",
               "DAMAGE_OVER_TIME","DAMAGE_TAKEN","ENEMY_DAMAGE_DEALT",
               "COOLDOWN_RECHARGE_RATE","CHARGE_RECHARGE_RATE","DAMAGE_TO_HEAL","CHARGES","ADDITIONAL_TARGETS",
               "ADDITIONAL_PROJECTILES","TARGET_COUNT"}:
        return raw,False
    if upper in TRUE_CUSTOM_STATS: return upper,True

    raw_words=raw.replace("_"," ")
    # Critical: classify the metric name itself before looking at a multi-mechanic phrase.
    for rx,canonical in CANONICAL_METRIC_HINTS:
        if rx.search(raw_words): return canonical,False

    compact=re.sub(r"[^A-Z0-9_]+","_",upper).strip("_")
    if compact in TRUE_CUSTOM_STATS: return compact,True

    # Only generic/empty parser metrics may use phrase context.
    if not raw or raw in {"UNKNOWN_STAT","CUSTOM_STAT"}:
        for rx,canonical in CANONICAL_METRIC_HINTS:
            if rx.search(str(phrase or "")): return canonical,False

    if compact and len(compact.split("_"))<=2 and len(compact)<=24:
        return compact,bool(raw)
    return raw or "UNKNOWN_STAT",bool(raw)

def canonicalize_effects(effects):
    rank={"HIGH":3,"MEDIUM":2,"LOW":1,None:0}
    norm=[]
    for e in effects:
        x=dict(e); v=x.get("value"); phrase=(x.get("provenance") or {}).get("phrase") or ""
        if x.get("kind")=="MODIFIER" and isinstance(v,dict):
            nv=dict(v); metric,is_custom=canonical_metric(nv.get("metric"),phrase); nv["metric"]=metric
            if is_custom: nv["custom_stat"]=True
            else: nv.pop("custom_stat",None)
            x["value"]=nv
        norm.append(x)
    best={}; other=[]
    for e in norm:
        if e.get("kind")!="MODIFIER" or not isinstance(e.get("value"),dict):
            other.append(e); continue
        v=e["value"]; key=((e.get("provenance") or {}).get("phrase"),v.get("metric"),v.get("operation"),
                           v.get("value"),v.get("from"),v.get("to"),tuple(e.get("target_filter") or []))
        if key not in best or rank.get(e.get("confidence"),0)>rank.get(best[key].get("confidence"),0): best[key]=e
    return semantic_dedupe(other+list(best.values()))


def infer_v310_effects(text):
    out = []

    for phrase in sentence_phrases(text):
        filters = target_filters_for_phrase(phrase)

        # "increase Slow from 40% to 50%"
        for m in SET_FROM_TO_PERCENT_RE.finditer(phrase):
            metric = metric_from_text(m.group(1))
            if metric == "UNKNOWN_STAT":
                custom = _custom_metric_name(m.group(1))
                metric = custom or "CUSTOM_STAT"
            out.append(fact(
                "MODIFIER",
                {
                    "metric": metric,
                    "operation": "SET_FROM_TO",
                    "from": float(m.group(2)),
                    "to": float(m.group(3)),
                    "unit": "percent",
                },
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in SET_FROM_TO_NUMBER_RE.finditer(phrase):
            metric = metric_from_text(m.group(1))
            if metric == "UNKNOWN_STAT":
                metric = _custom_metric_name(m.group(1)) or "CUSTOM_STAT"
            out.append(fact(
                "MODIFIER",
                {
                    "metric": metric,
                    "operation": "SET_FROM_TO",
                    "from": float(m.group(2)),
                    "to": float(m.group(3)),
                },
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Spatial talent modifiers.
        for m in AREA_BY_PERCENT_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "AREA", "operation": "ADD_PERCENT", "value": float(m.group(2))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in RANGE_BY_PERCENT_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "RANGE", "operation": "ADD_PERCENT", "value": float(m.group(2))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Duration changes are modifiers, not generic duration candidates.
        for m in DURATION_BY_RE.finditer(phrase):
            sign = -1.0 if re.search(r"\breduc\w*\b.{0,60}\bduration\b", phrase, re.I) else 1.0
            out.append(fact(
                "MODIFIER",
                {"metric": "DURATION", "operation": "ADD_SECONDS", "value": sign * float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        for m in ADDITIONAL_DURATION_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "DURATION", "operation": "ADD_SECONDS", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # "damage bonus by 5%"
        for m in DAMAGE_BONUS_BY_PERCENT_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "DAMAGE_BONUS", "operation": "ADD_PERCENT", "value": float(m.group(2))},
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

        # Custom hero stats, e.g. Sadism.
        for m in CUSTOM_STAT_BY_PERCENT_RE.finditer(phrase):
            metric = _custom_metric_name(m.group(1))
            if metric:
                operation = "REDUCE_PERCENT" if re.search(r"\breduc", m.group(0), re.I) else "ADD_PERCENT"
                out.append(fact(
                    "MODIFIER",
                    {"metric": metric, "operation": operation, "value": float(m.group(2)),
                     "custom_stat": True},
                    "MEDIUM", prov_text(phrase), target_filter=filters,
                ))

        for m in CUSTOM_STAT_BY_NUMBER_RE.finditer(phrase):
            metric = _custom_metric_name(m.group(1))
            if metric:
                operation = "REDUCE_FLAT" if re.search(r"\breduc", m.group(0), re.I) else "ADD_FLAT"
                out.append(fact(
                    "MODIFIER",
                    {"metric": metric, "operation": operation, "value": float(m.group(2)),
                     "custom_stat": True},
                    "MEDIUM", prov_text(phrase), target_filter=filters,
                ))

        # Target-scoped compound effects, e.g. Eye of Horus.
        for m in HEAL_ALLY_FLAT_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "HEALING", "operation": "HEAL_FLAT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=["ALLY"],
            ))

        for m in DAMAGE_ENEMY_FLAT_RE.finditer(phrase):
            out.append(fact(
                "MODIFIER",
                {"metric": "DAMAGE", "operation": "DEAL_FLAT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=["ENEMY"],
            ))

        for m in LESS_DAMAGE_TARGET_RE.finditer(phrase):
            tf = target_filters_for_phrase(m.group(2))
            out.append(fact(
                "MODIFIER",
                {"metric": "DAMAGE", "operation": "REDUCE_PERCENT", "value": float(m.group(1))},
                "HIGH", prov_text(phrase), target_filter=tf,
            ))

        # Conditional "20% more when doing so".
        for m in MORE_WHEN_RE.finditer(phrase):
            metric = "HEALING" if "heal" in m.group(0).lower() else "DAMAGE"
            out.append(fact(
                "MODIFIER",
                {"metric": metric, "operation": "ADD_PERCENT", "value": float(m.group(1))},
                "MEDIUM", prov_text(phrase),
                target_filter=filters,
                condition_text=m.group(2).strip(),
            ))

        # Apply N doses/stacks/etc.
        for m in APPLIES_N_RE.finditer(phrase):
            out.append(fact(
                "COUNT_MODIFIER",
                {
                    "metric": m.group(2).upper().rstrip("S"),
                    "operation": "APPLY_COUNT",
                    "value": int(m.group(1)),
                },
                "HIGH", prov_text(phrase), target_filter=filters,
            ))

    return semantic_dedupe(out)


def infer_v310_relations(text, effects, counter_rules, temporal_rules, triggers, conditions):
    """
    Relations are explicit graph edges between already extracted facts.
    They do not invent new numeric facts.
    """
    relations = []
    phrases = sentence_phrases(text)

    for phrase in phrases:
        lower = phrase.lower()

        # Recast/activation window.
        m = RECAST_WITHIN_RE.search(phrase)
        if m:
            relations.append({
                "type": "RECAST_WINDOW",
                "window_seconds": float(m.group(1)),
                "source_phrase": phrase,
                "confidence": "HIGH",
            })

        # Per-event + cap relation.
        m = PER_EVENT_PERCENT_CAP_RE.search(phrase)
        if m:
            relations.append({
                "type": "STACKED_MODIFIER",
                "per_event_percent": float(m.group(1)),
                "cap_percent": float(m.group(2)),
                "source_phrase": phrase,
                "confidence": "HIGH",
            })

        m = PER_EACH_PERCENT_RE.search(phrase)
        if m:
            relations.append({
                "type": "PER_COUNTER_MODIFIER",
                "metric_text": m.group(1).strip(),
                "value_percent": float(m.group(2)),
                "counter_text": m.group(3).strip(),
                "source_phrase": phrase,
                "confidence": "MEDIUM",
            })

        custom_stack = CUSTOM_STAT_STACK_CAP_RE.search(phrase)
        if custom_stack:
            relations.append({
                "type": "CUSTOM_STAT_STACKED_MODIFIER",
                "metric": _custom_metric_name(custom_stack.group(1)) or "CUSTOM_STAT",
                "per_event_percent": float(custom_stack.group(2)),
                "cap_percent": float(custom_stack.group(3)),
                "trigger": "ON_ALLIED_HERO_DEATH" if ALLIED_HERO_DEATH_RE.search(phrase) else None,
                "source_phrase": phrase,
                "confidence": "HIGH",
            })

        # Generic percent cap tied to a custom-stat increase in same phrase.
        cap = UP_TO_PERCENT_RE.search(phrase)
        if cap and re.search(r"\bincrease|gains?|gain\b", phrase, re.I):
            relations.append({
                "type": "CAP",
                "cap_percent": float(cap.group(1)),
                "source_phrase": phrase,
                "confidence": "MEDIUM",
            })

        if LOST_ON_DEATH_RE.search(phrase):
            relations.append({
                "type": "RESET_CONDITION",
                "condition": "ON_DEATH",
                "source_phrase": phrase,
                "confidence": "HIGH",
            })

        # Connect multiple target-scoped effects from the same phrase.
        phrase_effects = [
            e for e in effects
            if (e.get("provenance") or {}).get("phrase") == phrase
        ]
        if len(phrase_effects) > 1:
            relations.append({
                "type": "SIBLING_EFFECTS",
                "count": len(phrase_effects),
                "source_phrase": phrase,
                "confidence": "HIGH",
            })

    return relations


# Preserve proven v3.9 parser and extend it.
_infer_effects_v39 = infer_effects

def infer_effects(text):
    return canonicalize_effects(
        semantic_dedupe(_infer_effects_v39(text) + infer_v310_effects(text))
    )


# ============================================================
# QUEST / COUNTER MODEL
# ============================================================

QUEST_EVENT_RE = re.compile(
    r"\b(?:quest|repeatable quest):?\s*(.+?)(?=(?:reward:|repeatable quest:|$))",
    re.I,
)
REWARD_CLAUSE_RE = re.compile(
    r"\breward:\s*(?:after\s+)?(?:hitting|collecting|gaining|damaging|killing)?\s*"
    r"(\d+(?:\.\d+)?)?\s*([^.;]*?)(?=(?:reward:|repeatable quest:|$))",
    re.I,
)
THRESHOLD_GENERIC_RE = re.compile(
    r"\b(?:after|at|upon reaching)\s+(?:hitting|collecting|gaining|damaging|killing)?\s*"
    r"(\d+(?:\.\d+)?)\s+(?:heroes?|globes?|stacks?|hits?|times?|charges?)\b",
    re.I,
)


def infer_quest(text, talent):
    if not (talent.get("is_quest") or "quest" in text.lower()):
        return []

    thresholds = [float(m.group(1)) for m in THRESHOLD_GENERIC_RE.finditer(text)]

    milestones = []
    for m in REWARD_CLAUSE_RE.finditer(text):
        threshold = float(m.group(1)) if m.group(1) else None
        reward_text = m.group(2).strip(" ,.;")
        reward_effects = infer_effects(reward_text) if reward_text else []
        milestones.append({
            "threshold": threshold,
            "reward_text": reward_text,
            "reward_effects": reward_effects,
        })

    return [fact(
        "QUEST",
        {
            "enabled": True,
            "counter_text": next((m.group(1).strip() for m in QUEST_EVENT_RE.finditer(text)), None),
            "thresholds": thresholds,
            "milestones": milestones,
            "without_dying": bool(WITHOUT_DYING_RE.search(text)),
            "repeatable": "repeatable quest" in text.lower(),
            "uncapped": bool(re.search(r"\bno (?:longer )?cap\b|\buncapped\b", text, re.I)),
        },
        "HIGH" if talent.get("is_quest") else "MEDIUM",
        {"source_type": "HERODATA_STRUCTURED" if talent.get("is_quest")
         else "HERODATA_TEXT_INFERRED", "field": "is_quest/full_text"},
    )]


def infer_stacking(text):
    m = CAP_STACKS_RE.search(text)
    if "stack" not in text.lower() and not m:
        return []

    return [fact(
        "STACKING",
        {
            "type": "COUNTER_OR_BUFF_STACK",
            "max_stacks": float(m.group(1)) if m else None,
            "uncapped": bool(re.search(r"\bno (?:longer )?cap\b|\buncapped\b", text, re.I)),
            "reset_on_death": bool(WITHOUT_DYING_RE.search(text)),
        },
        "MEDIUM", prov_text(),
    )]


# ============================================================
# XML EVIDENCE WITH OBJECT-LEVEL DEDUP
# ============================================================

XML_INTERESTING_TAGS = {
    "Modification", "DamageResponse", "VitalArray", "RateMultiplierArray",
    "Duration", "Period", "MaxStackCount", "MaxStackCountPerCaster",
    "StackAlias", "Amount", "Chance", "Cooldown", "TimeUse", "Charge",
    "CountMax", "CountUse", "Range", "Radius", "Width", "Arc",
}


def xml_candidate_names(talent):
    out = []
    for field in ("talent_id", "ability_id", "button_id", "link_id"):
        value = talent.get(field)
        if value:
            out.append((str(value), field))
            out.append((str(value).split("|", 1)[0], field))

    for value in talent.get("tooltip_ability_link_ids", []) or []:
        out.append((str(value), "tooltip_ability_link_id"))
        out.append((str(value).split("|", 1)[0], "tooltip_ability_link_id"))

    return [(n, f) for n, f in out if n]


def relation_for_source_fields(object_id, source_fields, talent, final_class):
    tid = str(talent.get("talent_id") or "")
    aid = str(talent.get("ability_id") or "")

    if object_id == tid:
        return "DIRECT_TALENT_XML"

    if "ability_id" in source_fields:
        if final_class in {
            "BASIC_ATTACK_MODIFIER", "SYSTEM_MODIFIER", "RESOURCE_MODIFIER",
            "STAT_MODIFIER", "SUMMON_MODIFIER",
        } and object_id == aid and object_id != tid:
            return "SUSPECT_ALIAS"
        return "ABILITY_FAMILY_XML"

    if "tooltip_ability_link_id" in source_fields:
        return "ABILITY_FAMILY_XML"

    if source_fields & {"button_id", "link_id"}:
        return "LINKED_CATALOG_XML"

    return "SUSPECT_ALIAS"


def extract_xml_evidence(objects, talent, final_class):
    grouped = {}

    for name, source_field in xml_candidate_names(talent):
        for d in objects.get(name, []):
            key = (d.get("object_id"), d.get("xml_type"), d.get("file"))
            row = grouped.setdefault(key, {
                "object_id": d.get("object_id"),
                "xml_type": d.get("xml_type"),
                "file": d.get("file"),
                "source_fields": set(),
                "numeric_fields": [],
            })
            row["source_fields"].add(source_field)

            existing_nf = {
                (x.get("tag"), x.get("attr"), x.get("value"), tuple(x.get("path") or []))
                for x in row["numeric_fields"]
            }

            for node in descendants(d):
                if str(node.get("tag") or "") not in XML_INTERESTING_TAGS:
                    continue
                for attr_name, raw in (node.get("attrs") or {}).items():
                    value = const_value(objects, raw)
                    if value is None:
                        continue
                    nf_key = (node.get("tag"), attr_name, value, tuple(node.get("path") or []))
                    if nf_key in existing_nf:
                        continue
                    existing_nf.add(nf_key)
                    row["numeric_fields"].append({
                        "tag": node.get("tag"),
                        "attr": attr_name,
                        "value": value,
                        "raw": raw,
                        "path": node.get("path", []),
                    })

    out = []
    for row in grouped.values():
        source_fields = set(row.pop("source_fields"))
        row["source_fields"] = sorted(source_fields)
        row["relation"] = relation_for_source_fields(
            str(row.get("object_id") or ""), source_fields, talent, final_class
        )
        row["numeric_fields"] = row["numeric_fields"][:200]
        out.append(row)

    return out


# ============================================================
# NUMERIC COVERAGE
# ============================================================

def tooltip_numeric_tokens(text):
    tokens = []
    for m in NUM_TOKEN_RE.finditer(text):
        raw = m.group(0)
        value = float(m.group(1))
        unit_raw = (m.group(2) or "").lower()
        unit = None
        if "%" in raw or unit_raw == "%":
            unit = "percent"
        elif unit_raw in {"s", "sec", "secs", "second", "seconds"}:
            unit = "seconds"

        tokens.append({
            "raw": raw,
            "value": value,
            "unit": unit,
            "span": [m.start(), m.end()],
        })
    return tokens


def collect_explained_numbers(effects,internal_cd,quest,stacking,triggers,conditions,temporal,counter_rules,relations,quest_v2=None):
    explained=[]
    def add(v,unit=None,reason=None,phrase=None,match_abs=False,metric=None,operation=None):
        if isinstance(v,(int,float)):
            explained.append({"value":float(v),"unit":unit,"reason":reason,"phrase":phrase,
                              "match_abs":bool(match_abs),"metric":metric,"operation":operation})
    for item in effects:
        value=item.get("value"); phrase=(item.get("provenance") or {}).get("phrase")
        if item.get("kind")=="MODIFIER" and isinstance(value,dict):
            op=str(value.get("operation") or ""); metric=value.get("metric")
            if "value" in value:
                unit=None
                if "PERCENT" in op or op in {"RATIO_PERCENT","DEAL_PERCENT","SLOW_RATE_PERCENT"}: unit="percent"
                elif "SECONDS" in op or (metric=="DURATION" and op=="ADD_SECONDS"): unit="seconds"
                val=value.get("value")
                match_abs=isinstance(val,(int,float)) and float(val)<0 and op in {"ADD_SECONDS","ADD_FLAT","REDUCE_FLAT"}
                add(val,unit,f"MODIFIER:{metric}:{op}",phrase,match_abs,metric,op)
            ftunit="percent" if value.get("unit")=="percent" else None
            add(value.get("from"),ftunit,f"MODIFIER:{metric}:FROM",phrase,False,metric,"FROM")
            add(value.get("to"),ftunit,f"MODIFIER:{metric}:TO",phrase,False,metric,"TO")
        elif item.get("kind")=="COUNT_MODIFIER" and isinstance(value,dict):
            add(value.get("value"),None,f"COUNT:{value.get('metric')}:{value.get('operation')}",phrase)
        if item.get("kind")=="STATUS_EFFECT":
            add(item.get("duration_seconds"),"seconds",f"STATUS:{value}",phrase)
            add(item.get("amount_percent"),"percent",f"STATUS:{value}",phrase)
        if item.get("kind")=="STACK_RULE" and isinstance(value,dict): add(value.get("value"),None,f"STACK_RULE:{value.get('operation')}",phrase)
        if item.get("kind")=="SUMMON_RULE" and isinstance(value,dict): add(value.get("value"),None,"SUMMON_COUNT",phrase)
    for item in internal_cd: add(item.get("value"),"seconds","INTERNAL_COOLDOWN",(item.get("provenance") or {}).get("phrase"))
    for item in temporal:
        phrase=(item.get("provenance") or {}).get("phrase")
        add(item.get("period_seconds"),"seconds","PERIOD",phrase); add(item.get("duration_seconds"),"seconds","DURATION",phrase); add(item.get("delay_seconds"),"seconds","DELAY",phrase)
    for item in quest:
        value=item.get("value")
        if isinstance(value,dict):
            for v in value.get("thresholds",[]) or []: add(v,None,"QUEST_THRESHOLD")
            for ms in value.get("milestones",[]) or []: add(ms.get("threshold"),None,"QUEST_MILESTONE")
    for item in stacking:
        value=item.get("value")
        if isinstance(value,dict): add(value.get("max_stacks"),None,"MAX_STACKS")
    for item in triggers: add(item.get("n"),None,f"TRIGGER:{item.get('value')}")
    for item in conditions: add(item.get("threshold_percent"),"percent",f"CONDITION:{item.get('value')}")
    for item in counter_rules:
        value=item.get("value"); phrase=(item.get("provenance") or {}).get("phrase")
        if isinstance(value,dict):
            add(value.get("required_count"),None,f"COUNTER:{value.get('type')}",phrase)
            add(value.get("window_seconds"),"seconds",f"COUNTER_WINDOW:{value.get('type')}",phrase)
            add(value.get("threshold"),None,f"COUNTER_THRESHOLD:{value.get('type')}",phrase)
            add(value.get("max_procs"),None,f"COUNTER_MAX:{value.get('type')}",phrase)
            add(value.get("count"),None,f"COUNTER_COUNT:{value.get('type')}",phrase)
    for rel in relations:
        phrase=rel.get("source_phrase")
        add(rel.get("window_seconds"),"seconds",f"RELATION:{rel.get('type')}",phrase)
        add(rel.get("per_event_percent"),"percent",f"RELATION:{rel.get('type')}",phrase)
        add(rel.get("value_percent"),"percent",f"RELATION:{rel.get('type')}",phrase)
        add(rel.get("cap_percent"),"percent",f"RELATION:{rel.get('type')}",phrase)
    for q in quest_v2_explained_numbers(quest_v2): add(q.get("value"),q.get("unit"),q.get("reason"))
    return explained

def _number_equal(tv,ex):
    ev=ex.get("value")
    if not isinstance(ev,(int,float)): return False
    if abs(float(ev)-float(tv))<=1e-9: return True
    return bool(ex.get("match_abs")) and abs(abs(float(ev))-abs(float(tv)))<=1e-9

def _unit_score(tu,eu):
    if tu and eu and tu!=eu:return -100
    if tu and eu and tu==eu:return 4
    if not tu and eu:return 1
    if tu and not eu:return 0
    return 2

def _phrase_distance(text,token,phrase):
    if not text or not phrase:return 10000
    pos=[m.start() for m in re.finditer(re.escape(phrase),text,re.I)]
    if not pos:
        frag=phrase[:50].strip(); pos=[m.start() for m in re.finditer(re.escape(frag),text,re.I)] if frag else []
    if not pos:return 10000
    center=(token["span"][0]+token["span"][1])/2
    return min(abs(center-(s+len(phrase)/2)) for s in pos)

def diagnose_missing_token(token,explained,text=""):
    same=[ex for ex in explained if isinstance(ex.get("value"),(int,float)) and abs(abs(float(ex["value"]))-abs(float(token["value"])))<=1e-9]
    if same:
        if all(_unit_score(token.get("unit"),ex.get("unit"))<0 for ex in same):return "UNIT_MISMATCH"
        if any(float(ex["value"])==-float(token["value"]) for ex in same):return "SIGN_NORMALIZATION_FAILED"
        if len(same)>1:return "AMBIGUOUS_MULTIPLE_FACTS"
        return "CONTEXT_BINDING_FAILED"
    lo=max(0,token["span"][0]-70); hi=min(len(text),token["span"][1]+70); ctx=text[lo:hi].lower()
    if re.search(r"\bquest\b|\breward\b",ctx):return "NO_QUEST_FACT"
    if re.search(r"\bcooldown\b|\bduration\b|\barmor\b|\bdamage\b|\bheal\b|\bhealth\b|\bmana\b|\brange\b|\bstack\b|\bcharge\b",ctx):return "NO_FACT"
    return "UNKNOWN_CONTEXT"

def match_numeric_coverage(tokens,explained,text=""):
    matched=[];missing=[];used=set()
    for token in tokens:
        cand=[]
        for i,ex in enumerate(explained):
            if i in used or not _number_equal(token["value"],ex):continue
            us=_unit_score(token.get("unit"),ex.get("unit"))
            if us<0:continue
            dist=_phrase_distance(text,token,ex.get("phrase")); cs=0 if dist>=10000 else max(0,6-min(6,int(dist//40)))
            ap=-1 if ex.get("match_abs") and float(ex["value"])!=float(token["value"]) else 0
            cand.append((us+cs+ap,-dist,i,ex))
        if cand:
            cand.sort(reverse=True,key=lambda x:(x[0],x[1]));_,_,i,ex=cand[0];used.add(i);matched.append({"token":token,"explained_by":ex})
        else:
            x=dict(token);x["diagnostic"]=diagnose_missing_token(token,explained,text);missing.append(x)
    ratio=1.0 if not tokens else len(matched)/len(tokens);diag=Counter(x.get("diagnostic") for x in missing)
    return {"tokens_total":len(tokens),"tokens_explained":len(matched),"coverage_ratio":round(ratio,4),
            "matched":matched,"missing":missing,"missing_diagnostics":dict(diag)}



# ============================================================
# v3.11 QUEST ENGINE v2
# Structured counter -> milestone -> reward state machine.
# This extends, rather than replaces, the proven v3.10 facts.
# ============================================================

QUEST_AFTER_RE = re.compile(
    r"\b(?:reward|mythic reward|repeatable reward)?\s*:?\s*after\s+"
    r"(?:collecting|gathering|hitting|hit|reducing damage|sliming|gaining|applying|damaging|healing|killing|"
    r"using|earning|reaching|absorbing|granting)\s+"
    r"(\d+(?:\.\d+)?)\s+([^.;]+)",
    re.I,
)
QUEST_TOTAL_RE = re.compile(
    r"\b(?:for\s+)?a\s+total\s+of\s+(\d+(?:\.\d+)?)\s*(seconds?|times?|stacks?|heroes?|damage|healing)?",
    re.I,
)
QUEST_HIT_SINGLE_RE = re.compile(
    r"\bhit\s+(?:at least\s+)?(\d+)\s+(?:enemy\s+)?heroes?\s+with\s+a\s+single\s+([^.;]+)",
    re.I,
)
QUEST_LOSE_STACKS_RE = re.compile(
    r"\b(?:upon\s+dying|on\s+death|upon\s+death).{0,30}?\blose\s+(\d+)\s+stacks?",
    re.I,
)
QUEST_CAP_RE = re.compile(
    r"\b(?:up to|maximum of|max(?:imum)?(?: of)?)\s+(\d+(?:\.\d+)?)\s*(%|stacks?|charges?|damage|health|mana)?",
    re.I,
)
QUEST_PER_EVENT_RE = re.compile(
    r"\b(?:every time|each time|each additional time|whenever)\s+(.{0,100}?)"
    r"\b(?:increase|increases|gain|gains|grant|grants|reduce|reduces)\b(.{0,100}?)"
    r"\bby\s+(\d+(?:\.\d+)?)%?",
    re.I,
)
QUEST_REWARD_INCREASE_RE = re.compile(
    r"\b(?:reward|mythic reward|repeatable reward)\s*:\s*(?:permanently\s+)?"
    r"(?:increase|increases|gain|gains)\s+(.{1,80}?)\s+by\s+(\d+(?:\.\d+)?)\s*(%|seconds?)?",
    re.I,
)
QUEST_REWARD_SET_RE = re.compile(
    r"\b(?:reward|mythic reward|repeatable reward)\s*:\s*after\s+(.{0,100}?),\s*"
    r"(?:increase|set|reduce)(.{1,80}?)\s+(?:to|by)\s+(\d+(?:\.\d+)?)\s*(%|seconds?)?",
    re.I,
)
QUEST_INSTANT_ALL_REWARDS_RE = re.compile(
    r"\bhit\s+(\d+)\s+(?:enemy\s+)?heroes?.{0,80}?\b(?:instantly\s+)?gain\s+all\s+(?:other\s+)?rewards",
    re.I,
)
QUEST_REPEATABLE_RE = re.compile(r"\brepeatable\s+(?:quest|reward)\b", re.I)
QUEST_MYTHIC_RE = re.compile(r"\bmythic\s+reward\b", re.I)


def _quest_counter_unit(fragment):
    s = str(fragment or "").lower()
    if "second" in s:
        return "SECONDS"
    if "hero" in s:
        return "HERO_HITS"
    if "globe" in s:
        return "GLOBES"
    if "mineral" in s:
        return "MINERALS"
    if "stack" in s:
        return "STACKS"
    if "damage" in s:
        return "DAMAGE"
    if "heal" in s:
        return "HEALING"
    if "kill" in s:
        return "KILLS"
    return "COUNT"


def infer_quest_v2(text, effects, conditions, triggers):
    if not re.search(r"\bquest\b|\breward\b|\bmythic reward\b", text or "", re.I):
        return None

    phrases = sentence_phrases(text)
    model = {
        "enabled": True,
        "counter": {
            "event": None,
            "unit": None,
            "target_filter": [],
            "same_cast": False,
            "amount_per_event": None,
        },
        "cap": None,
        "death_penalty": None,
        "milestones": [],
        "repeatable": bool(QUEST_REPEATABLE_RE.search(text or "")),
        "mythic": bool(QUEST_MYTHIC_RE.search(text or "")),
        "completion_shortcuts": [],
        "source": "HERODATA_TEXT_INFERRED_V3_11",
    }

    # Counter/event inference.
    low = (text or "").lower()
    if "collect" in low or "gather" in low:
        model["counter"]["event"] = "COLLECT"
    elif "hit" in low or "damage" in low:
        model["counter"]["event"] = "HIT"
    elif "heal" in low:
        model["counter"]["event"] = "HEAL"
    elif "kill" in low:
        model["counter"]["event"] = "KILL"
    elif "reduce damage" in low:
        model["counter"]["event"] = "MITIGATE"

    if "regeneration globe" in low:
        model["counter"]["unit"] = "GLOBES"
    elif "mineral" in low:
        model["counter"]["unit"] = "MINERALS"
    elif "hero" in low:
        model["counter"]["unit"] = "HERO_HITS"
        model["counter"]["target_filter"] = ["HERO"]

    # "for a total of 150 seconds"
    for m in QUEST_TOTAL_RE.finditer(text or ""):
        threshold = float(m.group(1))
        unit = _quest_counter_unit(m.group(2) or m.group(0))
        model["counter"]["unit"] = unit
        model["milestones"].append({
            "threshold": threshold,
            "unit": unit,
            "kind": "QUEST_TARGET",
            "reward_effects": [],
            "source_phrase": m.group(0),
        })

    # "Hit at least 2 Heroes with a single ..."
    for m in QUEST_HIT_SINGLE_RE.finditer(text or ""):
        threshold = int(m.group(1))
        model["counter"]["same_cast"] = True
        model["counter"]["unit"] = "HERO_HITS"
        model["counter"]["target_filter"] = ["HERO"]
        model["milestones"].append({
            "threshold": threshold,
            "unit": "HERO_HITS",
            "kind": "SINGLE_CAST_REQUIREMENT",
            "reward_effects": [],
            "source_phrase": m.group(0),
        })

    # Explicit after-N milestones. Preserve phrase so later layers can bind rewards.
    for phrase in phrases:
        # broad "After <verb> N ..." form
        ma = re.search(
            r"\bafter\s+(?:collecting|gathering|hitting|sliming|gaining|reducing damage|"
            r"damaging|healing|killing|applying|earning|absorbing)\s+"
            r"(\d+(?:\.\d+)?)\s+([^,.;]+)",
            phrase, re.I
        )
        if ma:
            threshold = float(ma.group(1))
            unit = _quest_counter_unit(ma.group(2))
            kind = "MYTHIC_MILESTONE" if "mythic reward" in phrase.lower() else "MILESTONE"
            model["milestones"].append({
                "threshold": threshold,
                "unit": unit,
                "kind": kind,
                "reward_effects": [],
                "source_phrase": phrase,
            })

        # "After reducing damage 40 times"
        mb = re.search(r"\bafter\s+reducing\s+damage\s+(\d+(?:\.\d+)?)\s+times?", phrase, re.I)
        if mb:
            model["milestones"].append({
                "threshold": float(mb.group(1)),
                "unit": "TIMES",
                "kind": "MILESTONE",
                "reward_effects": [],
                "source_phrase": phrase,
            })

    # Caps.
    caps = list(QUEST_CAP_RE.finditer(text or ""))
    if caps:
        m = caps[-1]
        model["cap"] = {
            "value": float(m.group(1)),
            "unit": _quest_counter_unit(m.group(2) or m.group(0)),
            "source_phrase": m.group(0),
        }

    # Death stack penalty.
    m = QUEST_LOSE_STACKS_RE.search(text or "")
    if m:
        model["death_penalty"] = {
            "operation": "LOSE_STACKS",
            "value": int(m.group(1)),
            "condition": "ON_DEATH",
            "source_phrase": m.group(0),
        }

    # Per-event accumulation.
    for m in QUEST_PER_EVENT_RE.finditer(text or ""):
        model["counter"]["amount_per_event"] = float(m.group(3))

    # Bind extracted effects to the closest milestone by provenance phrase.
    for effect in effects or []:
        prov_phrase = ((effect.get("provenance") or {}).get("phrase") or "")
        if not prov_phrase:
            continue
        candidates = []
        for i, ms in enumerate(model["milestones"]):
            sp = ms.get("source_phrase") or ""
            score = 0
            if sp == prov_phrase:
                score = 3
            elif sp and sp in prov_phrase:
                score = 2
            elif prov_phrase and prov_phrase in sp:
                score = 2
            elif any(tok in prov_phrase.lower() for tok in ["reward:", "mythic reward:", "repeatable reward:"]):
                score = 1
            if score:
                candidates.append((score, i))
        if candidates:
            _, idx = max(candidates)
            model["milestones"][idx]["reward_effects"].append(effect)

    # Completion shortcut: "Hit 4 Heroes ... gain all Rewards".
    for m in QUEST_INSTANT_ALL_REWARDS_RE.finditer(text or ""):
        model["completion_shortcuts"].append({
            "threshold": int(m.group(1)),
            "unit": "HERO_HITS",
            "same_cast": True,
            "operation": "GRANT_ALL_REWARDS",
            "source_phrase": m.group(0),
        })

    # Deduplicate milestones while retaining the richest one.
    dedup = {}
    for ms in model["milestones"]:
        key = (ms.get("threshold"), ms.get("unit"), ms.get("kind"))
        old = dedup.get(key)
        if old is None or len(ms.get("reward_effects") or []) > len(old.get("reward_effects") or []):
            dedup[key] = ms
    model["milestones"] = list(dedup.values())

    return model


def quest_v2_explained_numbers(model):
    out = []
    if not model:
        return out

    def add(value, unit, reason):
        if value is None:
            return
        out.append({
            "value": float(value),
            "unit": unit,
            "reason": reason,
        })

    counter = model.get("counter") or {}
    add(counter.get("amount_per_event"), None, "QUEST_V2:AMOUNT_PER_EVENT")

    cap = model.get("cap")
    if cap:
        add(cap.get("value"), None, "QUEST_V2:CAP")

    penalty = model.get("death_penalty")
    if penalty:
        add(penalty.get("value"), None, "QUEST_V2:DEATH_PENALTY")

    for ms in model.get("milestones") or []:
        add(ms.get("threshold"), None, "QUEST_V2:MILESTONE")

    for sc in model.get("completion_shortcuts") or []:
        add(sc.get("threshold"), None, "QUEST_V2:COMPLETION_SHORTCUT")

    return out


# ============================================================
# COMPLETENESS
# ============================================================

def talent_completeness(final_class, effects, triggers, internal_cd, quest, coverage):
    if final_class == "UNRESOLVED":
        return "UNRESOLVED", "UNKNOWN_TARGET"

    if coverage["tokens_total"] > 0 and coverage["coverage_ratio"] < 0.5:
        return "PARTIAL", "LOW_NUMERIC_COVERAGE"

    if effects or triggers or internal_cd or quest:
        if coverage["coverage_ratio"] >= 0.8 or coverage["tokens_total"] == 0:
            return "COMPLETE", "MECHANICS_AND_NUMBERS_COVERED"
        return "PARTIAL", "MECHANICS_FOUND_NUMBERS_PARTIAL"

    return "PARTIAL", "TARGET_KNOWN_MECHANICS_MISSING"


def review_priority(completeness, reason):
    if completeness == "UNRESOLVED":
        return "ERROR"
    if reason in {"LOW_NUMERIC_COVERAGE", "TARGET_KNOWN_MECHANICS_MISSING"}:
        return "NEEDS_RULE"
    if reason == "MECHANICS_FOUND_NUMBERS_PARTIAL":
        return "LOW_NUMERIC_COVERAGE"
    return "INFO_ONLY"


# ============================================================
# RUNNER
# ============================================================

def run_talent_modifier_parser(
    catalog_path,
    herodata_path,
    talent_facts_path,
    output_path,
    report_path,
):
    catalog = load(catalog_path)
    talent_facts = load(talent_facts_path)
    objects = catalog.get("objects", {})

    heroes = {}
    counts = Counter()
    class_counts = Counter()
    target_counts = Counter()
    priority_counts = Counter()
    xml_relation_counts = Counter()
    missing_diagnostic_counts = Counter()
    review = []

    for hero, talents in talent_facts.get("heroes", {}).items():
        rows = []

        for talent in talents:
            counts["talents_total"] += 1
            text = merged_talent_text(talent)

            targets = base_targets(talent)
            targets, corrections = semantic_correct_targets(talent, targets, text)
            final_class = final_classification(targets)

            triggers = infer_triggers(text)
            conditions = infer_conditions(text)
            effects = infer_effects(text)
            effects = [enrich_modifier_damage_semantics(e, targets) for e in effects]
            internal_cd = infer_internal_cooldown(text)
            temporal = infer_temporal_rules(text, internal_cd)
            temporal += infer_windows_and_failures(text)
            temporal = semantic_dedupe(temporal)
            counter_rules = infer_counter_rules(text)
            relations = infer_v310_relations(
                text, effects, counter_rules, temporal, triggers, conditions
            )
            quest_v2 = infer_quest_v2(
                text, effects, conditions, triggers
            )
            quest = infer_quest(text, talent)
            stacking = infer_stacking(text)
            xml_evidence = extract_xml_evidence(objects, talent, final_class)

            numeric_tokens = tooltip_numeric_tokens(text)
            explained = collect_explained_numbers(
                effects, internal_cd, quest, stacking, triggers, conditions, temporal, counter_rules, relations, quest_v2
            )
            coverage = match_numeric_coverage(numeric_tokens, explained, text=text)

            completeness, completeness_reason = talent_completeness(
                final_class, effects, triggers, internal_cd, quest, coverage
            )
            priority = review_priority(completeness, completeness_reason)

            counts[completeness.lower()] += 1
            if triggers: counts["with_triggers"] += 1
            if conditions: counts["with_conditions"] += 1
            if effects: counts["with_effects"] += 1
            if xml_evidence: counts["with_xml_evidence"] += 1
            if quest: counts["with_quest"] += 1
            if internal_cd: counts["with_internal_cooldown"] += 1
            if temporal: counts["with_temporal_rules"] += 1
            if counter_rules: counts["with_counter_rules"] += 1
            if relations: counts["with_relations"] += 1
            if quest_v2: counts["with_quest_v2"] += 1
            if corrections: counts["with_semantic_correction"] += 1

            class_counts[final_class] += 1
            priority_counts[priority] += 1
            missing_diagnostic_counts.update(coverage.get("missing_diagnostics") or {})
            for t in targets:
                target_counts[str(t.get("type"))] += 1
            for x in xml_evidence:
                xml_relation_counts[str(x.get("relation"))] += 1

            row = {
                "hero": hero,
                "talent_id": talent.get("talent_id"),
                "name": talent.get("name"),
                "level": talent.get("level"),
                "resolver_status_original": talent.get("status"),
                "final_classification": final_class,
                "semantic_corrections": corrections,
                "targets": targets,
                "triggers": triggers,
                "conditions": conditions,
                "effects": effects,
                "temporal_rules": temporal,
                "counter_rules": counter_rules,
                "relations": relations,
                "quest_v2": quest_v2,
                "stacking": stacking,
                "internal_cooldown": internal_cd,
                "quest": quest,
                "xml_evidence": xml_evidence,
                "numeric_coverage": coverage,
                "completeness": completeness,
                "completeness_reason": completeness_reason,
                "review_priority": priority,
                "raw_text": {
                    "normalized": text,
                    "short_text": talent.get("short_text"),
                    "full_text": talent.get("full_text"),
                },
            }
            rows.append(row)

            if completeness != "COMPLETE":
                review.append({
                    "priority": priority,
                    "hero": hero,
                    "talent_id": talent.get("talent_id"),
                    "name": talent.get("name"),
                    "final_classification": final_class,
                    "reason": completeness_reason,
                    "missing_numbers": coverage.get("missing", [])[:20],
                })

        heroes[hero] = rows

    data = {
        "metadata": {
            "builder": "talent_modifier_parser_v3_12_7_damage_sources",
            "schema_version": "3.12.7",
        },
        "heroes": heroes,
    }

    summary = {
        "talents_total": counts["talents_total"],
        "complete": counts["complete"],
        "partial": counts["partial"],
        "unresolved": counts["unresolved"],
        "with_triggers": counts["with_triggers"],
        "with_conditions": counts["with_conditions"],
        "with_effects": counts["with_effects"],
        "with_xml_evidence": counts["with_xml_evidence"],
        "with_quest": counts["with_quest"],
        "with_internal_cooldown": counts["with_internal_cooldown"],
        "with_temporal_rules": counts["with_temporal_rules"],
        "with_counter_rules": counts["with_counter_rules"],
        "with_relations": counts["with_relations"],
        "with_quest_v2": counts["with_quest_v2"],
        "with_semantic_correction": counts["with_semantic_correction"],
        "review_queue": len(review),
    }

    report = {
        "summary": summary,
        "final_classifications": dict(class_counts),
        "target_types": dict(target_counts),
        "review_priorities": dict(priority_counts),
        "xml_relation_counts": dict(xml_relation_counts),
        "missing_diagnostics": dict(missing_diagnostic_counts),
        "review": review,
    }

    save(output_path, data)
    save(report_path, report)
    return report
