from __future__ import annotations
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, asdict
from hashlib import blake2b
from pathlib import Path
from typing import Iterable

from json_backend import load, save


@dataclass(frozen=True)
class ReferenceRule:
    relation: str
    expected_prefixes: tuple[str, ...]
    dependency: bool = True
    execution: bool = False
    ownership: bool = False
    condition: bool = False
    modifier: bool = False
    formula: bool = False
    owner_boundary: str | None = None


def _type_of(key: str | None) -> str | None:
    if not key or "::" not in key:
        return None
    return key.split("::", 1)[0]


def _matches_prefix(key: str, prefixes: Iterable[str]) -> bool:
    typ = _type_of(key) or ""
    return any(typ.startswith(p) for p in prefixes)


def _effect_rule(source_type: str, field: str | None, attr: str | None) -> ReferenceRule:
    f = field or ""

    # Effect-valued fields are not automatically execution. Several GameData
    # structures use an Effect link only as a context selector (which unit/location,
    # facing/origin, AI area hint, etc.). Those links are dependencies, never owners.
    context_fields = {
        "WhichUnit", "WhichLocation", "LaunchLocation", "ImpactLocation",
        "OffsetVectorStartLocation", "OffsetVectorEndLocation", "OffsetFacingFallback",
        "ProjectionSourceEffect", "TrackerUnit", "TrackedUnit", "ImpactUnit",
        "FacingLocation", "Origin", "Target", "TargetLocation",
    }
    if f == "AINotifySettings":
        return ReferenceRule("AI_EFFECT_REFERENCE", ("CEffect",), dependency=False)
    if f in context_fields:
        return ReferenceRule("EFFECT_CONTEXT_REFERENCE", ("CEffect",), dependency=True)

    # Effect history/filter fields constrain execution but do not execute the
    # referenced effect themselves.
    if f in {"ExcludeArray", "RequireEffectArray", "ExcludeEffectArray"}:
        return ReferenceRule("EFFECT_FILTER_CONDITION", ("CEffect",), dependency=True, condition=True)

    if f in {"DisplayEffect", "PeriodicDisplayEffect", "CursorEffect"}:
        return ReferenceRule("DISPLAY_REFERENCE", ("CEffect",), dependency=False)
    if source_type.startswith("CWeapon") and f == "Effect":
        return ReferenceRule("EXECUTES_ON_ATTACK", ("CEffect",), execution=True, ownership=True)

    # LaunchEffect on CEffectLaunchMissile configures/initializes the launched ammo unit.
    # It is executable, but it is an ownership boundary: mechanics reached only through
    # this branch belong to the launched/summoned unit, not to the casting ability.
    if source_type.startswith("CEffectLaunchMissile") and f == "LaunchEffect":
        return ReferenceRule("LAUNCHED_UNIT_INITIALIZER", ("CEffect",), dependency=True, execution=True,
                             ownership=False, owner_boundary="LAUNCHED_UNIT")

    if f in {"PeriodicEffect", "PeriodicEffectArray"}:
        return ReferenceRule("PERIODIC_EXECUTES", ("CEffect",), execution=True, ownership=True)
    if f in {"InitialEffect", "LaunchEffect", "SpawnEffect", "PrepEffect", "EnableEffect"}:
        return ReferenceRule("TRIGGERS_EFFECT", ("CEffect",), execution=True, ownership=True)
    if f in {
        "FinalEffect", "ExpireEffect", "RefreshEffect", "FinishEffect", "CancelEffect",
        "DeathResponse", "AtMaxEvents", "AtMinEvents", "IncreaseEvents", "DecreaseEvents", "Event",
    }:
        return ReferenceRule("EVENT_EXECUTES", ("CEffect",), execution=True, ownership=True)
    if f in {"CaseArray", "CaseDefault"}:
        return ReferenceRule("CONDITIONAL_EXECUTES", ("CEffect",), execution=True, ownership=True)
    if f == "AreaArray":
        return ReferenceRule("EXECUTES_PER_TARGET", ("CEffect",), execution=True, ownership=True)
    if f == "DamageResponse" or (f == "Handled" and source_type.startswith("CBehavior")):
        return ReferenceRule("TRIGGERS_ON_DAMAGE", ("CEffect",), execution=True, ownership=True, condition=True)

    # These are structurally executable effect slots across CAbil/CEffect records.
    if f in {"Effect", "EffectArray", "ImpactEffect", "SearchEffect", "Handled"}:
        return ReferenceRule("EXECUTES", ("CEffect",), execution=True, ownership=True)

    # Fail closed for any other effect-valued attribute. It remains in the
    # dependency graph, but cannot manufacture execution/ownership semantics.
    return ReferenceRule("REFERENCES_EFFECT", ("CEffect",), dependency=True)


