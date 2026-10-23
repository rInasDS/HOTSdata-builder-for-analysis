from __future__ import annotations

# Primary battleground packages. These are load-context identities, not semantic
# guesses about mechanics. A definition from another battleground package is
# fail-closed unless that package is listed as a proven dependency below.
MAP_CONTEXT_MODS = {
    "AlteracPass": {"alteracpass.stormmod"},
    "BattlefieldOfEternity": {"battlefieldofeternity.stormmod"},
    "BlackheartsBay": {"blackheartsbay.stormmod"},
    "BraxisHoldout": {"braxisholdout.stormmod", "braxisholdoutdata.stormmod"},
    "BraxisOutpost": {"braxisoutpost.stormmod"},
    "ControlPoints": {"skytemple.stormmod", "skytempledata.stormmod"},
    "Crypts": {"tombofthespiderqueen.stormmod"},
    "CursedHollow": {"cursedhollow.stormmod"},
    "DragonShire": {"dragonshire.stormmod"},
    "HauntedMines": {"hauntedmines.stormmod"},
    "HauntedWoods": {"gardenofterror.stormmod", "gardenofterrordata.stormmod"},
    "IndustrialDistrict": {"industrialdistrict.stormmod"},
    "LostCavern": {"lostcavern.stormmod"},
    "Shrines": {"infernalshrines.stormmod", "infernalshrinesdata.stormmod"},
    "SilverCity": {"silvercity.stormmod"},
    "TowersOfDoom": {"towersofdoom.stormmod"},
    "Volskaya": {"volskayadata.stormmod", "volskayamechanics.stormmod", "volskayasound.stormmod"},
}

# Dependencies proven by replay-observed exact CUnit identities.  Keep this
# explicit: replay proves that the unit exists on this map, and catalog
# provenance proves where its CUnit root is defined.  We never infer arbitrary
# dependencies from broad reference-graph reachability.
#
# v0.1.2 exposed these shared battleground data packages after the stricter
# fail-closed filter removed historical cross-map contamination.  The evidence
# below comes from the 296-replay Map DB corpus used for validation.
MAP_CONTEXT_DEPENDENCY_EVIDENCE = {
    "AlteracPass": {
        "warcraftdata.stormmod": {"Peon_Dummy", "SpiritHealer"},
    },
    "BattlefieldOfEternity": {
        "heavenhell.stormmod": {"Hell_GiantDemon_Base", "Hell_GiantDemon_Chains", "WeaponRackSpecialHell"},
    },
    "BraxisHoldout": {
        "starcraft.stormmod": {"ArcadeMachine", "TerranGoliath", "TerranGoliathDefender", "TerranRaven", "TerranRavenDefender"},
    },
    "BraxisOutpost": {
        "braxisholdoutdata.stormmod": {"BraxisHoldoutSCV"},
        "starcraft.stormmod": {"TerranGoliath", "TerranGoliathDefender", "TerranRaven", "TerranRavenDefender"},
    },
    "IndustrialDistrict": {
        "volskayadata.stormmod": {"VolskayaMercCaptureSlab"},
        "overwatchdata.stormmod": {"OWPickupChest", "OverwatchMechanic", "OverwatchTurret", "TurretPickup"},
        "conveyorbelts.stormmod": {"ConveyorBelt3x3East", "ConveyorBelt3x3North", "ConveyorBelt3x3South", "ConveyorBelt3x3West"},
    },
    "Shrines": {
        "heavenhell.stormmod": {"Hell_GiantDemon_Base", "Hell_GiantDemon_Chains", "WeaponRackSpecialHeaven"},
    },
    "Volskaya": {
        "overwatchdata.stormmod": {"HealingPulsePickup", "MercDefenderMeleeIndividual", "OWPickupChest", "OverwatchMechanic", "OverwatchTurret", "TurretPickup"},
        "conveyorbelts.stormmod": {"ConveyorBelt3x3East", "ConveyorBelt3x3North", "ConveyorBelt3x3South", "ConveyorBelt3x3West"},
    },
}

MAP_CONTEXT_DEPENDENCY_MODS = {
    map_id: set(mods) for map_id, mods in MAP_CONTEXT_DEPENDENCY_EVIDENCE.items()
}

ARAM_MAPS = {"BraxisOutpost", "IndustrialDistrict", "LostCavern", "SilverCity"}
ARAM_SHARED_MODS = {"arenamode.stormmod", "arenashared.stormmod"}


def _norm(s: str | None) -> str:
    return (s or "").replace("/", "\\").lower()


def _primary_mods(map_id: str) -> set[str]:
    return {m.lower() for m in MAP_CONTEXT_MODS.get(map_id, set())}


