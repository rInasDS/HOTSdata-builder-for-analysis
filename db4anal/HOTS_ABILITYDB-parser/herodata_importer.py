from __future__ import annotations
import json
import re
from json_backend import load as _orjson_load, save as _orjson_save
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

CATEGORY_MAP = {"Basic":"BASIC","Heroic":"HEROIC","Trait":"TRAIT","Mount":"MOUNT","Hearth":"HEARTH","Activable":"ACTIVE","Active":"ACTIVE"}
SLOT_MAP = {"HEROIC":"R","TRAIT":"D","ACTIVE":"ACTIVE","MOUNT":"Z","HEARTH":"B"}

def load_json(path: Path)->dict:
    data=_orjson_load(path)
    if not isinstance(data,dict): raise ValueError("HeroData root must be object")
    return data

def save_json(path:Path,data:dict)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    _orjson_save(path,data)

def text(v):
    if v is None:return None
    s=str(v).strip()
    return s or None

def parse_num_label(v,label):
    if not v:return None
    m=re.search(rf"{re.escape(label)}\s*:\s*([0-9]+(?:\.[0-9]+)?)",str(v),re.I)
    return float(m.group(1)) if m else None

def cooldown(e):
    for label in ("Cooldown","Charge Cooldown"):
        x=parse_num_label(e.get("cooldownText"),label)
        if x is not None:return x
    return None

def mana(e):
    for f in ("energy","mana","manaCost","energyCost","resourceCost"):
        v=e.get(f)
        if isinstance(v,(int,float)): return float(v)
        if isinstance(v,dict):
            for k in ("amount","value","cost"):
                if isinstance(v.get(k),(int,float)): return float(v[k])
    for f in ("energyText","manaText","fullText"):
        x=parse_num_label(e.get(f),"Mana")
        if x is not None:return x
    return None



def normalize_resource_block(v, fallback_type=None):
    if not isinstance(v,dict):
        return None
    amount=v.get("amount")
    regen=v.get("regenRate")
    scale=v.get("scale")
    regen_scale=v.get("regenScale")
    typ=text(v.get("type")) or fallback_type
    if all(x is None for x in (amount,regen,scale,regen_scale,typ)):
        return None
    return {
        "amount":amount,
        "scale":scale,
        "regenRate":regen,
        "regenScale":regen_scale,
        "type":typ,
    }

def hero_resources(h):
    """Canonical hero vitals while preserving raw HeroData compatibility."""
    out={}
    life=normalize_resource_block(h.get("life"),"Health")
    if life:
        out["health"]=life

    # HeroesProfile/HeroData commonly exposes the primary resource under `energy`
    # even when its semantic type is Mana. Accept aliases for forward compatibility.
    candidates=[]
    for key in ("energy","mana","resource","primaryResource"):
        v=h.get(key)
        if isinstance(v,dict):
            candidates.append((key,v))
    resources=h.get("resources")
    if isinstance(resources,dict):
        for key,v in resources.items():
            if isinstance(v,dict): candidates.append((str(key),v))

    for key,v in candidates:
        rec=normalize_resource_block(v, key.title())
        if not rec: continue
        typ=str(rec.get("type") or key).strip().lower()
        if typ in {"mana","energy","fury","rage","brew","essence"}:
            canon=typ
        else:
            canon=key.lower()
        if canon in {"life","health"}: canon="health"
        out.setdefault(canon,rec)

    return out

def charges(e):
    c=e.get("charges")
    if not isinstance(c,dict):return None,None
    try: count=int(c.get("countMax")) if c.get("countMax") is not None else None
    except: count=None
    return count,{
        "count_max":c.get("countMax"),"count_start":c.get("countStart"),"count_use":c.get("countUse"),
        "recast_cooldown":c.get("recastCooldown"),"is_count_hidden":c.get("isCountHidden")
    }

def slot(category,ability_type):
    at=str(ability_type or "").upper()
    if at in {"Q","W","E","R","D","Z","B"}: return at
    if at in SLOT_MAP:return SLOT_MAP[at]
    cat=CATEGORY_MAP.get(category,category.upper())
    return SLOT_MAP.get(cat,cat)

def canon_ability(hero,category,e,source,parent=None):
    c,cd=charges(e)
    aid=e.get("abilityId") or e.get("buttonId") or e.get("talentId") or e.get("linkId")
    canonical_id=text(aid)
    rec_key="|".join([
        hero,
        canonical_id or "<NO_ID>",
        slot(category,e.get("abilityType")),
        source,
        parent or "",
        text(e.get("buttonId")) or "",
        text(e.get("linkId")) or "",
    ])
    return {
        "record_key":rec_key,
        "hero":hero,"id":canonical_id,"ability_id":text(e.get("abilityId")),"button_id":text(e.get("buttonId")),
        "link_id":text(e.get("linkId")),"name":text(e.get("name")),"slot":slot(category,e.get("abilityType")),
        "category":CATEGORY_MAP.get(category,category.upper()),"ability_type":text(e.get("abilityType")),
        "source":source,"parent_link_id":parent,"cooldown":cooldown(e),"cooldown_text":text(e.get("cooldownText")),
        "mana_cost":mana(e),"charges":c,"charge_data":cd,"short_text":text(e.get("shortText")),
        "full_text":text(e.get("fullText")),"icon":text(e.get("icon"))
    }