def classify_reference(source_key: str, field: str | None, attr: str | None,
                       path: list[str] | None = None, raw_relation: str | None = None) -> ReferenceRule:
    """Classify a GameData reference by structural field semantics.

    This intentionally classifies *fields*, not hero/object names. Unknown fields
    remain REFERENCES and never propagate ownership/execution.
    """
    source_type = _type_of(source_key) or ""
    f = field or ""
    a = attr or ""

    if raw_relation == "INHERITS" or a == "parent":
        return ReferenceRule("INHERITS", tuple(), dependency=True)
    if raw_relation == "FORMULA_CONSTANT":
        return ReferenceRule("FORMULA_CONSTANT", ("const",), dependency=True, formula=True)

    # UI/button identity references are not combat execution edges.
    if a in {"DefaultButtonFace", "Face", "SourceButtonFace", "ModifiedAbilButton", "OriginalAbilButton"} or f in {
        "DefaultButtonFace", "SourceButtonFace", "ModifiedAbilButton", "OriginalAbilButton",
        "Button", "Buttons", "LayoutButtons", "RankArray", "ProgressButtonArray", "Face"
    }:
        return ReferenceRule("UI_REFERENCE", ("CButton",), dependency=False)
    if a == "Requirements" or f == "Requirements":
        return ReferenceRule("UI_REQUIREMENT", ("CRequirement",), dependency=False)

    # Talent structure first. AbilityModificationArray/Modifications@Entry is a
    # typed gameplay target; in raw XML Entry is usually an attribute of the
    # Modifications node, so classify by path+attr as well as by field name.
    if source_type.startswith("CTalent") and a == "Entry" and path and "AbilityModificationArray" in path:
        return ReferenceRule("TALENT_MODIFICATION_TARGET",
                             ("CAbil", "CBehavior", "CEffect", "CValidator", "CAccumulator", "CUnit", "CWeapon"),
                             dependency=True, modifier=True)
    if source_type.startswith("CTalent") and f == "Entry":
        if path and "AbilityModificationArray" in path and "Modifications" in path:
            return ReferenceRule("TALENT_MODIFICATION_TARGET",
                                 ("CAbil", "CBehavior", "CEffect", "CValidator", "CAccumulator", "CUnit", "CWeapon"),
                                 dependency=True, modifier=True)
        return ReferenceRule("TALENT_ENTRY", ("CBehavior", "CEffect", "CAbil", "CValidator", "CAccumulator"),
                             dependency=True, modifier=True)
    if source_type.startswith("CTalent") and (f.startswith("Abil") or a in {"Abil", "Ability"}):
        return ReferenceRule("TALENT_MODIFIES_ABILITY", ("CAbil",), dependency=True, modifier=True)
    if source_type.startswith("CTalent") and "Behavior" in f:
        return ReferenceRule("TALENT_GRANTS_BEHAVIOR", ("CBehavior",), dependency=True, modifier=True)

    # Validator composition / talent checks are conditions, never owners.
    if source_type.startswith("CValidatorPlayerTalent") and f == "Value":
        return ReferenceRule("CHECKS_TALENT", ("CTalent",), dependency=True, condition=True)
    if source_type.startswith("CValidator") and f == "CombineArray":
        return ReferenceRule("CONDITIONS", ("CValidator",), dependency=True, condition=True)
    if a == "Validator" or "Validator" in f:
        if f.startswith("RemoveValidator"):
            rel = "REMOVAL_CONDITION"
        elif f.startswith("DisableValidator"):
            rel = "DISABLE_CONDITION"
        else:
            rel = "CONDITIONS"
        return ReferenceRule(rel, ("CValidator",), dependency=True, condition=True)

    # Formula/accumulator dependencies.
    if a == "Accumulator" or "Accumulator" in f:
        return ReferenceRule("COMPUTES_WITH", ("CAccumulator",), dependency=True, formula=True)

    # Unit-level defensive/spatial catalog links used by non-hero map units.
    if f == "ArmorLink" or a == "ArmorLink":
        return ReferenceRule("UNIT_USES_ARMOR", ("CArmor",), dependency=True, ownership=True)
    if "Footprint" in f or a == "Footprint":
        return ReferenceRule("UNIT_USES_FOOTPRINT", ("CFootprint",), dependency=True)
    if f == "Mover" or a == "Mover":
        return ReferenceRule("SPATIAL_REFERENCE", ("CMover",), dependency=True)

    # Explicit effect-valued attributes/fields.
    effect_fields = {
        "Effect", "EffectArray", "InitialEffect", "FinalEffect", "ExpireEffect", "RefreshEffect",
        "PeriodicEffect", "PeriodicEffectArray", "ImpactEffect", "LaunchEffect", "SpawnEffect",
        "PrepEffect", "DisplayEffect", "PeriodicDisplayEffect", "CursorEffect", "FinishEffect",
        "CancelEffect", "SearchEffect", "EnableEffect", "ProjectionSourceEffect", "CaseDefault",
        "Handled", "AtMaxEvents", "Event", "Target", "WhichUnit", "WhichLocation", "LaunchLocation",
        "ImpactLocation", "AreaArray", "CaseArray", "AINotifySettings", "ExcludeArray", "RequireEffectArray",
        "ExcludeEffectArray"
    }
    if a in {"Effect", "AreaEffect", "Handled"} or f in effect_fields:
        return _effect_rule(source_type, f, a)

    # Behaviors need source-aware directionality.
    if a in {"Behavior", "BehaviorLink", "StackBehavior"} or "Behavior" in f:
        if source_type.startswith("CAbilBehavior") and f in {"Behavior", "BehaviorArray"}:
            return ReferenceRule("ACTIVATES_BEHAVIOR", ("CBehavior",), dependency=True, execution=True, ownership=True)
        if source_type.startswith("CEffectApplyBehavior"):
            return ReferenceRule("APPLIES_BEHAVIOR", ("CBehavior",), dependency=True, execution=True, ownership=True)
        if source_type.startswith("CEffectRemoveBehavior") or f.startswith("BehaviorLinkDisable"):
            return ReferenceRule("REMOVES_BEHAVIOR", ("CBehavior",), dependency=True)
        if source_type.startswith("CValidator"):
            return ReferenceRule("CHECKS_BEHAVIOR", ("CBehavior",), dependency=True, condition=True)
        if source_type.startswith("CUnit") and f == "BehaviorArray":
            return ReferenceRule("UNIT_HAS_BEHAVIOR", ("CBehavior",), dependency=True, ownership=True)
        if source_type.startswith("CTalent"):
            return ReferenceRule("TALENT_GRANTS_BEHAVIOR", ("CBehavior",), dependency=True, modifier=True)
        return ReferenceRule("REFERENCES_BEHAVIOR", ("CBehavior",), dependency=True)

    # Weapon mode semantics are deliberately asymmetric.
    if f == "WeaponEnableArray":
        return ReferenceRule("ACTIVATES_WEAPON", ("CWeapon",), dependency=True, execution=True, ownership=True)
    if f == "WeaponDisableArray":
        return ReferenceRule("DEACTIVATES_WEAPON", ("CWeapon",), dependency=True)
    if a == "Weapon" or "Weapon" in f:
        if source_type.startswith("CUnit") and f == "WeaponArray":
            return ReferenceRule("UNIT_EQUIPS_WEAPON", ("CWeapon",), dependency=True, ownership=True)
        return ReferenceRule("REFERENCES_WEAPON", ("CWeapon",), dependency=True)

    # Ability references: association/enablement is not execution ownership.
    if a in {"Abil", "Ability", "ModifyAbil"} or "Abil" in f or "Ability" in f:
        if f.startswith("AbilLinkEnable"):
            return ReferenceRule("ENABLES_ABILITY", ("CAbil",), dependency=True)
        if f.startswith("AbilLinkDisable"):
            return ReferenceRule("DISABLES_ABILITY", ("CAbil",), dependency=True)
        if f == "ParentAbil":
            return ReferenceRule("PARENT_ABILITY", ("CAbil",), dependency=True)
        if source_type.startswith("CUnit") and f == "AbilArray":
            return ReferenceRule("UNIT_HAS_ABILITY", ("CAbil",), dependency=True, ownership=True)
        if source_type.startswith("CTalent"):
            return ReferenceRule("TALENT_MODIFIES_ABILITY", ("CAbil",), dependency=True, modifier=True)
        return ReferenceRule("REFERENCES_ABILITY", ("CAbil",), dependency=True)

    # Unit creation is an owner boundary: summon mechanics belong to the summoned unit.
    if f in {"SpawnUnit", "ProducedUnitArray", "PlaceUnit", "AmmoUnit"} or a == "Unit":
        if source_type.startswith("CEffect") and f == "SpawnUnit":
            return ReferenceRule("SPAWNS_UNIT", ("CUnit",), dependency=True, execution=True,
                                 ownership=False, owner_boundary="SUMMON")
        return ReferenceRule("REFERENCES_UNIT", ("CUnit",), dependency=True)

    # Token systems can be either token behaviors or accumulators. Keep them typed but non-owning.
    if f == "TokenId":
        return ReferenceRule("TOKEN_STATE", ("CBehaviorToken", "CAccumulatorToken", "CBehavior"), dependency=True, modifier=True)

    # Movers are spatial dependencies, not combat ownership.
    if f == "Movers":
        return ReferenceRule("SPATIAL_REFERENCE", ("CMover",), dependency=True)

    return ReferenceRule("REFERENCES", tuple(), dependency=True)


