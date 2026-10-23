#!/usr/bin/env python3
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import math
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

try:
    import orjson
except ImportError as exc:  # pragma: no cover
    raise SystemExit("orjson is required: py -m pip install orjson") from exc

import replay_protocol_builder as rp

VERSION = "0.1.4"
CACHE_SCHEMA = "WORLD_STATE_V0_1_3"
CACHE_CONFIG_VERSION = "0.1.3"
FIXED_POINT_SCALE = 4096.0


def dump_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(value, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS))


def load_json(path: Path) -> Any:
    return orjson.loads(path.read_bytes())


def _idx(data: Any, n: int) -> Any:
    return rp._data_index(data, n)


def _event_type(e: dict) -> str:
    return rp._event_type(e)


def _event_time(e: dict) -> Optional[float]:
    return rp._event_time(e)


def _event_player(e: dict) -> Optional[str]:
    return rp._event_player(e)


def _to_int(v: Any) -> Optional[int]:
    return rp.safe_int(v)


def _parse_data(e: dict) -> Any:
    return rp.parse_embedded_json(e.get("Data")) if "Data" in e else e


def _flatten_numeric_array(value: Any) -> list[int]:
    """Flatten HeroesDecode's compact integer array representation.

    HeroesDecode may represent m_items as a list containing one {"0": v0, ...}
    object. Native decoder output may already be a list of ints.
    """
    if isinstance(value, dict):
        pairs = []
        for k, v in value.items():
            try:
                ki = int(k)
            except (TypeError, ValueError):
                continue
            vi = _to_int(v)
            if vi is not None:
                pairs.append((ki, vi))
        return [v for _, v in sorted(pairs)]
    if isinstance(value, (list, tuple)):
        out: list[int] = []
        for item in value:
            if isinstance(item, dict):
                out.extend(_flatten_numeric_array(item))
            else:
                vi = _to_int(item)
                if vi is not None:
                    out.append(vi)
        return out
    return []


def _point_from_fixed(value: Any) -> Optional[dict]:
    value = rp.parse_embedded_json(value)
    if not isinstance(value, (dict, list, tuple)):
        return None
    x = _to_int(_idx(value, 0) if _idx(value, 0) is not None else (value.get("x") if isinstance(value, dict) else None))
    y = _to_int(_idx(value, 1) if _idx(value, 1) is not None else (value.get("y") if isinstance(value, dict) else None))
    z = _to_int(_idx(value, 2) if _idx(value, 2) is not None else (value.get("z") if isinstance(value, dict) else None))
    if x is None or y is None:
        return None
    return {
        "x": x / FIXED_POINT_SCALE,
        "y": y / FIXED_POINT_SCALE,
        "z": (z / FIXED_POINT_SCALE) if z is not None else None,
        "raw": {"x": x, "y": y, "z": z},
    }


def parse_target_point_update(e: dict) -> Optional[dict]:
    if _event_type(e) != "sCmdUpdateTargetPointEvent":
        return None
    t = _event_time(e); pid = _event_player(e)
    data = _parse_data(e)
    if t is None or pid is None or not isinstance(data, (dict, list, tuple)):
        return None
    seq = _to_int(_idx(data, 0))
    point = _point_from_fixed(_idx(data, 1))
    if not point:
        return None
    return {"time": t, "player_id": pid, "kind": "TARGET_POINT_UPDATE", "sequence": seq, **point,
            "confidence": "HIGH_AS_INPUT_INTENT", "source": "sCmdUpdateTargetPointEvent"}


def parse_target_unit_update(e: dict) -> Optional[dict]:
    if _event_type(e) != "sCmdUpdateTargetUnitEvent":
        return None
    t = _event_time(e); pid = _event_player(e)
    data = _parse_data(e)
    if t is None or pid is None or not isinstance(data, (dict, list, tuple)):
        return None
    seq = _to_int(_idx(data, 0)); target = _idx(data, 1)
    if not isinstance(target, (dict, list, tuple)):
        return None
    snap = _point_from_fixed(_idx(target, 6))
    rec = {
        "time": t, "player_id": pid, "kind": "TARGET_UNIT_UPDATE", "sequence": seq,
        "target_tag": _to_int(_idx(target, 2)),
        "snapshot_unit_link": _to_int(_idx(target, 3)),
        "snapshot_control_player_id": _to_int(_idx(target, 4)),
        "snapshot_upkeep_player_id": _to_int(_idx(target, 5)),
        "confidence": "HIGH_AS_INPUT_INTENT", "source": "sCmdUpdateTargetUnitEvent",
    }
    if snap:
        rec["snapshot_point"] = {k: snap[k] for k in ("x", "y", "z", "raw")}
    return rec


def parse_point_order(e: dict) -> Optional[dict]:
    """Extract direct point-shaped SCmdEvent without naming it MOVE/ATTACK.

    HeroesDecode compact JSON distinguishes point-shaped command data structurally:
    Data[1] has x/y/z fixed-point fields. We intentionally call it POINT_ORDER,
    because command semantics (move, smart, attack-move, etc.) are a later layer.
    """
    if _event_type(e) != "sCmdEvent":
        return None
    t = _event_time(e); pid = _event_player(e); data = _parse_data(e)
    if t is None or pid is None or not isinstance(data, (dict, list, tuple)):
        return None
    maybe_point = _idx(data, 1)
    vals = _flatten_numeric_array(maybe_point)
    if len(vals) != 3:
        return None
    # Ability structures are normally small (abilLink/cmdIndex[/data]); point x/y are
    # fixed-point map coordinates and therefore much larger. Fail closed on ambiguity.
    if abs(vals[0]) < 4096 and abs(vals[1]) < 4096:
        return None
    point = _point_from_fixed(maybe_point)
    if not point:
        return None
    seq = _to_int(_idx(data, 2))
    if seq is None:
        seq = _to_int(_idx(data, 3))
    return {
        "time": t, "player_id": pid, "kind": "POINT_ORDER", "sequence": seq,
        "cmd_flags": _to_int(_idx(data, 0)), **point,
        "confidence": "HIGH_AS_COMMAND_TARGET", "source": "sCmdEvent",
    }


def parse_talent_selection_event(e: dict) -> Optional[dict]:
    if _event_type(e) != "sHeroTalentTreeSelectedEvent":
        return None
    t = _event_time(e); pid = _event_player(e); data = _parse_data(e)
    if t is None or pid is None:
        return None
    index = _to_int(_idx(data, 0) if isinstance(data, (dict, list, tuple)) else None)
    return {"time": t, "player_id": pid, "kind": "TALENT_TREE_SELECTION", "selection_index": index,
            "source": "sHeroTalentTreeSelectedEvent", "confidence": "HIGH_EVENT_LOW_IDENTITY"}