def _dependency_mods(map_id: str) -> set[str]:
    return {m.lower() for m in MAP_CONTEXT_DEPENDENCY_MODS.get(map_id, set())}


def definition_scope(defn: dict) -> str:
    f = _norm(defn.get("file"))
    # Outer package scope wins over an embedded .StormMap path. This permits a
    # current battleground stormmod to carry map-package data while still keeping
    # heroes.stormmod single-player/test maps isolated below.
    if "\\heroesbrawlmods\\" in f:
        return "BRAWL_SCOPED"
    if "\\heroesmapmods\\battlegroundmapmods\\" in f:
        return "BATTLEGROUND_SCOPED"
    if ".stormmap\\" in f or f.endswith(".stormmap"):
        return "EMBEDDED_STORMMAP_SCOPED"
    return "COMMON"


def definition_allowed(defn: dict, map_id: str) -> tuple[bool, str]:
    scope = definition_scope(defn)
    mod = _norm(defn.get("mod"))
    primary = _primary_mods(map_id)
    dependencies = _dependency_mods(map_id)

    if scope == "BRAWL_SCOPED":
        if mod in primary:
            return True, "CURRENT_MAP_MOD"
        if mod in dependencies:
            return True, "PROVEN_MAP_DEPENDENCY_MOD"
        if map_id in ARAM_MAPS and mod in {m.lower() for m in ARAM_SHARED_MODS}:
            return True, "ARAM_SHARED_MOD"
        return False, "FOREIGN_BRAWL_MOD"

    if scope == "BATTLEGROUND_SCOPED":
        # v0.1.1 only rejected a hand-maintained list of known foreign mods. That
        # let unlisted packages such as heavenhell.stormmod / overwatchdata.stormmod
        # leak into normal map contexts. v0.1.2 is fail-closed: battleground-scoped
        # data is admitted only when explicitly owned by this map or proven as a
        # dependency.
        if mod in primary:
            return True, "CURRENT_MAP_MOD"
        if mod in dependencies:
            return True, "PROVEN_MAP_DEPENDENCY_MOD"
        return False, "FOREIGN_BATTLEGROUND_MOD"

    if scope == "EMBEDDED_STORMMAP_SCOPED":
        # raw_xml/mods contains embedded single-player/tutorial/try-mode .StormMap
        # GameData inside otherwise common packages such as heroes.stormmod. There
        # is no proven mapping from those packages to a standard replay map context,
        # so they must not contribute authoritative standard-match semantics.
        return False, "UNPROVEN_EMBEDDED_STORMMAP"

    return True, "COMMON_OR_SHARED"


def filter_catalog_for_map(raw: dict, map_id: str) -> tuple[dict, dict]:
    objects = {}
    kept = dropped = 0
    reasons = {}
    kept_reasons = {}
    dropped_mods = {}
    for oid, defs in (raw.get("objects") or {}).items():
        out = []
        for d in defs or []:
            ok, reason = definition_allowed(d, map_id)
            if ok:
                out.append(d)
                kept += 1
                kept_reasons[reason] = kept_reasons.get(reason, 0) + 1
            else:
                dropped += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                mod = d.get("mod") or "<none>"
                dropped_mods[mod] = dropped_mods.get(mod, 0) + 1
        if out:
            objects[oid] = out
    primary = MAP_CONTEXT_MODS.get(map_id, set())
    dependencies = MAP_CONTEXT_DEPENDENCY_MODS.get(map_id, set())
    ctx = {
        "metadata": {**(raw.get("metadata") or {}), "map_context": map_id, "context_filtered": True},
        "objects": objects,
    }
    report = {
        "map_id": map_id,
        "primary_map_mods": sorted(primary),
        "dependency_map_mods": sorted(dependencies),
        "dependency_evidence": {
            mod: sorted(units) for mod, units in MAP_CONTEXT_DEPENDENCY_EVIDENCE.get(map_id, {}).items()
        },
        "allowed_map_mods": sorted(set(primary) | set(dependencies)),
        "kept_definitions": kept,
        "dropped_definitions": dropped,
        "keep_reasons": kept_reasons,
        "drop_reasons": reasons,
        "top_dropped_mods": sorted(dropped_mods.items(), key=lambda kv: (-kv[1], kv[0]))[:50],
    }
    return ctx, report


def provenance_allowed_for_map(prov: dict, map_id: str) -> bool:
    # Convenience integrity check against effective/typed/packaged provenance.
    ok, _ = definition_allowed({"file": prov.get("file"), "mod": prov.get("mod")}, map_id)
    return ok
