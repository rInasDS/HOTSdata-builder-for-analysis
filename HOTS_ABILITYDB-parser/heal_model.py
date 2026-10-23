from __future__ import annotations
import re

NUM=r"(\d+(?:\.\d+)?)(?!\d|\.\d)"

def _clean(text):
    s=str(text or "")
    s=re.sub(r"<n\s*/?>",". ",s,flags=re.I)
    s=re.sub(r"<[^>]+>","",s)
    s=re.sub(r"~~\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*~~","",s)
    return re.sub(r"\s+"," ",s).strip()

def _sentence(text,pos):
    bounds=[m.start() for m in re.finditer(r"(?<!\d)\.(?!\d)|\n",text)]
    left=max([b for b in bounds if b<pos],default=-1)
    right=min([b for b in bounds if b>=pos],default=len(text))
    return text[left+1:(right+1 if right<len(text) else right)].strip()

def _target(s):
    x=s.lower()
    if "nearby allied heroes" in x or "nearby allies" in x:return "ALLY_AREA"
    if "all allied heroes" in x:return "ALLY_HERO_GLOBAL"
    if "target allied hero" in x or "target ally" in x:return "ALLY_TARGET"
    if "allies with" in x:return "STATUS_BOUND_ALLIES"
    if re.search(r"\b(self|himself|herself)\b",x):return "SELF"
    return "SELF_OR_CONTEXT"

def _duration(s):
    m=re.search(r"\b(?:over|for)\s+(\d+(?:\.\d+)?)\s+seconds?\b",s,re.I)
    return float(m.group(1)) if m else None

def _tick(s):
    m=re.search(r"\bevery\s+(\d+(?:\.\d+)?)\s+seconds?\b",s,re.I)
    if m:return float(m.group(1))
    return 1.0 if re.search(r"\bper\s+second\b",s,re.I) else None

def _conditions(s):
    out=[]; low=s.lower()
    for kw in ("if ","while ","when ","after ","against "):
        if kw in low:
            out.append({"type":"TEXT_CONDITION","text":s[low.index(kw)+len(kw):].strip().rstrip(".")})
            break
    return out

_FLAT_SUFFIX=rf"{NUM}(?:\s+(?:health|hp)\b|(?!\s*(?:%|seconds?|sec\b|energy\b|mana\b|rage\b|fury\b|charges?\b)))"
PATTERNS=[
 # Flat healing must be an explicit amount, not merely any number appearing
 # after a word such as "Healing". This prevents charge counts, cast times,
 # damage values, and resource regeneration from becoming fake heal amounts.
 ("FLAT", re.compile(rf"\b(?:heal|heals|healed|restore|restores|restored|regain|regains|regained)\b[^.;]{{0,80}}?\b(?:for|by)\s+{_FLAT_SUFFIX}",re.I)),
 ("FLAT", re.compile(rf"\b(?:heal|heals|healed|restore|restores|restored|regain|regains|regained)\b[^.;]{{0,80}}?\b{NUM}\s+(?:health|hp)\b",re.I)),
 ("FLAT", re.compile(rf"\bhealing\b[^.;]{{0,70}}?\b(?:for|by)\s+{_FLAT_SUFFIX}",re.I)),
 ("MAX_HEALTH_PERCENT", re.compile(rf"\b(?:heal|heals|restore|restores|regain|regains)\b[^.;]{{0,70}}?\b{NUM}%\s+of\s+[^.;]{{0,35}}?(?:maximum|max)\s+(?:health|life)\b",re.I)),
 ("CURRENT_HEALTH_PERCENT", re.compile(rf"\b(?:heal|heals|restore|restores|regain|regains)\b[^.;]{{0,70}}?\b{NUM}%\s+of\s+[^.;]{{0,35}}?current\s+(?:health|life)\b",re.I)),
 ("MISSING_HEALTH_PERCENT", re.compile(rf"\b(?:heal|heals|restore|restores|regain|regains)\b[^.;]{{0,70}}?\b{NUM}%\s+of\s+[^.;]{{0,35}}?missing\s+(?:health|life)\b",re.I)),
 ("DAMAGE_DEALT_PERCENT", re.compile(rf"\b(?:heal|heals|healed|healing|restore|restores)\b[^.;]{{0,100}}?\b(?:for\s+)?{NUM}%\s+of\s+(?:(?:the|all)\s+)?(?:(?:spell|ability|basic attack)\s+)?damage\s+(?:dealt|done|(?:[A-Za-z][A-Za-z' -]{{0,35}}\s+)?deals?)\b|\b(?:heal|heals|healed|healing)\b[^.;]{{0,90}}?\b(?:for\s+)?{NUM}%\s+of\s+[A-Za-z][A-Za-z' -]{{0,45}}'s\s+damage\b",re.I)),
 ("DAMAGE_ABSORBED_PERCENT", re.compile(rf"\b(?:heal|heals|healed|healing|restore|restores)\b[^.;]{{0,100}}?\b(?:for\s+)?{NUM}%\s+of\s+(?:the\s+)?damage\s+(?:it\s+)?(?:absorbed|prevented)\b",re.I)),
 ("DAMAGE_TAKEN_PERCENT", re.compile(rf"\b(?:heal|heals|healing|restore|restores)\b[^.;]{{0,80}}?\b(?:for\s+)?{NUM}%\s+of\s+(?:the\s+)?damage\s+taken\b",re.I)),
 ("BASIC_ATTACK_DAMAGE_PERCENT", re.compile(rf"\bbasic attacks?\b[^.;]{{0,100}}?\b(?:heal|heals)\b[^.;]{{0,50}}?\b{NUM}%\s+of\s+(?:the\s+)?damage\s+dealt\b",re.I)),
]
RESOURCE_SPENT=re.compile(r"\b(?:heal|heals|healed|healing|restore|restores)\b[^.;]{0,110}?\bfor\s+the\s+amount\s+of\s+(energy|mana|rage|fury)\s+(?:consumed|spent)\b",re.I)
CONVERT=re.compile(rf"\b(?:damage\s+taken|incoming\s+damage)\b[^.;]{{0,90}}?\b(?:heal|healing|healed)\b[^.;]{{0,40}}?\b{NUM}%",re.I)
SNAPSHOT=re.compile(rf"\blose\s+{NUM}%\s+of\s+[^.;]{{0,35}}?current\s+health\b[^.;]{{0,160}}?\b(?:heal|heals|regain|regains)\b[^.;]{{0,80}}?(?:same|that|amount)",re.I)