def resolve_ambiguous_candidate(edge: dict) -> dict:
    """Resolve an ambiguous raw candidate only when field semantics prove the target family."""
    rule = classify_reference(edge.get("source_key", ""), edge.get("field"), edge.get("attr"),
                              edge.get("path"), edge.get("relation"))
    candidates = list(edge.get("candidate_keys") or edge.get("replay_candidate_keys") or [])
    if not rule.expected_prefixes:
        return {"status": "UNRESOLVED_UNKNOWN_FIELD", "rule": rule, "matches": []}
    matches = [k for k in candidates if _matches_prefix(k, rule.expected_prefixes)]
    if len(matches) == 1:
        return {"status": "RESOLVED_TYPED_FIELD", "rule": rule, "target_key": matches[0], "matches": matches}
    if not matches:
        return {"status": "UNRESOLVED_EXPECTED_TYPE_MISSING", "rule": rule, "matches": []}
    # TokenId is a shared runtime identity: the same id can intentionally exist as
    # an accumulator and one or more token behaviors/effects. Do not guess one owner.
    # Materialize the shared identity while keeping every concrete candidate visible.
    if rule.relation == "TOKEN_STATE":
        return {"status": "RESOLVED_SHARED_TOKEN_IDENTITY", "rule": rule,
                "identity_only": True, "matches": matches}
    return {"status": "UNRESOLVED_TYPED_AMBIGUITY", "rule": rule, "matches": matches}