def _stat_named_rows(value: Any) -> dict[str, Any]:
    """Decode HeroesDecode/native SStatGameEvent named key/value rows.

    HeroesDecode serializes each logical named field with an extra wrapper layer. A
    PlayerInit string row, for example, may look structurally like::

        [{"0": {"0": "ToonHandle"}, "1": "2-Hero-..."},
         {"0": {"0": "PlayerHandle"}, "1": "..."}]

    Native/other decoded JSON can instead expose direct ``[key, value]`` pairs.
    Parse both forms, and parse every pair in a row rather than only row[0].
    """
    out: dict[str, Any] = {}
    if not isinstance(value, (list, tuple)):
        return out

    def ordered_values(obj: Any) -> list[Any]:
        if isinstance(obj, (list, tuple)):
            return list(obj)
        if isinstance(obj, dict):
            numeric = []
            for k, v in obj.items():
                try:
                    numeric.append((int(k), v))
                except (TypeError, ValueError):
                    continue
            if numeric:
                return [v for _, v in sorted(numeric)]
        return []

    def unwrap_key(obj: Any) -> Any:
        cur = obj
        # HeroesDecode wraps the key one or occasionally two levels deep.
        for _ in range(3):
            if isinstance(cur, (dict, list, tuple)):
                nxt = _idx(cur, 0)
                if nxt is None or nxt is cur:
                    break
                cur = nxt
            else:
                break
        return cur

    def consume_pair(pair: Any) -> bool:
        if not isinstance(pair, (dict, list, tuple)):
            return False
        key_box = _idx(pair, 0)
        val = _idx(pair, 1)
        key = unwrap_key(key_box)
        if isinstance(key, (str, int)) and val is not None:
            out[str(key)] = val
            return True
        return False

    for row in value:
        # Direct pair form: [key, value] or {0:key, 1:value}.
        if consume_pair(row):
            key_candidate = unwrap_key(_idx(row, 0)) if isinstance(row, (dict, list, tuple)) else None
            # If row[0] is itself a pair wrapper (HeroesDecode), consume all sibling pairs too.
            if isinstance(_idx(row, 0), (dict, list, tuple)) and not isinstance(key_candidate, (str, int)):
                pass
        # HeroesDecode common form: one row contains several wrapped pairs.
        for pair in ordered_values(row):
            consume_pair(pair)
    return out


def parse_stat_game_event(e: dict) -> Optional[dict]:
    if _event_type(e) != "statGameEvent":
        return None
    t = _event_time(e); data = _parse_data(e)
    if t is None or not isinstance(data, (dict, list, tuple)):
        return None
    name = _idx(data, 0)
    if name is None:
        return None
    return {
        "time": t, "name": str(name),
        "strings": _stat_named_rows(_idx(data, 1)),
        "ints": _stat_named_rows(_idx(data, 2)),
        "fixed": _stat_named_rows(_idx(data, 3)),
    }

def build_tracker_player_map(replay: dict, players: dict[str, dict]) -> tuple[dict[int, str], list[dict]]:
    mapping: dict[int, str] = {}
    evidence: list[dict] = []
    known_toon = set(players)
    for e in replay.get("TrackerEvents", []) or []:
        if not isinstance(e, dict):
            continue
        st = parse_stat_game_event(e)
        if not st or st["name"] != "PlayerInit":
            continue
        pid = _to_int(st["ints"].get("PlayerID"))
        toon = st["strings"].get("ToonHandle")
        if pid is None or toon is None:
            continue
        toon = str(toon)
        confidence = "HIGH" if toon in known_toon else "MEDIUM_TOON_NOT_IN_PLAYERS"
        if pid not in mapping or mapping[pid] == toon:
            mapping[pid] = toon
        evidence.append({"time": st["time"], "tracker_player_id": pid, "player_id": toon, "source": "statGameEvent:PlayerInit", "confidence": confidence})
    return mapping, evidence


def _fixed_value(v: Any) -> Optional[float]:
    iv = _to_int(v)
    return (iv / FIXED_POINT_SCALE) if iv is not None else None

def tracker_tag(data: Any) -> Optional[tuple[int, int]]:
    a = _to_int(_idx(data, 0)); b = _to_int(_idx(data, 1))
    return (a, b) if a is not None and b is not None else None


def tag_text(tag: tuple[int, int]) -> str:
    return f"{tag[0]}:{tag[1]}"


def _canonical_players(players: dict[str, dict], schema: Optional[rp.AbilitySchemaIndex]) -> tuple[dict[str, dict], dict[str, list[str]]]:
    result = {}
    hero_to_pids: dict[str, list[str]] = defaultdict(list)
    for pid, p in players.items():
        hero = schema.canonical_hero(p.get("hero"), p.get("hero_id")) if schema else (p.get("hero") or p.get("hero_id"))
        row = {**p, "canonical_hero": hero}
        result[pid] = row
        if hero:
            hero_to_pids[str(hero)].append(pid)
    return result, hero_to_pids


def _owner_for_born(control_pid: Optional[int], unit_type: Any, players: dict[str, dict], hero_to_pids: dict[str, list[str]], schema: Optional[rp.AbilitySchemaIndex], tracker_player_map: dict[int, str]) -> tuple[Optional[str], str, bool]:
    canonical = schema.canonical_hero(unit_type) if schema else None
    mapped_pid = tracker_player_map.get(control_pid) if control_pid is not None else None
    if mapped_pid is not None and mapped_pid in players:
        pid = mapped_pid
        phero = players[pid].get("canonical_hero")
        if canonical and phero == canonical:
            return pid, "PLAYERINIT_MAP_AND_HERO_TYPE", True
        # A controlled summon is not the hero unit. Keep it out of position state.
        return None, "CONTROLLED_NONHERO_OR_UNKNOWN", False
    if canonical and len(hero_to_pids.get(canonical, [])) == 1:
        return hero_to_pids[canonical][0], "UNIQUE_HERO_TYPE", True
    return None, "UNRESOLVED", False