def infer_heal_models(text, source="TEXT"):
    text=_clean(text); low=text.lower()
    if not any(w in low for w in ("heal","healing","restore health","regain health","regenerate")): return []
    out=[]; seen=set()
    for model,rx in PATTERNS:
        for m in rx.finditer(text):
            val=next((float(g) for g in m.groups() if g is not None),None)
            if val is None: continue
            sent=_sentence(text,m.start())
            tick=_tick(sent)
            actual="PERCENT_MAX_HEALTH_PER_TICK" if model=="MAX_HEALTH_PERCENT" and tick is not None else model
            row={"amount_model":actual,"value":val,"unit":"flat" if actual=="FLAT" else "percent",
                 "duration_seconds":(_duration(sent) if (tick is not None or re.search(r"\bover\s+\d",sent,re.I)) else None),"tick_interval_seconds":tick,
                 "target_model":_target(sent),"conditions":_conditions(sent),
                 "source":source,"evidence":sent}
            row["confidence"]="MEDIUM"
            if actual in {"DAMAGE_DEALT_PERCENT","BASIC_ATTACK_DAMAGE_PERCENT"}: row["source_event"]="DAMAGE_DEALT"
            if actual=="DAMAGE_TAKEN_PERCENT": row["source_event"]="DAMAGE_TAKEN"
            if actual=="DAMAGE_ABSORBED_PERCENT": row["source_event"]="DAMAGE_PREVENTED_OR_ABSORBED"
            if actual=="BASIC_ATTACK_DAMAGE_PERCENT": row["source_damage_type"]="BASIC_ATTACK"
            elif actual=="DAMAGE_DEALT_PERCENT":
                _sl=sent.lower()
                if "spell damage" in _sl: row["source_damage_type"]="SPELL"
                elif "basic attack" in _sl: row["source_damage_type"]="BASIC_ATTACK"
                elif "ability damage" in _sl: row["source_damage_type"]="ABILITY"
            k=(actual,val,sent)
            if k not in seen: seen.add(k); out.append(row)
    for m in RESOURCE_SPENT.finditer(text):
        sent=_sentence(text,m.start())
        out.append({"amount_model":"RESOURCE_SPENT_1_TO_1","value":1.0,"unit":"health_per_resource",
                    "resource":m.group(1).upper(),"source_event":"RESOURCE_SPENT","duration_seconds":_duration(sent),
                    "target_model":_target(sent),"conditions":_conditions(sent),"source":source,"confidence":"MEDIUM","evidence":sent})
    for m in CONVERT.finditer(text):
        sent=_sentence(text,m.start())
        out.append({"amount_model":"DAMAGE_TAKEN_CONVERSION_PERCENT","value":float(m.group(1)),"unit":"percent",
                    "source_event":"DAMAGE_TAKEN","conversion":True,"duration_seconds":_duration(sent),
                    "target_model":"SELF","conditions":_conditions(sent),"source":source,"confidence":"MEDIUM","evidence":sent})
    for m in SNAPSHOT.finditer(text):
        sent=_sentence(text,m.start())
        out.append({"amount_model":"SELF_DAMAGE_SNAPSHOT_RETURN","value":float(m.group(1)),
                    "unit":"percent_current_health_lost","source_event":"SELF_HEALTH_LOSS_SNAPSHOT","snapshot":True,
                    "duration_seconds":_duration(sent),"target_model":"SELF","conditions":_conditions(sent),
                    "source":source,"confidence":"MEDIUM","evidence":sent})
    uniq=[];seen=set()
    for r in out:
        k=(r.get("amount_model"),r.get("value"),r.get("evidence"))
        if k not in seen: seen.add(k); uniq.append(r)
    return uniq