def _field_values(record: dict | None, field: str) -> list[str]:
    out=[]
    if not record:
        return out
    for node in (record.get("fields") or {}).get(field,[]) or []:
        attrs=node.get("attrs") or {}
        for name in ("value","Value","Link"):
            if attrs.get(name) is not None:
                out.append(str(attrs[name]))
                break
    return out


def _is_pure_missile_ammo(records: dict, source_record: dict | None) -> bool:
    """A CEffectLaunchMissile can launch either an implementation projectile or a
    gameplay unit/summon. Only the former keeps cast ownership through LaunchEffect.

    GameData exposes this distinction on the AmmoUnit itself: projectile units carry
    FlagArray[index=Missile,value=1]. Summoned gameplay units (e.g. Zagara Roach)
    do not, and may carry weapons/abilities instead. Fail closed if unresolved.
    """
    ammo_ids=_field_values(source_record,"AmmoUnit")
    if len(ammo_ids)!=1:
        return False
    unit=records.get(f"CUnit::{ammo_ids[0]}")
    if not unit:
        # Some unit subclasses may use a more specific CUnit* catalog type.
        matches=[r for k,r in records.items() if k.startswith("CUnit") and r.get("object_id")==ammo_ids[0]]
        if len(matches)!=1:
            return False
        unit=matches[0]
    for node in (unit.get("fields") or {}).get("FlagArray",[]) or []:
        attrs=node.get("attrs") or {}
        if str(attrs.get("index") or "").lower()=="missile" and str(attrs.get("value") or "") in {"1","true","True"}:
            return True
    return False


