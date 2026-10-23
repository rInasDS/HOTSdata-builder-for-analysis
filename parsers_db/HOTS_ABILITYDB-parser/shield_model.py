from __future__ import annotations
import re

NUM=r"(\d+(?:\.\d+)?)"

def _clean(text):
    s=str(text or "")
    s=re.sub(r"<n\s*/?>",". ",s,flags=re.I)
    s=re.sub(r"<[^>]+>","",s)
    s=re.sub(r"~~\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)\s*~~","",s)
    return re.sub(r"\s+"," ",s).strip()


_PATTERNS=[
    ("MAX_HEALTH_PERCENT", re.compile(rf"(?:shield\s+(?:equal\s+to\s+|for\s+)?|(?:gain|gains|grant|grants|give|gives|receive|receives|provides?)\s+(?:[^.;]{{0,45}}?\s+)?(?:a\s+)?shield\s+(?:equal\s+to\s+|for\s+)?|gain\s+){NUM}%\s+(?:of\s+)?(?:his\s+|her\s+|their\s+|your\s+|the\s+|[A-Za-z][A-Za-z' -]{{0,30}}'s\s+)?(?:maximum|max)\s+(?:health|life)|{NUM}%\s+(?:maximum|max)\s+(?:health|life)\s+as\s+a\s+shield", re.I)),
    ("MISSING_HEALTH_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+|for\s+)?{NUM}%\s+of\s+[^.]{{0,75}}?(?:missing\s+health|health\s+(?:they|he|she|you|the\s+target)\s+(?:are|is)?\s*missing)", re.I)),
    ("CURRENT_HEALTH_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+)?{NUM}%\s+of\s+[^.]{{0,55}}?current\s+health", re.I)),
    ("MAX_MANA_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+)?{NUM}%\s+of\s+[^.]{{0,55}}?(?:maximum|max)\s+mana", re.I)),
    ("DAMAGE_DEALT_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+)?{NUM}%\s+of\s+(?:the\s+)?damage\s+(?:it\s+|they\s+|he\s+|she\s+)?dealt", re.I)),
    ("DAMAGE_BLOCKED_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+)?{NUM}%\s+of\s+(?:the\s+)?damage\s+blocked", re.I)),
    ("SPELL_DAMAGE_TAKEN_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+)?{NUM}%\s+of\s+(?:the\s+)?spell\s+damage\s+taken", re.I)),
    ("DAMAGE_TAKEN_PERCENT", re.compile(rf"shield\s+(?:equal\s+to\s+)?{NUM}%\s+of\s+(?:the\s+)?damage\s+taken", re.I)),
    ("FLAT", re.compile(rf"(?:gain|gains|grant|grants|receive|receives)\s+(?:a\s+)?{NUM}\s+(?:point\s+)?shield\b|shield\s+(?:of\s+|equal\s+to\s+)?{NUM}\b(?!%)|\bhas\s+(?:a\s+)?(?:permanent\s+)?{NUM}\s+shield\b", re.I)),
]

# Explicit special case: "Shield equal to her maximum Health" => 100% max health.
_FULL_MAX_HEALTH=re.compile(r"shield\s+equal\s+to\s+(?:his|her|their|your|the)\s+maximum\s+health",re.I)
_DURATION=re.compile(r"\bfor\s+(\d+(?:\.\d+)?)\s+seconds?",re.I)
_MAX_PERCENT=re.compile(r"\bup\s+to\s+(\d+(?:\.\d+)?)%",re.I)
_MAX_STACKS=re.compile(r"\bstacks?\s+up\s+to\s+(\d+)\s+times?",re.I)
_RESOURCE_SHIELD=re.compile(r"(?:shield[^.;]{0,45}?(?:absorbs?|equal\s+to)|gain\s+(?:a\s+)?shield[^.;]{0,35}?)(?:\s+)?(\d+(?:\.\d+)?)%\s+(?:of\s+)?[^.;]{0,25}?(?:maximum|max)\s+(?:health|life)[^.]{0,90}?(?:per|for each)\s+(?:point\s+of\s+)?(energy|mana|rage|fury)(?:\s+consumed)?",re.I)
_DECAY_ONLY=re.compile(r"shield[^.]{0,90}?decays?\s+over\s+(\d+(?:\.\d+)?)\s+seconds?",re.I)
_DECAY=re.compile(r"shield[^.]{0,120}?(?:decay|decays|loses?|lose|decreases?|decrease)[^.]{0,80}?(?:(\d+(?:\.\d+)?)%|over\s+(\d+(?:\.\d+)?)\s+seconds?)",re.I)