def build_tracker_state(replay: dict, players: dict[str, dict], schema: Optional[rp.AbilitySchemaIndex]) -> dict:
    players2, hero_to_pids = _canonical_players(players, schema)
    tracker_player_map, tracker_player_evidence = build_tracker_player_map(replay, players2)
    units: dict[tuple[int, int], dict] = {}
    hero_tags_by_index: dict[int, list[tuple[int, int]]] = defaultdict(list)
    hero_units_by_player: dict[str, list[dict]] = defaultdict(list)
    anchors: dict[str, list[dict]] = defaultdict(list)
    lifecycle: dict[str, list[dict]] = defaultdict(list)
    forms: dict[str, list[dict]] = defaultdict(list)
    ownership_diag: list[dict] = []
    ownership_reason_counts: dict[str, int] = defaultdict(int)
    hero_candidate_unresolved = 0

    events = [e for e in (replay.get("TrackerEvents", []) or []) if isinstance(e, dict)]
    events.sort(key=lambda e: (_event_time(e) if _event_time(e) is not None else float("inf")))

    def add_anchor(pid: str, t: float, x: float, y: float, source: str, confidence: str, unit_tag: Optional[tuple[int,int]]=None, approximate: bool=False):
        anchors[pid].append({"time": t, "x": float(x), "y": float(y), "source": source,
                             "confidence": confidence, "approximate": approximate,
                             **({"unit_tag": tag_text(unit_tag)} if unit_tag else {})})

    def active_hero_tag_for_index(index: int, t: float) -> Optional[tuple[int, int]]:
        candidates = []
        for tg in hero_tags_by_index.get(index, []):
            u = units.get(tg)
            if not u or not u.get("is_hero_unit"):
                continue
            born = u.get("born_time", -float("inf")); died = u.get("died_time")
            if t >= born and (died is None or t <= died + 1e-6):
                candidates.append(tg)
        return candidates[-1] if len(candidates) == 1 else None

    for e in events:
        typ = _event_type(e); t = _event_time(e); data = _parse_data(e)
        if t is None:
            continue
        if typ in {"unitBornEvent", "unitInitEvent"}:
            tg = tracker_tag(data); unit_type = _idx(data, 2)
            control = _to_int(_idx(data, 3)); upkeep = _to_int(_idx(data, 4))
            x = _to_int(_idx(data, 5)); y = _to_int(_idx(data, 6))
            if not tg:
                continue
            pid, owner_reason, is_hero = _owner_for_born(control, unit_type, players2, hero_to_pids, schema, tracker_player_map)
            ownership_reason_counts[str(owner_reason)] += 1
            canonical_candidate = schema.canonical_hero(unit_type) if schema else None
            if owner_reason == "UNRESOLVED" and canonical_candidate:
                hero_candidate_unresolved += 1
            rec = {
                "tag": tag_text(tg), "tag_index": tg[0], "tag_recycle": tg[1], "unit_type": unit_type,
                "control_player_id": control, "upkeep_player_id": upkeep, "born_time": t,
                "owner_player_id": pid, "owner_reason": owner_reason, "is_hero_unit": bool(is_hero),
                "died_time": None,
            }
            units[tg] = rec
            if is_hero and pid:
                hero_tags_by_index[tg[0]].append(tg)
                hero_units_by_player[pid].append(rec)
                lifecycle[pid].append({"time": t, "kind": "HERO_UNIT_BORN", "unit_tag": tag_text(tg), "unit_type": unit_type, "source": typ, "confidence": "HIGH"})
                if x is not None and y is not None:
                    add_anchor(pid, t, x, y, "TRACKER_UNIT_BORN", "HIGH", tg, False)
                forms[pid].append({"time": t, "unit_type": unit_type, "source": typ, "confidence": "HIGH"})
            elif control is not None and control in tracker_player_map:
                ownership_diag.append({"time": t, "tag": tag_text(tg), "unit_type": unit_type, "control_player_id": control, "player_id": tracker_player_map.get(control), "reason": owner_reason})

        elif typ == "unitDiedEvent":
            tg = tracker_tag(data)
            if not tg:
                continue
            u = units.get(tg)
            x = _to_int(_idx(data, 3)); y = _to_int(_idx(data, 4))
            if u:
                u["died_time"] = t
                pid = u.get("owner_player_id")
                if u.get("is_hero_unit") and pid:
                    lifecycle[pid].append({"time": t, "kind": "HERO_UNIT_DIED", "unit_tag": tag_text(tg), "unit_type": u.get("unit_type"), "source": typ, "confidence": "HIGH"})
                    if x is not None and y is not None:
                        add_anchor(pid, t, x, y, "TRACKER_UNIT_DIED", "HIGH", tg, False)

        elif typ == "unitRevivedEvent":
            tg = tracker_tag(data)
            if not tg:
                continue
            u = units.get(tg); x = _to_int(_idx(data, 2)); y = _to_int(_idx(data, 3))
            if u and u.get("is_hero_unit") and u.get("owner_player_id"):
                pid = u["owner_player_id"]; u["died_time"] = None
                lifecycle[pid].append({"time": t, "kind": "HERO_UNIT_REVIVED", "unit_tag": tag_text(tg), "unit_type": u.get("unit_type"), "source": typ, "confidence": "HIGH"})
                if x is not None and y is not None:
                    add_anchor(pid, t, x, y, "TRACKER_UNIT_REVIVED", "HIGH", tg, False)

        elif typ == "unitTypeChangeEvent":
            tg = tracker_tag(data); new_type = _idx(data, 2)
            if not tg or new_type is None:
                continue
            u = units.get(tg)
            if u:
                old_type = u.get("unit_type"); u["unit_type"] = new_type
                if u.get("is_hero_unit") and u.get("owner_player_id"):
                    pid = u["owner_player_id"]
                    forms[pid].append({"time": t, "unit_type": new_type, "previous_unit_type": old_type, "source": typ, "confidence": "HIGH_SAME_UNIT_TAG"})
                    lifecycle[pid].append({"time": t, "kind": "HERO_UNIT_TYPE_CHANGE", "unit_tag": tag_text(tg), "unit_type": new_type, "previous_unit_type": old_type, "source": typ, "confidence": "HIGH"})

        elif typ == "unitOwnerChangeEvent":
            tg = tracker_tag(data); control = _to_int(_idx(data, 2)); upkeep = _to_int(_idx(data, 3))
            if not tg:
                continue
            u = units.get(tg)
            if u:
                u["control_player_id"] = control; u["upkeep_player_id"] = upkeep
                if u.get("is_hero_unit") and u.get("owner_player_id"):
                    pid = u["owner_player_id"]
                    lifecycle[pid].append({"time": t, "kind": "HERO_UNIT_OWNER_CHANGE", "unit_tag": tag_text(tg), "control_player_id": control, "upkeep_player_id": upkeep, "source": typ, "confidence": "HIGH"})

        elif typ == "unitPositionsEvent":
            first = _to_int(_idx(data, 0)); items = _flatten_numeric_array(_idx(data, 1))
            if first is None or len(items) < 3:
                continue
            unit_index = first
            for i in range(0, len(items) - 2, 3):
                unit_index += items[i]
                # HeroesDecode's SUnitPositionsEvent m_items coordinates are already in
                # the same map-coordinate scale as unitBorn/unitDied. Do NOT multiply
                # them by four. v0.1-v0.1.2 did so and produced a systematic 4x scale
                # error that was caught by cross-source replay evidence.
                x = items[i + 1]
                y = items[i + 2]
                tg = active_hero_tag_for_index(unit_index, t)
                if tg:
                    u = units[tg]; pid = u.get("owner_player_id")
                    if pid:
                        add_anchor(pid, t, x, y, "TRACKER_PERIODIC_POSITION", "MEDIUM_APPROX", tg, True)

    # Stat-game positions provide an independent player-id anchored position source.
    stat_player_events: list[dict] = []
    for e in replay.get("TrackerEvents", []) or []:
        if not isinstance(e, dict):
            continue
        st = parse_stat_game_event(e)
        if not st or st["name"] not in {"PlayerSpawned", "PlayerDeath"}:
            continue
        tracker_pid = _to_int(st["ints"].get("PlayerID"))
        pid = tracker_player_map.get(tracker_pid) if tracker_pid is not None else None
        if not pid or pid not in players2:
            continue
        x = _fixed_value(st["fixed"].get("PositionX")); y = _fixed_value(st["fixed"].get("PositionY"))
        rec = {"time": st["time"], "player_id": pid, "kind": st["name"], "source": "statGameEvent", "confidence": "HIGH"}
        if x is not None and y is not None:
            rec.update({"x": x, "y": y})
            add_anchor(pid, st["time"], x, y, f"STAT_{st['name'].upper()}", "HIGH", None, False)
        stat_player_events.append(rec)

    for pid in players2:
        for rows in (anchors[pid], lifecycle[pid], forms[pid]):
            rows.sort(key=lambda r: r.get("time", 0.0))
    diag_counts: dict[str, int] = defaultdict(int)
    for d in ownership_diag:
        key = f"{d.get('player_id')}|{d.get('unit_type')}|{d.get('reason')}"
        diag_counts[key] += 1
    diag_samples = ownership_diag[:100]
    return {
        "players": players2,
        "hero_units_by_player": dict(hero_units_by_player),
        "position_anchors": dict(anchors),
        "lifecycle": dict(lifecycle),
        "form_timeline": dict(forms),
        "ownership_diagnostics": diag_samples,
        "ownership_diagnostic_total": len(ownership_diag),
        "ownership_diagnostic_counts": dict(diag_counts),
        "ownership_reason_counts": dict(ownership_reason_counts),
        "hero_candidate_unresolved": hero_candidate_unresolved,
        "tracker_player_map": tracker_player_map,
        "tracker_player_evidence": tracker_player_evidence,
        "stat_player_events": stat_player_events,
    }