def _source_operation(record: dict | None, edge: dict) -> str | None:
    if not record:
        return None
    order = (edge.get("provenance") or {}).get("order")
    file = (edge.get("provenance") or {}).get("file")
    for d in record.get("definitions", []) or []:
        if order is not None and d.get("order") == order:
            return d.get("operation")
    for d in record.get("definitions", []) or []:
        if file and d.get("file") == file:
            return d.get("operation")
    return None


def _edge_id(e: dict) -> str:
    raw = "|".join([
        str(e.get("source_key")), str(e.get("target_key") or e.get("target_id")),
        str(e.get("relation")), "/".join(e.get("path") or []), str(e.get("attr")),
        str((e.get("provenance") or {}).get("order")),
    ]).encode("utf-8")
    return "ref_" + blake2b(raw, digest_size=10).hexdigest()


def _materialize_typed_edge(edge: dict, records: dict, *, ambiguous: bool = False) -> tuple[dict | None, dict | None]:
    identity_matches = []
    if ambiguous:
        resolved = resolve_ambiguous_candidate(edge)
        rule: ReferenceRule = resolved["rule"]
        if resolved.get("identity_only"):
            target_key = None
            identity_matches = list(resolved.get("matches") or [])
            resolution = resolved["status"]
        elif not resolved.get("target_key"):
            u = dict(edge)
            u["typed_status"] = resolved["status"]
            u["expected_prefixes"] = list(rule.expected_prefixes)
            u["matching_candidate_keys"] = resolved.get("matches", [])
            return None, u
        else:
            target_key = resolved["target_key"]
            resolution = resolved["status"]
    else:
        rule = classify_reference(edge.get("source_key", ""), edge.get("field"), edge.get("attr"),
                                  edge.get("path"), edge.get("relation"))
        target_key = edge.get("target_key")
        resolution = edge.get("resolution") or "PRE_RESOLVED"

    source_key = edge.get("source_key")

    # Contextual refinement for CEffectLaunchMissile.LaunchEffect. A pure missile
    # is an implementation vehicle for the casting ability, so its LaunchEffect
    # remains ability-owned. If AmmoUnit is a gameplay unit/summon, keep the
    # launched-unit owner boundary. This replaces the old blanket LaunchEffect
    # boundary that incorrectly cut Varian Lion's Fang / Leoric Drain Hope.
    if rule.relation == "LAUNCHED_UNIT_INITIALIZER" and _is_pure_missile_ammo(records, records.get(source_key)):
        rule = ReferenceRule("PROJECTILE_LAUNCH_EXECUTES", ("CEffect",), dependency=True,
                             execution=True, ownership=True)

    op = _source_operation(records.get(source_key), edge)
    ownership = rule.ownership
    owner_boundary = rule.owner_boundary
    if op == "CONTRIBUTE" and ownership:
        ownership = False
        owner_boundary = "CONTRIBUTION"

    target_type = _type_of(target_key)
    out = {
        "source_key": source_key,
        "source_type": _type_of(source_key),
        "target_key": target_key,
        "target_type": target_type,
        "target_id": edge.get("target_id") or (target_key.split("::", 1)[1] if target_key and "::" in target_key else None),
        "field": edge.get("field"),
        "path": edge.get("path") or [],
        "attr": edge.get("attr"),
        "relation": rule.relation,
        "resolution": resolution,
        "expected_prefixes": list(rule.expected_prefixes),
        "propagation": {
            "dependency": rule.dependency,
            "execution": rule.execution,
            "ownership": ownership,
            "condition": rule.condition,
            "modifier": rule.modifier,
            "formula": rule.formula,
        },
        "owner_boundary": owner_boundary,
        "source_definition_operation": op,
        "target_in_replay_catalog": target_key in records,
        "provenance": edge.get("provenance") or {},
    }
    if ambiguous:
        out["raw_candidate_keys"] = edge.get("candidate_keys") or []
    if identity_matches:
        out["identity_candidate_keys"] = identity_matches
        out["target_type"] = "TOKEN_IDENTITY"
        out["target_in_replay_catalog"] = False
    out["edge_id"] = _edge_id(out)
    return out, None


