from __future__ import annotations
from pathlib import Path
from json_backend import load, save

FIXTURE_HEROES_VERSION = "2.55.17.97771"


def _rows(data, hero):
    return list((data.get("heroes") or {}).get(hero, []) or [])


def _find(data, hero, token):
    t = token.lower()
    return [r for r in _rows(data, hero) if t in str(r.get("record_key") or "").lower()]


def _effect_ids(row, *keys):
    out=[]
    for key in keys:
        for x in row.get(key) or []:
            eid=x.get("effect_id") or x.get("object_id") or x.get("behavior_id")
            if eid: out.append(str(eid))
    return out


def run_combat_semantic_gate(combat_path: Path, herodata_path: Path, output_path: Path) -> dict:
    combat=load(combat_path); hd=load(herodata_path)
    version=(hd.get("metadata") or {}).get("heroes_version")
    same=version==FIXTURE_HEROES_VERSION
    checks=[]

    def check(name, ok, details=None, *, blocking=True):
        row={"name":name,"passed":bool(ok),"blocking":bool(blocking),"details":details or {}}
        checks.append(row)

    # Generic contamination invariant: hearth/mount implementation chains must not
    # manufacture direct combat mechanics for the activating ability.
    hearth_bad=[]
    for hero, rows in (combat.get("heroes") or {}).items():
        for r in rows or []:
            parts=str(r.get("record_key") or "").split("|")
            slot=parts[2] if len(parts)>2 else None
            if slot=="B" and any(r.get(k) for k in ("damage","heal","shield_candidates","conditional_damage","conditional_heal","conditional_shield_candidates")):
                hearth_bad.append({"hero":hero,"record_key":r.get("record_key")})
    check("hearth_has_no_direct_combat", not hearth_bad, {"violations":hearth_bad[:20],"count":len(hearth_bad)})

    # Current-build semantic fixtures. They are regression evidence, never production
    # hero-specific patches. On another game-data version they are informative only.
    block=same

    fenix=_find(combat,"Fenix","FenixRepeaterCannon")
    fenix_bad=[eid for r in fenix for eid in _effect_ids(r,"damage","conditional_damage") if eid=="FenixHeroWeaponDamage"]
    check("fenix_repeater_does_not_own_weapon_damage", bool(fenix) and not fenix_bad,{"rows":len(fenix),"bad":fenix_bad},blocking=block)

    xul=_find(combat,"Xul","NecromancerCursedStrikes")
    xul_ids=[eid for r in xul for eid in _effect_ids(r,"damage","conditional_damage")]
    check("xul_cursed_strikes_owns_weapon_damage","NecromancerCursedStrikesDamage" in xul_ids,{"effects":xul_ids},blocking=block)

    zag=_find(combat,"Zagara","ZagaraInfestedDrop")
    zag_ids=[eid for r in zag for eid in _effect_ids(r,"damage","conditional_damage")]
    check("zagara_drop_direct_damage_present","ZagaraInfestedDropDamage" in zag_ids,{"effects":zag_ids},blocking=block)
    check("zagara_drop_does_not_own_roach_attack","ZagaraRoachAttackDamage" not in zag_ids,{"effects":zag_ids},blocking=block)

    bw=_find(combat,"Brightwing","FaerieDragonPolymorph")
    bw_hits=[]
    for r in bw:
        for x in r.get("conditional_damage") or []:
            if x.get("effect_id")=="BrightwingPolymorphUnstableAnomalyDamage":
                bw_hits.append(x)
    bw_ok=any((x.get("formula") or {}).get("model")=="PERCENT_MAX_HEALTH" and (x.get("formula") or {}).get("value_percent") is not None for x in bw_hits)
    check("brightwing_unstable_anomaly_is_conditional_percent_hp",bw_ok,{"matches":bw_hits[:3]},blocking=block)

    var=_find(combat,"Varian","VarianDemoralizingShout")
    var_damage=[eid for r in var for eid in _effect_ids(r,"damage","conditional_damage")]
    var_mods=[m for r in var for key in ("modifiers","conditional_modifiers") for m in (r.get(key) or [])]
    var_ok=any(m.get("modifier_kind")=="DAMAGE_DEALT_FRACTION" and float(m.get("value"))<0 for m in var_mods if isinstance(m.get("value"),(int,float)))
    check("varian_demoralizing_is_modifier_not_damage", bool(var) and not var_damage and var_ok,{"damage":var_damage,"modifiers":var_mods[:6]},blocking=block)

    lions=_find(combat,"Varian","VarianLionsFang")
    lions_heal=[eid for r in lions for eid in _effect_ids(r,"heal","conditional_heal")]
    check("varian_lions_fang_heal_path_present",
          "VarianLionsFangCasterHealHero" in lions_heal and "VarianLionsFangCasterHealNonHero" in lions_heal,
          {"heal_effects":lions_heal},blocking=block)

    zarya=_find(combat,"Zarya","ZaryaPersonalBarrier")
    base=[x for r in zarya for x in (r.get("shield_candidates") or [])]
    cond=[x for r in zarya for x in (r.get("conditional_shield_candidates") or [])]
    check("zarya_barrier_base_and_talent_branches_separate", bool(base) and bool(cond),
          {"base_limits":[x.get("limit") for x in base],"conditional_limits":[x.get("limit") for x in cond]},blocking=block)

    rag=_find(combat,"Ragnaros","RagnarosSubmerge")
    rag_heal=[eid for r in rag for eid in _effect_ids(r,"heal","conditional_heal")]
    check("ragnaros_submerge_heal_path_present","RagnarosSubmergeHeal" in rag_heal,{"heal_effects":rag_heal},blocking=block)

    failures=[c for c in checks if c["blocking"] and not c["passed"]]
    report={"summary":{"heroes_version":version,"fixture_version":FIXTURE_HEROES_VERSION,
                       "same_build_fixtures":same,"checks_total":len(checks),
                       "passed":sum(1 for c in checks if c["passed"]),"blocking_failures":len(failures)},
            "checks":checks,"failures":failures}
    save(output_path,report)
    if failures:
        raise RuntimeError(f"Combat semantic gate failed: {len(failures)} blocking fixture(s)")
    return report