def compact_point_intent(rows: list[dict], min_distance: float = 0.75, max_interval: float = 0.50) -> list[dict]:
    if len(rows) <= 2:
        return rows
    out = [rows[0]]
    last = rows[0]
    for row in rows[1:-1]:
        dt = float(row.get("time", 0.0)) - float(last.get("time", 0.0))
        dist = math.hypot(float(row.get("x", 0.0))-float(last.get("x", 0.0)), float(row.get("y", 0.0))-float(last.get("y", 0.0)))
        # Preserve target changes and periodic samples, but not every high-frequency mouse update.
        if dist >= min_distance or dt >= max_interval:
            out.append(row); last = row
    if rows[-1] is not out[-1]:
        out.append(rows[-1])
    return out

def build_input_state(replay: dict, players: dict[str, dict], detail: str = "compact") -> dict:
    point_updates: dict[str, list[dict]] = defaultdict(list)
    point_orders: dict[str, list[dict]] = defaultdict(list)
    unit_updates: dict[str, list[dict]] = defaultdict(list)
    talent_events: dict[str, list[dict]] = defaultdict(list)
    for e in replay.get("GameEvents", []) or []:
        if not isinstance(e, dict):
            continue
        typ = _event_type(e)
        rec = None
        if typ == "sCmdUpdateTargetPointEvent":
            rec = parse_target_point_update(e)
            if rec: point_updates[rec["player_id"]].append(rec)
        elif typ == "sCmdUpdateTargetUnitEvent":
            rec = parse_target_unit_update(e)
            if rec: unit_updates[rec["player_id"]].append(rec)
        elif typ == "sCmdEvent":
            rec = parse_point_order(e)
            if rec: point_orders[rec["player_id"]].append(rec)
        elif typ == "sHeroTalentTreeSelectedEvent":
            rec = parse_talent_selection_event(e)
            if rec: talent_events[rec["player_id"]].append(rec)
    for d in (point_updates, point_orders, unit_updates, talent_events):
        for rows in d.values(): rows.sort(key=lambda r: r.get("time", 0.0))
    raw_counts = {
        "target_point_updates": {pid: len(rows) for pid, rows in point_updates.items()},
        "point_orders": {pid: len(rows) for pid, rows in point_orders.items()},
        "target_unit_updates": {pid: len(rows) for pid, rows in unit_updates.items()},
    }
    if detail == "none":
        point_updates = defaultdict(list); point_orders = defaultdict(list); unit_updates = defaultdict(list)
    elif detail == "compact":
        point_updates = defaultdict(list, {pid: compact_point_intent(rows) for pid, rows in point_updates.items()})
        point_orders = defaultdict(list, {pid: compact_point_intent(rows) for pid, rows in point_orders.items()})
    return {
        "target_point_updates": dict(point_updates),
        "point_orders": dict(point_orders),
        "target_unit_updates": dict(unit_updates),
        "talent_selection_events": dict(talent_events),
        "raw_counts": raw_counts,
        "detail": detail,
    }