def build_typed_reference_graph_data(cat: dict):
    records = cat.get("records", {})
    typed = []
    unresolved = []

    for e in cat.get("reference_edges", []) or []:
        t, u = _materialize_typed_edge(e, records, ambiguous=False)
        if t: typed.append(t)
        if u: unresolved.append(u)
    for e in cat.get("ambiguous_reference_candidates", []) or []:
        t, u = _materialize_typed_edge(e, records, ambiguous=True)
        if t: typed.append(t)
        if u: unresolved.append(u)

    uniq = {}
    for e in typed:
        sig = (e["source_key"], e.get("target_key"), e["relation"], tuple(e.get("path") or []),
               e.get("attr"), (e.get("provenance") or {}).get("order"))
        uniq[sig] = e
    typed = list(uniq.values())

    rel = Counter(e["relation"] for e in typed)
    res = Counter(e["resolution"] for e in typed)
    boundaries = Counter(e.get("owner_boundary") or "NONE" for e in typed)
    prop = Counter()
    for e in typed:
        for k, v in e["propagation"].items():
            if v: prop[k] += 1
    unresolved_counts = Counter(u.get("typed_status", "UNKNOWN") for u in unresolved)

    graph = {
        "metadata": {
            "builder": "typed_reference_resolver_v1_2_combat_bridge",
            "fail_closed": True,
            "principle": "reference != execution != ownership",
            "map_context": (cat.get("metadata") or {}).get("map_context"),
        },
        "edges": sorted(typed, key=lambda e: (e["source_key"], e.get("target_key") or "", e["relation"], e["edge_id"])),
        "unresolved": unresolved,
    }
    report = {
        "summary": {
            "typed_edges": len(typed), "unresolved_references": len(unresolved),
            "ownership_edges": prop["ownership"], "execution_edges": prop["execution"],
            "condition_edges": prop["condition"], "modifier_edges": prop["modifier"],
            "formula_edges": prop["formula"], "contribution_boundaries": boundaries["CONTRIBUTION"],
            "summon_boundaries": boundaries["SUMMON"],
            "external_targets": sum(1 for e in typed if not e["target_in_replay_catalog"]),
        },
        "relation_counts": dict(rel), "resolution_counts": dict(res),
        "owner_boundary_counts": dict(boundaries), "propagation_counts": dict(prop),
        "unresolved_counts": dict(unresolved_counts),
    }
    return graph, report


def build_typed_reference_graph(replay_catalog_path: Path, out_path: Path, report_path: Path) -> dict:
    cat = load(replay_catalog_path)
    graph, report = build_typed_reference_graph_data(cat)
    save(out_path, graph); save(report_path, report)
    return report

def has_propagating_path(edges: list[dict], start_key: str, target_key: str, channel: str = "ownership") -> bool:
    adj = defaultdict(list)
    for e in edges:
        if (e.get("propagation") or {}).get(channel):
            adj[e["source_key"]].append(e.get("target_key"))
    q = deque([start_key]); seen = set()
    while q:
        cur = q.popleft()
        if cur == target_key:
            return True
        if cur in seen:
            continue
        seen.add(cur)
        for nxt in adj.get(cur, []):
            if nxt and nxt not in seen:
                q.append(nxt)
    return False