_DAMAGE_AS_SHIELD=re.compile(rf"\bgain\s+{NUM}%\s+of\s+damage\s+dealt\s+from\s+abilities\s+and\s+{NUM}%\s+from\s+basic\s+attacks\s+as\s+shields?",re.I)
_SHIELD_RESTORE=re.compile(rf"\b(?:shield(?:\s+capacitor)?\s+)?(?:regain|regains|restore|restores|recharge|recharges)\s+{NUM}\s+shields?\b",re.I)
_SHIELD_REGEN_PERCENT=re.compile(rf"\bshield\b[^.;]{{0,80}}?\bregenerates?\s+at\s+{NUM}%\s+per\s+second",re.I)
_DEATH_REDUCE=re.compile(r"\bevery\s+death[^.]{0,60}?reduce(?:s|d)?\s+(?:this\s+)?shield\s+by\s+(\d+(?:\.\d+)?)%",re.I)


def _value_from_match(m):
    # Patterns may have two alternative capture groups.
    for g in m.groups():
        if g is not None:
            try:return float(g)
            except:pass
    return None


def _sentence(text,pos):
    bounds=[m.start() for m in re.finditer(r"(?<!\d)\.(?!\d)|\n",text)]
    left=max([b for b in bounds if b<pos],default=-1)
    right=min([b for b in bounds if b>=pos],default=len(text))
    return text[left+1:(right+1 if right<len(text) else right)].strip()


def _target_model(sentence):
    s=sentence.lower()
    if "nearby allied heroes" in s or "nearby allies" in s:
        return "ALLY_HERO_AREA"
    if "allied hero" in s or "target ally" in s or "target allied" in s:
        return "ALLY_TARGET"
    if re.search(r"\b(gain|gains)\b",s) and not "allied" in s:
        return "SELF"
    return "UNKNOWN"


def _trigger_model(sentence):
    s=sentence.lower()
    if "activate" in s:return "ON_CAST"
    if "upon taking fatal damage" in s:return "ON_FATAL_DAMAGE"
    if s.startswith("when ") or " when " in s:return "ON_CONDITION"
    if s.startswith("after ") or " after " in s:return "ON_CONDITION"
    if "casting " in s:return "ON_CAST"
    if "hit by" in s or "hits " in s:return "ON_HIT"
    return "ON_RESOLVE"


def _conditions(sentence):
    s=sentence.lower();out=[]
    if "if " in s:
        cond=s.split("if ",1)[1].strip().rstrip('.')
        out.append({"type":"TEXT_CONDITION","text":cond})
    if "while " in s:
        cond=s.split("while ",1)[1].strip().rstrip('.')
        out.append({"type":"TEXT_CONDITION","text":cond})
    if "upon taking fatal damage" in s:
        out.append({"type":"FATAL_DAMAGE"})
    return out


