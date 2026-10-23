from __future__ import annotations
import re
from collections import Counter, defaultdict
from fact_utils import load, save
NUM_RE=re.compile(r"\d+(?:\.\d+)?%?")
SPACE_RE=re.compile(r"\s+")
RULES=[
("PERCENT_MAX_HEALTH",r"\[n\]%.*?(?:maximum|max) health"),
("PERCENT_CURRENT_HEALTH",r"\[n\]%.*?current health"),
("COMBO_WINDOW",r"\b(?:abilities?|hits?).*?\[n\].*?within \[n\] seconds"),
("COUNTER_THRESHOLD",r"\b(?:after|at|upon|hitting|hit).*?\[n\].*?(?:heroes?|hits?|stacks?|times?)"),
("FLAT_DAMAGE",r"\b(?:deal|deals|take|takes).*?\[n\] damage\b"),
("FLAT_HEAL",r"\bheal.*?\[n\].*?(?:health|hp)\b"),
("ARMOR",r"\b(?:spell )?armor\b"),("COOLDOWN",r"\bcooldown\b|\brecharge\b"),
("DURATION",r"\b(?:for|over|up to) \[n\] seconds?\b"),("ACTIVATION_WINDOW",r"\bwithin \[n\] seconds?\b"),
("PERIODIC",r"\bevery \[n\] seconds?\b"),("QUEST",r"\bquest\b|\breward\b"),
("RESOURCE",r"\bmana\b|\benergy\b|\bfury\b"),("RANGE",r"\brange\b"),("STACKS",r"\bstacks?\b|\bstacking\b"),
("CHARGES",r"\bcharges?\b"),("SPEED",r"\battack speed\b|\bmovement speed\b"),("SHIELD",r"\bshield\b"),
("REGEN_RATE",r"\bper second\b|\bregeneration\b"),("DAMAGE_TO_HEAL",r"\bheal.*?\[n\]%.*?damage dealt\b"),
("STAT_MODIFIER",r"\bspell power\b|\battack damage\b|\bhealth\b"),
("ADDITIONAL_TARGETS",r"\badditional.*?(?:targets?|heroes?|enemies|projectiles?|shots?|vines?)"),
("PROC_LIMIT",r"\b(?:up to|maximum|max).*?\[n\].*?(?:times?|procs?|uses?|activations?)"),
("SET_FROM_TO",r"\bfrom \[n\]%? to \[n\]%?\b"),
("CUSTOM_STAT",r"\b(?:sadism|hatred|fury|trait|bonus)\b.*?\[n\]%"),
("AREA_MODIFIER",r"\barea\b.*?\[n\]%"),
("DURATION_MODIFIER",r"\bduration\b.*?\[n\].*?seconds"),
("TARGET_SCOPED_EFFECT",r"\b(?:allies|enemies|structures)\b.*?\[n\]"),
("PER_COUNTER",r"\b(?:per|for each)\b.*?\[n\]%|\[n\]%.*?(?:per|for each)\b"),
("RECAST_WINDOW",r"\b(?:cast again|recast|reactivate).*?within.*?\[n\] seconds"),
("SUMMON_COUNT",r"\b(?:spawn|summon|create).*?\[n\]\b"),]

def normalized_context(text,start,end,radius=75):
    lo=max(0,start-radius); hi=min(len(text),end+radius); c=NUM_RE.sub("[N]",text[lo:hi]); return SPACE_RE.sub(" ",c).strip().lower()

def coarse_pattern(context):
    for name,pat in RULES:
        if re.search(pat,context,re.I): return name
    return "UNKNOWN_NUMERIC_CONTEXT"

def run_rule_miner(talent_modifier_facts_path,report_path):
    data=load(talent_modifier_facts_path); coarse=Counter(); exact=Counter(); examples=defaultdict(list); diagnostics=Counter(); twm=0; mt=0
    for hero,rows in data.get("heroes",{}).items():
        for row in rows:
            missing=(row.get("numeric_coverage") or {}).get("missing") or []
            if not missing: continue
            twm+=1; text=(row.get("raw_text") or {}).get("normalized") or ""
            for token in missing:
                diagnostics[token.get('diagnostic') or 'UNSPECIFIED'] += 1
                mt+=1; span=token.get("span") or [0,0]; context=normalized_context(text,span[0],span[1]); bucket=coarse_pattern(context); coarse[bucket]+=1; exact[context]+=1
                if len(examples[bucket])<50: examples[bucket].append({"hero":hero,"talent_id":row.get("talent_id"),"name":row.get("name"),"missing_token":token,"context":context})
    report={"summary":{"talents_with_missing_numbers":twm,"missing_numeric_tokens":mt,"unknown_numeric_contexts":coarse["UNKNOWN_NUMERIC_CONTEXT"]},"coarse_missing_mechanics":coarse.most_common(),"missing_diagnostics":diagnostics.most_common(),"top_normalized_contexts":exact.most_common(250),"examples_by_mechanic":dict(examples)}
    save(report_path,report); return report