def _talent_state(players: dict[str, dict], raw_events: dict[str, list[dict]]) -> dict[str, dict]:
    """Keep canonical talent identity separate from low-identity replay evidence.

    Players.HeroTalents provides the canonical selected TalentNameId/slot.
    sHeroTalentTreeSelectedEvent proves that a talent-tree selection happened, but its
    numeric selection index is not a canonical talent id. Older World State versions
    mixed both into one list, doubling the apparent talent record count.
    """
    out: dict[str, dict] = {}
    for pid, p in players.items():
        selected = []
        seen = set()
        for t in p.get("talents", []) or []:
            talent_id = t.get("talent_id")
            slot_id = t.get("slot_id")
            key = (str(talent_id) if talent_id is not None else None, slot_id)
            if key in seen:
                continue
            seen.add(key)
            selected.append({
                "time": t.get("time"),
                "talent_id": talent_id,
                "slot_id": slot_id,
                "source": "Players.HeroTalents",
                "confidence": "HIGH_IDENTITY" if talent_id else "MEDIUM",
            })
        selected.sort(key=lambda r: (float(r.get("time")) if r.get("time") is not None else float("inf")))
        evidence = list(raw_events.get(pid, []))
        evidence.sort(key=lambda r: (float(r.get("time")) if r.get("time") is not None else float("inf")))
        out[pid] = {
            "selected": selected,
            "evidence": evidence,
            "selected_count": len(selected),
            "evidence_count": len(evidence),
        }
    return out