def infer_shield_models(text, source="TEXT"):
    text=_clean(text)
    if "shield" not in text.lower():return []
    out=[];seen=set()
    if _FULL_MAX_HEALTH.search(text):
        m=_FULL_MAX_HEALTH.search(text); sent=_sentence(text,m.start())
        out.append({"effect_class":"ABSORB_SHIELD","amount_model":"MAX_HEALTH_PERCENT","value":100.0,"unit":"percent",
                    "duration_seconds":_near_duration(sent),"target_model":_target_model(sent),
                    "trigger":_trigger_model(sent),"conditions":_conditions(sent),
                    "source":source,"confidence":"MEDIUM","evidence":sent})
    for model,rx in _PATTERNS:
        for m in rx.finditer(text):
            val=_value_from_match(m)
            if val is None:continue
            sent=_sentence(text,m.start())
            if _RESOURCE_SHIELD.search(sent):
                continue
            key=(model,val,sent)
            if key in seen:continue
            seen.add(key)
            row={"effect_class":"ABSORB_SHIELD","amount_model":model,"value":val,
                 "unit":"flat" if model=="FLAT" else "percent",
                 "duration_seconds":None if "permanent" in sent.lower() else _near_duration(sent),
                 "target_model":_target_model(sent),"trigger":_trigger_model(sent),
                 "conditions":_conditions(sent),"source":source,"confidence":"MEDIUM","evidence":sent}
            mm=_MAX_PERCENT.search(sent)
            if mm:row["max_total_percent"]=float(mm.group(1))
            ms=_MAX_STACKS.search(sent)
            if ms:row["max_stacks"]=int(ms.group(1))
            dr=_DEATH_REDUCE.search(text)
            if dr:row["decay_per_death_percent"]=float(dr.group(1))
            out.append(row)
    for m in _RESOURCE_SHIELD.finditer(text):
        sent=_sentence(text,m.start())
        out.append({"effect_class":"ABSORB_SHIELD","amount_model":"MAX_HEALTH_PERCENT_PER_RESOURCE",
                    "value":float(m.group(1)),"unit":"percent_per_resource","resource":m.group(2).upper(),
                    "consume_resource":bool(re.search(r"consume|consumes|spend|spends",sent,re.I)),
                    "duration_seconds":_near_duration(sent),"target_model":_target_model(sent),
                    "trigger":_trigger_model(sent),"conditions":_conditions(sent),"source":source,"confidence":"MEDIUM","evidence":sent})
    for m in _DAMAGE_AS_SHIELD.finditer(text):
        sent=_sentence(text,m.start())
        vals=[float(m.group(1)),float(m.group(2))]
        for src_type,val in (("ABILITY",vals[0]),("BASIC_ATTACK",vals[1])):
            out.append({"effect_class":"ABSORB_SHIELD","amount_model":"DAMAGE_DEALT_PERCENT","value":val,"unit":"percent",
                        "source_event":"DAMAGE_DEALT","source_damage_type":src_type,"duration_seconds":_near_duration(sent),
                        "target_model":"SELF","trigger":"ON_DAMAGE_DEALT","conditions":_conditions(sent),"source":source,"confidence":"MEDIUM","evidence":sent})
    for m in _SHIELD_RESTORE.finditer(text):
        sent=_sentence(text,m.start())
        out.append({"effect_class":"SHIELD_RESTORE","amount_model":"FLAT_RESTORE","value":float(m.group(1)),"unit":"flat",
                    "duration_seconds":None,"target_model":"SELF","trigger":_trigger_model(sent),"conditions":_conditions(sent),
                    "source":source,"confidence":"MEDIUM","evidence":sent})
    for m in _SHIELD_REGEN_PERCENT.finditer(text):
        sent=_sentence(text,m.start())
        _delay=re.search(r"after\s+not\s+taking\s+damage\s+for\s+(\d+(?:\.\d+)?)\s+seconds?",sent,re.I)
        out.append({"effect_class":"SHIELD_REGEN","amount_model":"CURRENT_SHIELD_PERCENT_PER_SECOND","value":float(m.group(1)),"unit":"percent_per_second",
                    "duration_seconds":None,"activation_delay_seconds":float(_delay.group(1)) if _delay else None,
                    "target_model":"SELF","trigger":"PASSIVE_REGEN","conditions":_conditions(sent),
                    "source":source,"confidence":"MEDIUM","evidence":sent})
    dm=_DECAY.search(text)
    if dm:
        for r in out:
            r["decay_model"]="PERCENT_OR_DURATION"
            r["decay_percent"]=float(dm.group(1)) if dm.group(1) else None
            r["decay_duration_seconds"]=float(dm.group(2)) if dm.group(2) else None
    if not out:
        dm2=_DECAY_ONLY.search(text)
        if dm2:
            sent=_sentence(text,dm2.start())
            out.append({"effect_class":"ABSORB_SHIELD","amount_model":"INHERIT_BASE_SHIELD",
                        "value":None,"unit":"inherited","decay_model":"DURATION_OVERRIDE",
                        "decay_duration_seconds":float(dm2.group(1)),
                        "target_model":_target_model(sent),"trigger":_trigger_model(sent),
                        "conditions":_conditions(sent),"source":source,"confidence":"MEDIUM","evidence":sent})
    # stable dedupe
    uniq=[];seen2=set()
    for r in out:
        k=(r.get("amount_model"),r.get("value"),r.get("evidence"))
        if k not in seen2:seen2.add(k);uniq.append(r)
    return uniq


def _near_duration(sentence):
    m=_DURATION.search(sentence)
    return float(m.group(1)) if m else None