def canon_talent(hero,level,e):
    return {
        "hero":hero,"level":level,"talent_id":text(e.get("talentId")),"ability_id":text(e.get("abilityId")),
        "button_id":text(e.get("buttonId")),"link_id":text(e.get("linkId")),"name":text(e.get("name")),
        "ability_type":text(e.get("abilityType")),"is_quest":e.get("isQuest"),
        "upgrades_ability_type":e.get("upgradesAbilityType"),"has_hotkey":e.get("hasHotkey"),
        "sort":e.get("sort"),"short_text":text(e.get("shortText")),"full_text":text(e.get("fullText")),
        "tooltip_ability_link_ids":e.get("tooltipAbilityLinkIds") or []
    }

def import_herodata(data):
    meta=data.get("meta",{}); items=data.get("items",{})
    if not isinstance(items,dict): raise ValueError("HeroData has no items object")
    heroes={}; missing_id=[]; missing_slot=[]; dup=[]; dup_record=[]; total_a=total_s=total_t=0
    slot_counts=Counter()

    for hero_key,h in items.items():
        if not isinstance(h,dict):continue
        name=str(h.get("name") or hero_key)
        rec={"hero_key":hero_key,"name":name,"unit_id":text(h.get("unitId")),"hyperlink_id":text(h.get("hyperlinkId")),
             "attribute_id":text(h.get("attributeId")),"title":text(h.get("title")),"roles":h.get("roles") or [],
             "expanded_role":text(h.get("expandedRole")),"radius":h.get("radius"),"inner_radius":h.get("innerRadius"),
             "sight":h.get("sight"),"speed":h.get("speed"),"life":h.get("life"),
             "energy":h.get("energy"),"resources":hero_resources(h),"weapons":h.get("weapons") or [],
             "summoned_unit_ids":h.get("summonedUnitIds") or [],"abilities":[],"talents":[]}

        groups=h.get("abilities",{})
        if isinstance(groups,dict):
            for cat,entries in groups.items():
                if isinstance(entries,list):
                    for e in entries:
                        if isinstance(e,dict):
                            rec["abilities"].append(canon_ability(name,cat,e,"HERODATA_ABILITIES")); total_a+=1

        sub=h.get("subAbilities",{})
        if isinstance(sub,dict):
            for parent,groups in sub.items():
                if not isinstance(groups,dict):continue
                for cat,entries in groups.items():
                    if isinstance(entries,list):
                        for e in entries:
                            if isinstance(e,dict):
                                rec["abilities"].append(canon_ability(name,cat,e,"HERODATA_SUBABILITY",str(parent))); total_a+=1; total_s+=1

        talents=h.get("talents",{})
        if isinstance(talents,dict):
            for level,entries in talents.items():
                if isinstance(entries,list):
                    for e in entries:
                        if isinstance(e,dict):
                            rec["talents"].append(canon_talent(name,level,e)); total_t+=1

        seen=defaultdict(int)
        seen_record=defaultdict(int)
        for a in rec["abilities"]:
            if not a["id"]: missing_id.append({"hero":name,"name":a["name"],"source":a["source"]})
            else: seen[a["id"]]+=1
            seen_record[a["record_key"]]+=1
            if not a["slot"]: missing_slot.append({"hero":name,"id":a["id"]})
            else: slot_counts[a["slot"]]+=1
        for aid,c in seen.items():
            if c>1: dup.append({"hero":name,"ability_id":aid,"count":c})
        for rk,c in seen_record.items():
            if c>1: dup_record.append({"hero":name,"record_key":rk,"count":c})
        heroes[name]=rec

    canonical={"metadata":{"builder":"herodata_importer_v2","heroes_version":meta.get("heroesVersion"),"hdp_version":meta.get("hdpVersion")},"heroes":heroes}
    report={"metadata":canonical["metadata"],"summary":{"heroes":len(heroes),"abilities_total":total_a,"subabilities_total":total_s,
            "talents_total":total_t,"missing_ability_id":len(missing_id),"missing_slot":len(missing_slot),
            "duplicate_ability_ids_within_hero":len(dup),"duplicate_record_keys":len(dup_record)},"ability_slots":dict(slot_counts),
            "missing_ability_id":missing_id,"missing_slot":missing_slot,"duplicate_ability_ids_within_hero":dup,
            "duplicate_record_keys":dup_record}
    return canonical,report

def import_herodata_file(input_path,canonical_output,report_output):
    c,r=import_herodata(load_json(input_path)); save_json(canonical_output,c); save_json(report_output,r); return r
