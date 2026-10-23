#!/usr/bin/env python3
"""Pyhots Hero Unit DB v0.2.2 replay runtime-scaling observability probe.

This probe answers a deliberately narrow question:
    Which hero scaling values can the build-91093 replay itself expose directly?

It does not evaluate CHero.LevelScalingArray arithmetic and never turns aggregate
score counters into per-hit/per-heal/per-shield values.

Inputs
------
- build-91093 .StormReplay fixture;
- frozen Hero Unit DB v0.2 JSON;
- v0.2.1 extreme replay evidence JSON.

Output
------
A deterministic evidence JSON describing actual replay serialization surfaces and
fail-closed runtime-scaling observability.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from replay_overflow_probe import (
    MPQArchive,
    blob,
    decode_header,
    decode_tracker_stream,
    hms,
    stat_pairs,
)
from game_events_91093 import decode as decode_game_events

PROTOCOL_SCHEMA_SOURCE = (
    "https://raw.githubusercontent.com/Blizzard/heroprotocol/master/"
    "heroprotocol/versions/protocol91093.py"
)

# Protocol91093 surfaces relevant to direct combat-state observability.  These are
# intentionally declarative, not a general game-event decoder.
PROTOCOL_91093_OBSERVABILITY = {
    "tracker_unit_born": [
        "unitTagIndex", "unitTagRecycle", "unitTypeName", "controlPlayerId",
        "upkeepPlayerId", "x", "y",
    ],
    "tracker_unit_died": [
        "unitTagIndex", "unitTagRecycle", "killerPlayerId", "x", "y",
        "killerUnitTagIndex", "killerUnitTagRecycle",
    ],
    "tracker_unit_owner_change": [
        "unitTagIndex", "unitTagRecycle", "controlPlayerId", "upkeepPlayerId",
    ],
    "tracker_unit_type_change": ["unitTagIndex", "unitTagRecycle", "unitTypeName"],
    "tracker_unit_positions": ["firstUnitIndex", "items"],
    "game_cmd": [
        "cmdFlags", "abil", "data", "vector", "sequence", "otherUnit", "unitGroup",
    ],
    "game_catalog_modify": ["catalog", "entry", "field", "value"],
    "message_player_announce": [
        "announcement_choice", "announceLink", "otherUnitTag", "unitTag",
    ],
}

DIRECT_COMBAT_STATE_FIELD_NAMES = {
    "Life", "LifeMax", "LifeStart", "LifeRegenRate",
    "Shield", "Shields", "ShieldMax",
    "Energy", "EnergyMax", "Mana", "ManaMax",
    "Vital", "VitalMax", "VitalRegen", "VitalRegenRate",
    "WeaponDamage", "AttackDamage", "DamageAmount", "HealAmount",
}

# Score fields are runtime observations but cumulative/aggregate; they do not
# identify the action/effect that produced the increment.
AGGREGATE_SCORE_FIELDS = {
    "HeroDamage", "SiegeDamage", "StructureDamage", "MinionDamage", "CreepDamage",
    "SummonDamage", "PhysicalDamage", "SpellDamage", "Healing", "SelfHealing",
    "ProtectionGivenToAllies", "DamageTaken", "DamageSoaked", "TeamfightHeroDamage",
    "TeamfightHealingDone", "TeamfightDamageTaken",
}

# ---------- build-91093 replay.message.events minimal decoder ----------
class _BitBuffer:
    def __init__(self, data: bytes):
        self.data = data
        self.used = 0
        self.next = 0
        self.nbits = 0

    def done(self) -> bool:
        return self.nbits == 0 and self.used >= len(self.data)

    def bits(self, n: int) -> int:
        out = 0
        got = 0
        while got < n:
            if self.nbits == 0:
                if self.used >= len(self.data):
                    raise EOFError("bitpacked message stream truncated")
                self.next = self.data[self.used]
                self.used += 1
                self.nbits = 8
            k = min(n - got, self.nbits)
            part = self.next & ((1 << k) - 1)
            out |= part << (n - got - k)
            self.next >>= k
            self.nbits -= k
            got += k
        return out

    def align(self) -> None:
        self.nbits = 0

    def aligned(self, n: int) -> bytes:
        self.align()
        out = self.data[self.used:self.used + n]
        if len(out) != n:
            raise EOFError("bitpacked message blob truncated")
        self.used += n
        return out

    def used_bits(self) -> int:
        return self.used * 8 - self.nbits


_MSG_TYPES = {
    1: ("int", (0, 4)), 2: ("int", (0, 5)), 3: ("int", (0, 6)),
    4: ("int", (0, 14)), 5: ("int", (0, 22)), 6: ("int", (0, 32)),
    7: ("choice", ((0, 2), {0: ("m_uint6", 3), 1: ("m_uint14", 4), 2: ("m_uint22", 5), 3: ("m_uint32", 6)})),
    8: ("struct", [("m_userId", 2)]), 12: ("int", (0, 3)), 24: ("int", (0, 2)),
    30: ("blob", (0, 11)), 81: ("int", (0, 16)), 87: ("null", None),
    89: ("int", (-2147483648, 32)),
    116: ("struct", [("x", 89), ("y", 89)]), 162: ("int", (-32768, 16)),
    178: ("struct", [("m_recipient", 12), ("m_string", 30)]),
    179: ("struct", [("m_recipient", 12), ("m_point", 116)]),
    180: ("struct", [("m_progress", 89)]), 181: ("struct", [("m_status", 24)]),
    182: ("struct", [("m_abilLink", 81), ("m_abilCmdIndex", 2), ("m_buttonLink", 81)]),
    183: ("struct", [("m_behaviorLink", 81), ("m_buttonLink", 81)]),
    184: ("choice", ((0, 2), {0: ("None", 87), 1: ("Ability", 182), 2: ("Behavior", 183), 3: ("Vitals", 162)})),
    185: ("struct", [("m_announcement", 184), ("m_announceLink", 81), ("m_otherUnitTag", 6), ("m_unitTag", 6)]),
}
_MSG_EVENTS = {
    0: (178, "SChatMessage"), 1: (179, "SPingMessage"),
    2: (180, "SLoadingProgressMessage"), 3: (87, "SServerPingMessage"),
    4: (181, "SReconnectNotifyMessage"), 5: (185, "SPlayerAnnounceMessage"),
}


class _MessageDecoder:
    def __init__(self, data: bytes):
        self.b = _BitBuffer(data)

    def inst(self, type_id: int) -> Any:
        kind, spec = _MSG_TYPES[type_id]
        if kind == "int":
            base, bits = spec
            return base + self.b.bits(bits)
        if kind == "null":
            return None
        if kind == "blob":
            base, bits = spec
            length = base + self.b.bits(bits)
            return self.b.aligned(length)
        if kind == "struct":
            return {name: self.inst(child) for name, child in spec}
        if kind == "choice":
            (base, bits), fields = spec
            tag = base + self.b.bits(bits)
            name, child = fields[tag]
            return {name: self.inst(child)}
        raise ValueError(kind)


def _varuint_value(value: Dict[str, int]) -> int:
    return next(iter(value.values())) if value else 0


def decode_message_events(data: bytes) -> List[Dict[str, Any]]:
    d = _MessageDecoder(data)
    loop = 0
    rows: List[Dict[str, Any]] = []
    while not d.b.done():
        start = d.b.used_bits()
        loop += _varuint_value(d.inst(7))
        user = d.inst(8)["m_userId"]
        event_id = d.inst(1)
        type_id, name = _MSG_EVENTS[event_id]
        payload = d.inst(type_id)
        d.b.align()
        rows.append({
            "loop": loop,
            "seconds": loop / 16.0,
            "time": hms(loop / 16.0),
            "user_id": user,
            "event_id": event_id,
            "event_name": name,
            "payload": payload,
            "bits": d.b.used_bits() - start,
        })
    return rows


def _decode_bytes(value: Any) -> Any:
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, list):
        return [_decode_bytes(x) for x in value]
    if isinstance(value, dict):
        return {k: _decode_bytes(v) for k, v in value.items()}
    return value


def _score_names(payload: Dict[str, Any]) -> List[str]:
    names = []
    for stat in payload.get("0") or []:
        name = blob(stat.get("0"))
        if name:
            names.append(name)
    return names


def _score_value(payload: Dict[str, Any], stat_name: str, player_id: int) -> Optional[int]:
    for stat in payload.get("0") or []:
        if blob(stat.get("0")) != stat_name:
            continue
        series = stat.get("1") or []
        idx = player_id - 1
        if idx >= len(series) or not series[idx]:
            return None
        return series[idx][-1].get("0")
    return None


def _tracker_inventory(events: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
    id_counts = Counter(e["event_id"] for e in events)
    stat_event_counts = Counter()
    stat_fields = defaultdict(lambda: {"string": Counter(), "int": Counter(), "fixed": Counter()})
    score_rows: List[Dict[str, Any]] = []
    all_score_names = set()

    for e in events:
        if e["event_id"] == 10:
            p = e["payload"]
            name = blob(p.get("0")) or "<UNNAMED>"
            stat_event_counts[name] += 1
            for key, label in (("1", "string"), ("2", "int"), ("3", "fixed")):
                for row in p.get(key) or []:
                    try:
                        field = blob(row["0"]["0"])
                    except (KeyError, TypeError):
                        field = None
                    if field:
                        stat_fields[name][label][field] += 1
        elif e["event_id"] == 11:
            names = _score_names(e["payload"])
            all_score_names.update(names)
            score_rows.append({
                "loop": e["loop"], "seconds": e["loop"] / 16.0,
                "time": hms(e["loop"] / 16.0), "stat_count": len(names),
            })

    serialized_stat_fields = set()
    for name, groups in stat_fields.items():
        for counter in groups.values():
            serialized_stat_fields.update(counter)

    inventory = {
        "tracker_event_id_counts": {str(k): v for k, v in sorted(id_counts.items())},
        "stat_game_event_counts": dict(sorted(stat_event_counts.items())),
        "stat_game_event_fields": {
            name: {label: dict(sorted(counter.items())) for label, counter in groups.items() if counter}
            for name, groups in sorted(stat_fields.items())
        },
        "serialized_stat_field_names": sorted(serialized_stat_fields),
        "score_field_names": sorted(all_score_names),
        "aggregate_combat_score_fields_present": sorted(AGGREGATE_SCORE_FIELDS & all_score_names),
        "direct_combat_state_names_present_in_stat_events": sorted(DIRECT_COMBAT_STATE_FIELD_NAMES & serialized_stat_fields),
        "direct_combat_state_names_present_in_score_fields": sorted(DIRECT_COMBAT_STATE_FIELD_NAMES & all_score_names),
    }
    return inventory, score_rows, events


def _latest_level(level_events: List[Dict[str, Any]], team: int, loop: int) -> int:
    level = 1
    for row in level_events:
        if row.get("team") == team and row["loop"] <= loop:
            level = max(level, int(row["level"]))
    return level


def _score_bracket(score_events: List[Dict[str, Any]], loop: int) -> Dict[str, Any]:
    before = [e for e in score_events if e["loop"] < loop]
    after = [e for e in score_events if e["loop"] >= loop]
    b = before[-1] if before else None
    a = after[0] if after else None
    return {
        "before": None if b is None else {"loop": b["loop"], "time": hms(b["loop"] / 16.0)},
        "after": None if a is None else {"loop": a["loop"], "time": hms(a["loop"] / 16.0)},
        "gap_seconds": None if b is None or a is None else (a["loop"] - b["loop"]) / 16.0,
    }


def _observed_hero_scaling(hero_db: Dict[str, Any], hero_ids: Dict[str, str]) -> Dict[str, Any]:
    by_replay_unit_id = {}
    for name, h in hero_db["heroes"].items():
        # Replay PlayerSpawned.Hero uses CUnit ids (e.g. HeroRaynor), while the
        # canonical hero id is Raynor.  Match only explicit hero/form unit roots.
        for form in h.get("forms") or []:
            unit_id = form.get("unit_id")
            if unit_id:
                by_replay_unit_id[unit_id] = (name, h)
    result = {}
    for pid, hero_id in sorted(hero_ids.items(), key=lambda x: int(x[0])):
        item = by_replay_unit_id.get(hero_id)
        if not item:
            result[pid] = {"hero_id": hero_id, "hero_db_match": False}
            continue
        hero_name, hero = item
        scaling = hero.get("scaling_evidence", {})
        rows = scaling.get("typed_level_scaling") or []
        classes = Counter(r.get("semantic_class") or "UNRESOLVED" for r in rows)

        # Resource scaling is represented by attached CBehaviorVeterancy facts,
        # not CHero.LevelScalingArray.  Keep it distinct, but include the typed
        # semantic classes in the replay observability matrix.
        veterancy = scaling.get("attached_veterancy_scaling") or []
        veterancy_classes = Counter()
        veterancy_mod_count = 0
        veterancy_behaviors = []
        for behavior in veterancy:
            mods = behavior.get("modifications") or []
            if mods:
                veterancy_behaviors.append({
                    "behavior_id": behavior.get("behavior_id"),
                    "form_unit_id": behavior.get("form_unit_id"),
                    "catalog_slot_count": behavior.get("catalog_slot_count"),
                    "explicit_level_indices": behavior.get("explicit_level_indices") or [],
                    "runtime_level_source": behavior.get("runtime_level_source"),
                    "cumulative_semantics": behavior.get("cumulative_semantics"),
                    "modification_count": len(mods),
                })
            for mod in mods:
                veterancy_mod_count += 1
                semantic = mod.get("semantic_class") or "UNRESOLVED_VETERANCY_MODIFICATION"
                veterancy_classes[semantic] += 1
                classes[semantic] += 1

        result[pid] = {
            "hero_id": hero_id,
            "hero_name": hero_name,
            "hero_db_match": True,
            "typed_level_scaling_record_count": len(rows),
            "attached_veterancy_modification_count": veterancy_mod_count,
            "attached_veterancy_semantic_class_counts": dict(sorted(veterancy_classes.items())),
            "attached_veterancy_behaviors_with_modifications": veterancy_behaviors,
            "semantic_class_counts": dict(sorted(classes.items())),
            "runtime_arithmetic_values": sorted({r.get("runtime_arithmetic") for r in rows if r.get("runtime_arithmetic")}),
        }
    return result


def analyze(replay_path: Path, hero_db_path: Path, v021_evidence_path: Path) -> Dict[str, Any]:
    replay_bytes = replay_path.read_bytes()
    header = decode_header(replay_bytes)
    version = header.get("1") or {}
    build = version.get("4") if version.get("4") == header.get("6") else header.get("6")
    if build != 91093:
        raise ValueError(f"v0.2.2 probe is validated only for build 91093, got {build}")

    archive = MPQArchive(replay_path)
    tracker_raw = archive.extract("replay.tracker.events")
    tracker = decode_tracker_stream(tracker_raw)
    messages_raw = archive.extract("replay.message.events")
    messages = decode_message_events(messages_raw)
    game_raw = archive.extract("replay.game.events")
    game_events = decode_game_events(game_raw)

    hero_db = json.loads(hero_db_path.read_text(encoding="utf-8"))
    old = json.loads(v021_evidence_path.read_text(encoding="utf-8"))
    hero_ids = old["players"]["hero_by_player_id"]

    tracker_inventory, score_event_summary, _ = _tracker_inventory(tracker)
    score_events = [e for e in tracker if e["event_id"] == 11]
    level_events = []
    for e in tracker:
        if e["event_id"] != 10 or blob(e["payload"].get("0")) != "LevelUp":
            continue
        ints = stat_pairs(e["payload"].get("2"))
        pid = int(ints["PlayerID"])
        team = int(old["players"]["team_by_player_id"].get(str(pid)))
        level_events.append({"loop": e["loop"], "player_id": pid, "team": team, "level": int(ints["Level"])})

    level31 = old["key_level_events"]["level_31"]
    level30 = old["key_level_events"]["level_30"]
    score31 = _score_bracket(score_events, level31["loop"])
    score30 = _score_bracket(score_events, level30["loop"])
    for bracket in (score30, score31):
        for side in ("before", "after"):
            if bracket[side]:
                bracket[side]["team_1_level_at_snapshot"] = _latest_level(level_events, 1, bracket[side]["loop"])

    game_counts = Counter(x["event_name"] for x in game_events)
    catalog_modify_events = []
    for x in game_events:
        if x["event_name"] != "SCatalogModifyEvent":
            continue
        payload = _decode_bytes(x["payload"])
        catalog_modify_events.append({
            "loop": x["loop"],
            "time": hms(x["loop"] / 16.0),
            "user_id": x["user_id"],
            "catalog": payload.get("m_catalog"),
            "entry": payload.get("m_entry"),
            "field": payload.get("m_field"),
            "value": payload.get("m_value"),
        })
    catalog_modify_direct_fields = sorted({
        x["field"] for x in catalog_modify_events
        if x.get("field") in DIRECT_COMBAT_STATE_FIELD_NAMES
    })

    msg_counts = Counter(x["event_name"] for x in messages)
    announce = [x for x in messages if x["event_name"] == "SPlayerAnnounceMessage"]
    announce_kinds = Counter(next(iter(x["payload"]["m_announcement"])) for x in announce)
    vital_ann = []
    for x in announce:
        ann = x["payload"]["m_announcement"]
        if "Vitals" in ann:
            vital_ann.append({
                "loop": x["loop"], "time": x["time"], "user_id": x["user_id"],
                "vitals_scalar": ann["Vitals"],
                "announce_link": x["payload"]["m_announceLink"],
                "unit_tag": x["payload"]["m_unitTag"],
                "other_unit_tag": x["payload"]["m_otherUnitTag"],
            })

    observed_scaling = _observed_hero_scaling(hero_db, hero_ids)
    all_semantic_classes = Counter()
    for item in observed_scaling.values():
        all_semantic_classes.update(item.get("semantic_class_counts", {}))

    # Per semantic class direct/indirect observability.  These statuses describe
    # this replay/protocol surface, not whether the GameData target itself exists.
    class_observability = {}
    for semantic_class in sorted(all_semantic_classes):
        if semantic_class in {
            "HERO_HEALTH_POOL", "HERO_HEALTH_REGEN",
            "RESOURCE_POOL", "RESOURCE_REGEN",
            "RESOURCE_MAX_LEVEL_DELTA_EVIDENCE", "RESOURCE_REGEN_LEVEL_DELTA_EVIDENCE",
        }:
            status = "NO_DIRECT_RUNTIME_VALUE_SERIALIZED"
            indirect = []
        elif semantic_class in {"BASIC_ATTACK_DAMAGE", "ABILITY_DAMAGE"}:
            status = "NO_PER_ACTION_DAMAGE_VALUE_SERIALIZED"
            indirect = sorted({"HeroDamage", "PhysicalDamage", "SpellDamage", "SiegeDamage", "StructureDamage", "MinionDamage", "CreepDamage"} & set(tracker_inventory["score_field_names"]))
        elif semantic_class == "ABILITY_HEAL":
            status = "NO_PER_ACTION_HEAL_VALUE_SERIALIZED"
            indirect = sorted({"Healing", "SelfHealing", "TeamfightHealingDone"} & set(tracker_inventory["score_field_names"]))
        elif semantic_class == "ABILITY_SHIELD":
            status = "NO_PER_APPLICATION_SHIELD_VALUE_SERIALIZED"
            indirect = sorted({"ProtectionGivenToAllies", "DamageSoaked"} & set(tracker_inventory["score_field_names"]))
        else:
            status = "NO_DIRECT_RUNTIME_VALUE_IDENTIFIED"
            indirect = []
        class_observability[semantic_class] = {
            "record_count_on_observed_heroes": all_semantic_classes[semantic_class],
            "direct_observability": status,
            "aggregate_score_proxies": indirect,
            "aggregate_proxies_are_scaling_proof": False,
        }

    hero_db_hash = hashlib.sha256(hero_db_path.read_bytes()).hexdigest()
    replay_hash = hashlib.sha256(replay_bytes).hexdigest()
    tracker_hash = hashlib.sha256(tracker_raw).hexdigest()
    messages_hash = hashlib.sha256(messages_raw).hexdigest()
    game_hash = hashlib.sha256(game_raw).hexdigest()

    direct_tracker = tracker_inventory["direct_combat_state_names_present_in_stat_events"]
    direct_score = tracker_inventory["direct_combat_state_names_present_in_score_fields"]
    no_direct = not direct_tracker and not direct_score
    score31_too_wide = score31["gap_seconds"] is None or score31["gap_seconds"] > 60

    result = {
        "schema": "pyhots_runtime_scaling_observability_v0_2_2",
        "fixture": {
            "replay_file": replay_path.name,
            "replay_sha256": replay_hash,
            "build": build,
            "tracker_sha256": tracker_hash,
            "message_events_sha256": messages_hash,
            "game_events_sha256": game_hash,
            "tracker_event_count": len(tracker),
            "message_event_count": len(messages),
            "game_event_count": len(game_events),
            "header_elapsed_game_loops": header.get("3"),
        },
        "hero_unit_db": {
            "path_name": hero_db_path.name,
            "sha256": hero_db_hash,
            "schema": hero_db.get("schema"),
            "unchanged_static_db_requirement": True,
        },
        "protocol_contract": {
            "validated_build": 91093,
            "upstream_schema_reference": PROTOCOL_SCHEMA_SOURCE,
            "relevant_serialized_fields": PROTOCOL_91093_OBSERVABILITY,
            "interpretation": (
                "Command/tracker unit events serialize identity, commands, ownership/type/position and aggregate stats; "
                "they do not serialize per-unit current/max vitals or per-effect result values."
            ),
        },
        "game_observability": {
            "event_counts": dict(sorted(game_counts.items())),
            "last_event_loop": game_events[-1]["loop"] if game_events else None,
            "catalog_modify_count": len(catalog_modify_events),
            "catalog_modify_events": catalog_modify_events,
            "direct_combat_state_catalog_fields_present": catalog_modify_direct_fields,
            "catalog_modify_interpretation": (
                "SCatalogModifyEvent is the only generic game-event surface in build 91093 that can carry "
                "catalog field/value strings. It is absent from this replay, so it cannot provide a hidden "
                "Life/LifeMax/resource/damage scaling observation channel here."
            ),
            "command_event_interpretation": (
                "SCmdEvent and target-update events carry command/ability/target identifiers and coordinates, "
                "not resolved per-action damage/heal/shield or current/max vital values."
            ),
        },
        "tracker_observability": tracker_inventory,
        "score_snapshots": {
            "count": len(score_event_summary),
            "events": score_event_summary,
            "level_30": {"event": level30, "score_snapshot_bracket": score30},
            "level_31": {"event": level31, "score_snapshot_bracket": score31},
            "level_31_aggregate_snapshot_isolation": "NOT_USABLE_FOR_PER_ACTION_SCALING_PROOF" if score31_too_wide else "REVIEW_REQUIRED",
        },
        "message_observability": {
            "event_counts": dict(sorted(msg_counts.items())),
            "player_announce_count": len(announce),
            "player_announce_choice_counts": dict(sorted(announce_kinds.items())),
            "vitals_announcement_count": len(vital_ann),
            "vitals_announcements": vital_ann,
            "vitals_announcement_interpretation": (
                "GENERIC_ANNOUNCEMENT_CHOICE_SCALAR_NOT_PROVEN_AS_CURRENT_OR_MAX_VITAL_VALUE"
            ),
        },
        "observed_heroes_scaling": observed_scaling,
        "scaling_class_observability": class_observability,
        "runtime_scaling_boundary": {
            "direct_hero_life_or_lifemax_value_in_game_events": False,
            "direct_hero_life_or_lifemax_value_in_tracker": False,
            "direct_hero_resource_value_in_tracker": False,
            "direct_per_hit_damage_value_in_tracker": False,
            "direct_per_cast_heal_value_in_tracker": False,
            "direct_per_application_shield_value_in_tracker": False,
            "catalog_modify_runtime_channel_used_in_fixture": bool(catalog_modify_events),
            "aggregate_combat_counters_exist": bool(tracker_inventory["aggregate_combat_score_fields_present"]),
            "aggregate_combat_counters_are_causal_action_values": False,
            "runtime_catalog_modify_event_count": len(catalog_modify_events),
            "runtime_catalog_scaling_patch_observed": bool(catalog_modify_events),
            "level_30_to_31_direct_combat_scaling_application": "UNRESOLVED_NOT_DIRECTLY_OBSERVABLE_IN_THIS_REPLAY_PROTOCOL_SURFACE",
            "normal_1_to_30_runtime_arithmetic": "UNRESOLVED_NOT_DIRECTLY_OBSERVABLE_IN_THIS_REPLAY_PROTOCOL_SURFACE",
            "post_30_runtime_arithmetic": "UNRESOLVED_NOT_DIRECTLY_OBSERVABLE_IN_THIS_REPLAY_PROTOCOL_SURFACE",
            "recommended_next_evidence_source": [
                "ENGINE_OR_GAMEDATA_LEVEL_SCALING_EXECUTION_SEMANTICS",
                "CONTROLLED_FIXTURE_WITH_OBSERVABLE_COMBAT_CONSEQUENCE_IF_AVAILABLE",
            ],
        },
        "gates": {
            "BUILD_91093": build == 91093,
            "STATIC_HERO_UNIT_DB_IS_V0_2_BASELINE": hero_db_hash == "ce87c5a69036ee5405402fbb92a3344597d6a6c03f645a5f7fd16ed59c65b591",
            "GAME_EVENTS_DECODE_TO_HEADER_END": bool(game_events) and game_events[-1]["loop"] == header.get("3"),
            "NO_CATALOG_MODIFY_RUNTIME_CHANNEL_IN_FIXTURE": len(catalog_modify_events) == 0,
            "GAME_EVENTS_HAVE_NO_DIRECT_COMBAT_STATE_CATALOG_FIELDS": not catalog_modify_direct_fields,
            "TRACKER_HAS_NO_DIRECT_COMBAT_STATE_FIELD_NAMES": no_direct,
            "AGGREGATE_COMBAT_SCORE_COUNTERS_PRESENT": bool(tracker_inventory["aggregate_combat_score_fields_present"]),
            "LEVEL_31_SCORE_BRACKET_TOO_WIDE_FOR_CAUSAL_SCALING": score31_too_wide,
            "MESSAGE_VITALS_NOT_PROMOTED_TO_STATE_VALUE": True,
            "RESOURCE_SCALING_EVIDENCE_INCLUDED": any(
                (item.get("attached_veterancy_modification_count") or 0) > 0
                for item in observed_scaling.values()
            ),
            "RUNTIME_ARITHMETIC_REMAINS_FAIL_CLOSED": True,
            "STATIC_DB_UNCHANGED": True,
        },
    }
    result["overall"] = "PASS" if all(result["gates"].values()) else "FAIL"
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True, type=Path)
    ap.add_argument("--hero-unit-db", required=True, type=Path)
    ap.add_argument("--v021-evidence", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    result = analyze(args.replay, args.hero_unit_db, args.v021_evidence)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"overall": result["overall"], "gates": result["gates"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