def _position_scale_integrity(players: dict[str, dict], max_dt: float = 2.0) -> dict:
    """Cross-check periodic tracker coordinates against independent exact anchors.

    We compare candidate coordinate scales 1.0, 0.25, and 4.0. The comparison is
    intentionally empirical and robust (median error): movement between nearby samples
    creates noise, but a systematic 4x coordinate bug dominates that noise by orders of
    magnitude.
    """
    exact_sources = {
        "TRACKER_UNIT_BORN", "TRACKER_UNIT_DIED", "TRACKER_UNIT_REVIVED",
        "STAT_PLAYERSPAWNED", "STAT_PLAYERDEATH",
    }
    candidate_scales = (1.0, 0.25, 4.0)
    errors: dict[float, list[float]] = {k: [] for k in candidate_scales}
    pairs = 0
    for p in players.values():
        anchors = p.get("position_anchors", []) or []
        periodic = [a for a in anchors if a.get("source") == "TRACKER_PERIODIC_POSITION"]
        exact = [a for a in anchors if a.get("source") in exact_sources]
        if not periodic or not exact:
            continue
        for e in exact:
            et = e.get("time")
            if et is None:
                continue
            q = min(periodic, key=lambda a: abs(float(a.get("time", 0.0)) - float(et)))
            dt = abs(float(q.get("time", 0.0)) - float(et))
            if dt > max_dt:
                continue
            try:
                ex, ey = float(e["x"]), float(e["y"])
                qx, qy = float(q["x"]), float(q["y"])
            except (KeyError, TypeError, ValueError):
                continue
            for scale in candidate_scales:
                errors[scale].append(math.hypot(qx * scale - ex, qy * scale - ey))
            pairs += 1

    def median(xs: list[float]) -> Optional[float]:
        if not xs:
            return None
        ys = sorted(xs)
        n = len(ys)
        return ys[n // 2] if n % 2 else (ys[n // 2 - 1] + ys[n // 2]) / 2.0

    medians = {str(k): median(v) for k, v in errors.items()}
    usable = {k: v for k, v in ((k, median(errors[k])) for k in candidate_scales) if v is not None}
    best_scale = min(usable, key=usable.get) if usable else None
    if pairs < 3:
        status = "INSUFFICIENT_EVIDENCE"
    elif best_scale == 1.0:
        status = "PASS"
    else:
        status = "FAIL_SCALE_MISMATCH"
    return {
        "status": status,
        "matched_cross_source_pairs": pairs,
        "max_time_delta_seconds": max_dt,
        "candidate_median_errors": medians,
        "best_scale": best_scale,
    }


def _alive_intervals(events: list[dict]) -> list[dict]:
    intervals = []
    alive_since = None
    for e in sorted(events, key=lambda r: r.get("time", 0.0)):
        k = e.get("kind"); t = e.get("time")
        if k in {"HERO_UNIT_BORN", "HERO_UNIT_REVIVED"}:
            if alive_since is None: alive_since = t
        elif k == "HERO_UNIT_DIED" and alive_since is not None:
            intervals.append({"start": alive_since, "end": t, "state": "ALIVE", "confidence": "HIGH"})
            alive_since = None
    if alive_since is not None:
        intervals.append({"start": alive_since, "end": None, "state": "ALIVE", "confidence": "HIGH"})
    return intervals


def _position_gaps(anchors: list[dict]) -> list[dict]:
    gaps = []
    for a, b in zip(anchors, anchors[1:]):
        dt = float(b["time"]) - float(a["time"])
        if dt <= 0: continue
        dist = math.hypot(float(b["x"])-float(a["x"]), float(b["y"])-float(a["y"]))
        gaps.append({"start": a["time"], "end": b["time"], "dt": dt, "distance": dist,
                     "required_average_speed": dist/dt,
                     "interpolation": "NOT_ASSUMED",
                     "reason": "movement/teleport/vehicle semantics not yet evaluated"})
    return gaps


def _map_metadata(replay: dict) -> dict:
    result = {"map_name": replay.get("__map_name__"), "map_id": replay.get("__map_id__")}
    for key in ("Map", "MapName", "map", "map_name"):
        if result["map_name"] is None and replay.get(key): result["map_name"] = replay.get(key); break
    details = replay.get("Details")
    if isinstance(details, dict):
        for key in ("m_title", "Map", "MapName"):
            if details.get(key): result["map_name"] = details.get(key); break
    # Stat GameStart commonly carries MapID in HeroesDecode output.
    for e in replay.get("TrackerEvents", []) or []:
        if not isinstance(e, dict) or _event_type(e) != "statGameEvent": continue
        data = _parse_data(e)
        if _idx(data, 0) != "GameStart": continue
        text_rows = _idx(data, 1)
        if isinstance(text_rows, list):
            for row in text_rows:
                if not isinstance(row, (dict,list,tuple)): continue
                pair = _idx(row,0)
                if isinstance(pair,(dict,list,tuple)) and _idx(pair,0)=="MapID":
                    result["map_id"] = _idx(pair,1)
                    break
    return result


def build_world_state(replay: dict, source_name: str, schema: Optional[rp.AbilitySchemaIndex], movement_detail: str = "compact") -> dict:
    players = rp.replay_players(replay)
    tracker = build_tracker_state(replay, players, schema)
    inputs = build_input_state(replay, players, movement_detail)
    talents = _talent_state(tracker["players"], inputs["talent_selection_events"])
    build = rp.extract_replay_build(replay)
    explicit_build = _to_int(replay.get("__base_build__"))
    build_value = explicit_build if explicit_build is not None else build.get("build")
    build_status = "OBSERVED_DECODER_METADATA" if explicit_build is not None else build.get("status")
    meta = {
        "source": source_name,
        "source_format": replay.get("__source_format__") or "DECODED_JSON",
        "build": build_value, "build_status": build_status,
        **_map_metadata(replay),
    }
    out_players = {}
    for pid, p in tracker["players"].items():
        anchors = tracker["position_anchors"].get(pid, [])
        lifecycle = tracker["lifecycle"].get(pid, [])
        out_players[pid] = {
            "identity": {
                "player_id": pid,
                "name": p.get("name"),
                "hero": p.get("canonical_hero") or p.get("hero"),
                "hero_display_name": p.get("hero"),
                "hero_unit_id": p.get("hero_unit_id") or p.get("hero_id"),
                "hero_attribute_id": p.get("hero_attribute_id"),
                "raw_hero_link_id": p.get("hero_link_id"),
                "team": p.get("team"),
            },
            "talents": talents.get(pid, {"selected": [], "evidence": [], "selected_count": 0, "evidence_count": 0}),
            "hero_units": tracker["hero_units_by_player"].get(pid, []),
            "lifecycle": lifecycle,
            "alive_intervals": _alive_intervals(lifecycle),
            "form_timeline": tracker["form_timeline"].get(pid, []),
            "position_anchors": anchors,
            "position_gaps": _position_gaps(anchors),
            "movement_intent": {
                "detail": inputs["detail"],
                "point_orders": inputs["point_orders"].get(pid, []),
                "target_point_updates": inputs["target_point_updates"].get(pid, []),
                "target_unit_updates": inputs["target_unit_updates"].get(pid, []),
                "raw_counts": {k: v.get(pid, 0) for k, v in inputs["raw_counts"].items()},
            },
        }
    position_integrity = _position_scale_integrity(out_players)
    # Aggregate born-unit ownership reasons without storing every unit diagnostic row.
    ownership_reason_counts = dict(tracker.get("ownership_reason_counts", {}))
    hero_candidate_unresolved = int(tracker.get("hero_candidate_unresolved", 0))

    return {
        "schema": {"name": "PYHOTS_WORLD_STATE", "version": VERSION},
        "metadata": meta,
        "players": out_players,
        "integrity": {
            "position_scale": position_integrity,
            "player_identity": {
                "players": len(out_players),
                "tracker_player_mappings": len(tracker.get("tracker_player_map", {})),
                "players_with_hero_unit": sum(bool(p.get("hero_units")) for p in out_players.values()),
                "players_with_position_anchors": sum(bool(p.get("position_anchors")) for p in out_players.values()),
                "hero_candidate_unresolved": hero_candidate_unresolved,
            },
            "talents": {
                "selected": sum(int(p.get("talents", {}).get("selected_count", 0)) for p in out_players.values()),
                "evidence": sum(int(p.get("talents", {}).get("evidence_count", 0)) for p in out_players.values()),
                "players_over_seven_selected": sum(int(p.get("talents", {}).get("selected_count", 0)) > 7 for p in out_players.values()),
            },
            "build_provenance": {
                "build": build_value,
                "status": "PASS" if build_value is not None else "FAIL_UNKNOWN_BUILD",
            },
        },
        "diagnostics": {
            "ownership": tracker["ownership_diagnostics"],
            "ownership_total": tracker["ownership_diagnostic_total"],
            "ownership_counts": tracker["ownership_diagnostic_counts"],
            "ownership_reason_counts": tracker.get("ownership_reason_counts", {}),
            "hero_candidate_unresolved": tracker.get("hero_candidate_unresolved", 0),
            "tracker_player_map": tracker["tracker_player_map"],
            "tracker_player_evidence": tracker["tracker_player_evidence"],
            "stat_player_events": tracker["stat_player_events"],
        },
    }


def summarize_world_state(state: dict) -> dict:
    players = state.get("players", {})
    return {
        "source": state.get("metadata", {}).get("source"),
        "build": state.get("metadata", {}).get("build"),
        "map_id": state.get("metadata", {}).get("map_id"),
        "players": len(players),
        "players_with_hero_unit": sum(bool(p.get("hero_units")) for p in players.values()),
        "players_with_position_anchors": sum(bool(p.get("position_anchors")) for p in players.values()),
        "position_anchors": sum(len(p.get("position_anchors", [])) for p in players.values()),
        "point_orders": sum(len(p.get("movement_intent", {}).get("point_orders", [])) for p in players.values()),
        "target_point_updates": sum(len(p.get("movement_intent", {}).get("target_point_updates", [])) for p in players.values()),
        "target_unit_updates": sum(len(p.get("movement_intent", {}).get("target_unit_updates", [])) for p in players.values()),
        "raw_point_orders": sum(p.get("movement_intent", {}).get("raw_counts", {}).get("point_orders", 0) for p in players.values()),
        "raw_target_point_updates": sum(p.get("movement_intent", {}).get("raw_counts", {}).get("target_point_updates", 0) for p in players.values()),
        "raw_target_unit_updates": sum(p.get("movement_intent", {}).get("raw_counts", {}).get("target_unit_updates", 0) for p in players.values()),
        "talent_picks": sum(int(p.get("talents", {}).get("selected_count", 0)) for p in players.values()),
        "talent_evidence_events": sum(int(p.get("talents", {}).get("evidence_count", 0)) for p in players.values()),
        "form_events": sum(len(p.get("form_timeline", [])) for p in players.values()),
        "ownership_diagnostics": int(state.get("diagnostics", {}).get("ownership_total", len(state.get("diagnostics", {}).get("ownership", [])))),
        "hero_candidate_unresolved": int(state.get("diagnostics", {}).get("hero_candidate_unresolved", 0)),
        "position_scale_status": state.get("integrity", {}).get("position_scale", {}).get("status"),
        "position_scale_pairs": int(state.get("integrity", {}).get("position_scale", {}).get("matched_cross_source_pairs", 0)),
        "position_scale_best": state.get("integrity", {}).get("position_scale", {}).get("best_scale"),
        "players_over_seven_selected_talents": int(state.get("integrity", {}).get("talents", {}).get("players_over_seven_selected", 0)),
    }


def _replay_scope(summary: dict) -> tuple[str, Optional[str]]:
    """Classify replay for strict World-State integrity accounting.

    Core DB gates are intentionally scoped to ten-player replay states. Replays with a
    different player count are preserved and reported, but quarantined rather than
    silently forcing hero/map/position gates to fail. This is evidence-based and does
    not inspect filenames or map names.
    """
    players = int(summary.get("players", 0) or 0)
    if players == 10:
        return "CORE_10_PLAYER", None
    return "QUARANTINED", "NONSTANDARD_PLAYER_COUNT"


def _sum_summary_totals(items: list[dict]) -> dict:
    return {
        "players":sum(x["players"] for x in items),
        "players_with_hero_unit":sum(x["players_with_hero_unit"] for x in items),
        "players_with_position_anchors":sum(x["players_with_position_anchors"] for x in items),
        "position_anchors":sum(x["position_anchors"] for x in items),
        "point_orders":sum(x["point_orders"] for x in items),
        "target_point_updates":sum(x["target_point_updates"] for x in items),
        "target_unit_updates":sum(x["target_unit_updates"] for x in items),
        "raw_point_orders":sum(x["raw_point_orders"] for x in items),
        "raw_target_point_updates":sum(x["raw_target_point_updates"] for x in items),
        "raw_target_unit_updates":sum(x["raw_target_unit_updates"] for x in items),
        "talent_picks":sum(x["talent_picks"] for x in items),
        "talent_evidence_events":sum(x["talent_evidence_events"] for x in items),
        "form_events":sum(x["form_events"] for x in items),
        "ownership_diagnostics":sum(x["ownership_diagnostics"] for x in items),
        "hero_candidate_unresolved":sum(x["hero_candidate_unresolved"] for x in items),
        "position_scale_pairs":sum(x["position_scale_pairs"] for x in items),
        "players_over_seven_selected_talents":sum(x["players_over_seven_selected_talents"] for x in items),
    }


def _fingerprint(path: Path, config_sig: str) -> str:
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{st.st_mtime_ns}|{config_sig}|{CACHE_SCHEMA}".encode()
    return hashlib.sha256(raw).hexdigest()[:24]


def _load_replay(path: Path, heroesdecode_command: list[str], timeout: float, keep_decoded_dir: Optional[Path]) -> dict:
    if path.suffix.lower() == ".stormreplay":
        return rp.decode_stormreplay_heroesdecode(path, heroesdecode_command, timeout=timeout, keep_decoded_dir=keep_decoded_dir)
    value = load_json(path)
    if not rp.is_decoded_replay_json(value):
        raise RuntimeError("NOT_DECODED_REPLAY_JSON")
    return value


_WS_SCHEMA: Optional[rp.AbilitySchemaIndex] = None
_WS_HD_CMD: list[str] = []
_WS_TIMEOUT: float = 180.0
_WS_KEEP_DIR: Optional[Path] = None
_WS_MOVEMENT_DETAIL: str = "compact"

def _init_worker(schema_path_s: Optional[str], hd_cmd: list[str], timeout: float, keep_dir_s: Optional[str], movement_detail: str) -> None:
    global _WS_SCHEMA, _WS_HD_CMD, _WS_TIMEOUT, _WS_KEEP_DIR, _WS_MOVEMENT_DETAIL
    _WS_SCHEMA = rp.AbilitySchemaIndex(load_json(Path(schema_path_s))) if schema_path_s else None
    _WS_HD_CMD = list(hd_cmd or [])
    _WS_TIMEOUT = float(timeout)
    _WS_KEEP_DIR = Path(keep_dir_s) if keep_dir_s else None
    _WS_MOVEMENT_DETAIL = movement_detail

def _worker(path_s: str) -> tuple[str, dict]:
    path = Path(path_s)
    replay = _load_replay(path, _WS_HD_CMD, _WS_TIMEOUT, _WS_KEEP_DIR)
    state = build_world_state(replay, path.name, _WS_SCHEMA, _WS_MOVEMENT_DETAIL)
    return path_s, state


def discover_inputs(explicit: list[str], dirs: list[str], recursive: bool, output_dir: Path) -> list[Path]:
    paths: set[Path] = set()
    for x in explicit:
        p = Path(x).expanduser().resolve()
        if p.is_file(): paths.add(p)
    for d in dirs:
        root = Path(d).expanduser().resolve()
        if not root.is_dir(): continue
        it = root.rglob("*") if recursive else root.glob("*")
        for p in it:
            if not p.is_file(): continue
            if output_dir in p.parents: continue
            if p.suffix.lower() in {".stormreplay", ".json"}: paths.add(p.resolve())
    return sorted(paths, key=lambda p: str(p).lower())


def main() -> int:
    ap = argparse.ArgumentParser(description="Pyhots World State Builder v0.1")
    ap.add_argument("--replay", action="append", default=[])
    ap.add_argument("--replay-dir", action="append", default=[])
    ap.add_argument("--no-recursive", action="store_true")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--abilities-complete", help="Optional Pyhots Ability DB for canonical hero-unit identity")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--heroesdecode-path")
    ap.add_argument("--heroesdecode-timeout", type=float, default=180.0)
    ap.add_argument("--keep-decoded-json", action="store_true")
    ap.add_argument("--movement-detail", choices=("compact","full","none"), default="compact", help="How much input target-point evidence to store. Default compact.")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    outdir = Path(args.output_dir).expanduser().resolve(); outdir.mkdir(parents=True, exist_ok=True)
    cache_dir = outdir / ".world_state_cache"; cache_dir.mkdir(exist_ok=True)
    replay_out = outdir / "replays"; replay_out.mkdir(exist_ok=True)
    keep_dir = outdir / "decoded_replays" if args.keep_decoded_json else None
    if keep_dir: keep_dir.mkdir(exist_ok=True)

    schema_path = None
    if args.abilities_complete:
        full = Path(args.abilities_complete).expanduser().resolve()
        if not full.exists(): raise SystemExit(f"Ability DB not found: {full}")
        sha = rp.file_sha256(full)
        compact = rp.compact_ability_schema(load_json(full), source_sha256=sha)
        schema_path = cache_dir / f"ability_identity_{sha[:16]}.json"
        if not schema_path.exists(): dump_json(schema_path, compact)

    inputs = discover_inputs(args.replay, args.replay_dir, not args.no_recursive, outdir)
    if not inputs:
        raise SystemExit("No .StormReplay or decoded replay JSON inputs found")
    need_hd = any(p.suffix.lower()==".stormreplay" for p in inputs)
    hd_cmd: list[str] = []
    hd_version = None
    if need_hd:
        hd_cmd = rp.resolve_heroesdecode_command(args.heroesdecode_path)
        ok, hd_version = rp.check_heroesdecode_command(hd_cmd)
        if not ok: raise SystemExit(f"HeroesDecode unavailable: {hd_version}")

    config_sig = hashlib.sha256(orjson.dumps({"v":CACHE_CONFIG_VERSION,"schema":str(schema_path),"heroesdecode":hd_version,"movement_detail":args.movement_detail})).hexdigest()
    cached = 0; todo = []; states: list[dict] = []; failures = []
    for p in inputs:
        key = _fingerprint(p, config_sig); cp = cache_dir / f"{key}.json"
        if cp.exists() and not args.force:
            try:
                state = load_json(cp); states.append(state); cached += 1; continue
            except Exception: pass
        todo.append((p, cp))

    print(f"Pyhots World State Builder v{VERSION}")
    print(f"Discovered: {len(inputs)} | cached: {cached} | to process: {len(todo)} | workers: {max(1,args.workers)}")
    if hd_version: print(f"HeroesDecode: {hd_version}")

    t0=time.time()
    tasks=[str(p) for p,_ in todo]
    path_to_cache={str(p):cp for p,cp in todo}
    if tasks:
        with cf.ProcessPoolExecutor(
            max_workers=max(1,args.workers),
            initializer=_init_worker,
            initargs=(str(schema_path) if schema_path else None, hd_cmd, args.heroesdecode_timeout, str(keep_dir) if keep_dir else None, args.movement_detail),
        ) as ex:
            futs={ex.submit(_worker,ps):ps for ps in tasks}
            for fut in cf.as_completed(futs):
                ps=futs[fut]
                try:
                    _, state=fut.result(); dump_json(path_to_cache[ps], state); states.append(state)
                except Exception as exc:
                    failures.append({"file":ps,"error":f"{type(exc).__name__}: {exc}"})

    # materialize compact per-replay outputs from cache/states
    summaries=[]
    for state in states:
        key=hashlib.sha256(str(state.get("metadata",{}).get("source")).encode()).hexdigest()[:16]
        dump_json(replay_out / f"{key}.world_state.json", state)
        summaries.append(summarize_world_state(state))

    for x in summaries:
        scope, reason = _replay_scope(x)
        x["replay_scope"] = scope
        if reason:
            x["quarantine_reason"] = reason

    core_summaries = [x for x in summaries if x.get("replay_scope") == "CORE_10_PLAYER"]
    quarantined = [x for x in summaries if x.get("replay_scope") == "QUARANTINED"]
    quarantine_reason_counts: dict[str, int] = defaultdict(int)
    for x in quarantined:
        quarantine_reason_counts[str(x.get("quarantine_reason") or "UNKNOWN")] += 1

    agg={
        "schema":{"name":"PYHOTS_WORLD_STATE_SUMMARY","version":VERSION},
        "replays_discovered":len(inputs), "replays_processed":len(states), "failed":len(failures), "cached":cached,
        "builds":{}, "maps":{}, "core_maps":{},
        "totals":_sum_summary_totals(summaries),
        "core_totals":_sum_summary_totals(core_summaries),
        "scope":{
            "core_replays":len(core_summaries),
            "quarantined_replays":len(quarantined),
            "quarantine_reason_counts":dict(quarantine_reason_counts),
        },
        "elapsed_seconds":round(time.time()-t0,3), "heroesdecode_version":hd_version,
    }
    for x in summaries:
        b=str(x.get("build")); agg["builds"][b]=agg["builds"].get(b,0)+1
        m=str(x.get("map_id") or "UNKNOWN"); agg["maps"][m]=agg["maps"].get(m,0)+1
    for x in core_summaries:
        m=str(x.get("map_id") or "UNKNOWN"); agg["core_maps"][m]=agg["core_maps"].get(m,0)+1

    position_status_counts: dict[str, int] = defaultdict(int)
    best_scale_counts: dict[str, int] = defaultdict(int)
    for x in core_summaries:
        position_status_counts[str(x.get("position_scale_status") or "UNKNOWN")] += 1
        best_scale_counts[str(x.get("position_scale_best"))] += 1
    ct = agg["core_totals"]
    gates = {
        "PROCESSING": "PASS" if not failures and len(states) == len(inputs) else "FAIL",
        "QUARANTINE_ACCOUNTING": "PASS" if len(core_summaries) + len(quarantined) == len(states) else "FAIL",
        "HERO_OWNERSHIP": "PASS" if ct["players"] > 0 and ct["players_with_hero_unit"] == ct["players"] and ct["hero_candidate_unresolved"] == 0 else "FAIL",
        "POSITION_COVERAGE": "PASS" if ct["players"] > 0 and ct["players_with_position_anchors"] == ct["players"] else "FAIL",
        "POSITION_SCALE": "PASS" if position_status_counts.get("FAIL_SCALE_MISMATCH", 0) == 0 and best_scale_counts.get("0.25", 0) == 0 and best_scale_counts.get("4.0", 0) == 0 and ct["position_scale_pairs"] >= max(3, len(core_summaries)//2) else "FAIL",
        "MAP_ID": "PASS" if agg["core_maps"].get("UNKNOWN", 0) == 0 else "FAIL",
        "BUILD_PROVENANCE": "PASS" if agg["builds"].get("None", 0) == 0 and agg["builds"].get("UNKNOWN", 0) == 0 else "FAIL",
        "TALENT_CANONICALIZATION": "PASS" if ct["players_over_seven_selected_talents"] == 0 else "FAIL",
    }
    agg["integrity"] = {
        "gates": gates,
        "world_state_integrity": "PASS" if all(v == "PASS" for v in gates.values()) else "FAIL",
        "scope": agg["scope"],
        "position_scale_status_counts": dict(position_status_counts),
        "position_scale_best_counts": dict(best_scale_counts),
    }
    dump_json(outdir/"summary.json",agg); dump_json(outdir/"replay_summaries.json",summaries); dump_json(outdir/"quarantined_replays.json",quarantined); dump_json(outdir/"failed_replays.json",failures); dump_json(outdir/"integrity_report.json",agg["integrity"])
    print(orjson.dumps(agg, option=orjson.OPT_INDENT_2).decode())
    return 0 if not failures else 2

if __name__ == "__main__":
    raise SystemExit(main())
