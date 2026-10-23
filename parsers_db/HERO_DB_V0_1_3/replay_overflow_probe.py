#!/usr/bin/env python3
"""Pyhots Hero Unit DB v0.2.1 extreme replay scaling/overflow evidence probe.

Purpose
-------
Extract replay-proven level/XP facts from a .StormReplay without HeroesDecode,
then compare replay level-up brackets against the canonical GameData HeroXPCurve.

This probe is intentionally fail-closed:
- it proves replay level progression and XP/stat counter behavior;
- it proves bracket alignment with GameData thresholds;
- it does NOT infer hero HP/damage/heal/shield arithmetic at level 31+;
- it does NOT call the post-30 offset mechanism a 20-bit wrap merely because 2^20
  lies inside the replay-constrained interval.

No third-party MPQ or s2protocol packages are required for this fixture.  The
minimal MPQ reader and Blizzard versioned-data decoder here cover the encodings
used by the validated build-91093 replay.
"""
from __future__ import annotations

import argparse
import bz2
import hashlib
import json
import struct
import zlib
from bisect import bisect_left
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

MASK32 = 0xFFFFFFFF
FIXED_SCALE = 4096
XP_COMPONENTS = ("MinionXP", "CreepXP", "StructureXP", "HeroXP", "TrickleXP")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _crypt_table() -> List[int]:
    out = [0] * 0x500
    seed = 0x00100001
    for index1 in range(0x100):
        index2 = index1
        for _ in range(5):
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp1 = (seed & 0xFFFF) << 16
            seed = (seed * 125 + 3) % 0x2AAAAB
            temp2 = seed & 0xFFFF
            out[index2] = (temp1 | temp2) & MASK32
            index2 += 0x100
    return out


CRYPT = _crypt_table()


def mpq_hash_string(value: str, hash_type: int) -> int:
    seed1 = 0x7FED7FED
    seed2 = 0xEEEEEEEE
    for c in value.encode("utf-8").upper():
        seed1 = (CRYPT[(hash_type << 8) + c] ^ ((seed1 + seed2) & MASK32)) & MASK32
        seed2 = (c + seed1 + seed2 + ((seed2 << 5) & MASK32) + 3) & MASK32
    return seed1


def mpq_decrypt(data: bytes, key: int) -> bytes:
    out = bytearray(data)
    seed1 = key & MASK32
    seed2 = 0xEEEEEEEE
    for i in range(len(data) // 4):
        seed2 = (seed2 + CRYPT[0x400 + (seed1 & 0xFF)]) & MASK32
        value = struct.unpack_from("<I", data, i * 4)[0]
        decoded = (value ^ ((seed1 + seed2) & MASK32)) & MASK32
        struct.pack_into("<I", out, i * 4, decoded)
        seed1 = (((((~seed1) & MASK32) << 21) + 0x11111111) | (seed1 >> 11)) & MASK32
        seed2 = (decoded + seed2 + ((seed2 << 5) & MASK32) + 3) & MASK32
    return bytes(out)


class MPQArchive:
    FILE_IMPLODE = 0x00000100
    FILE_COMPRESS = 0x00000200
    FILE_ENCRYPTED = 0x00010000
    FILE_SINGLE_UNIT = 0x01000000
    FILE_SECTOR_CRC = 0x04000000

    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        if self.data[:4] == b"MPQ\x1b":
            _, self.base, _ = struct.unpack_from("<III", self.data, 4)
        else:
            self.base = self.data.find(b"MPQ\x1a")
        if self.base < 0 or self.data[self.base:self.base + 4] != b"MPQ\x1a":
            raise ValueError("MPQ archive header not found")

        o = self.base
        (
            self.header_size,
            self.archive_size,
            self.version,
            self.block_shift,
            self.hash_pos,
            self.block_pos,
            self.hash_count,
            self.block_count,
        ) = struct.unpack_from("<IIHHIIII", self.data, o + 4)
        self.sector_size = 512 << self.block_shift

        hash_raw = self.data[o + self.hash_pos:o + self.hash_pos + self.hash_count * 16]
        block_raw = self.data[o + self.block_pos:o + self.block_pos + self.block_count * 16]
        hash_raw = mpq_decrypt(hash_raw, mpq_hash_string("(hash table)", 3))
        block_raw = mpq_decrypt(block_raw, mpq_hash_string("(block table)", 3))

        self.hash_table = [struct.unpack_from("<IIHHI", hash_raw, i * 16) for i in range(self.hash_count)]
        self.block_table = [struct.unpack_from("<IIII", block_raw, i * 16) for i in range(self.block_count)]

    def find(self, name: str) -> Optional[int]:
        start = mpq_hash_string(name, 0) % self.hash_count
        ha = mpq_hash_string(name, 1)
        hb = mpq_hash_string(name, 2)
        for k in range(self.hash_count):
            row = self.hash_table[(start + k) % self.hash_count]
            row_a, row_b, _locale, _platform, block_index = row
            if block_index == 0xFFFFFFFF:
                return None
            if block_index not in (0xFFFFFFFE, 0xFFFFFFFF) and row_a == ha and row_b == hb:
                return block_index
        return None

    @staticmethod
    def _decompress(chunk: bytes, expected: Optional[int] = None) -> bytes:
        if expected is not None and len(chunk) == expected:
            return chunk
        if not chunk:
            return b""
        mask = chunk[0]
        data = chunk[1:]
        if mask & 0x10:  # BZip2
            data = bz2.decompress(data)
            mask &= ~0x10
        if mask & 0x02:  # zlib
            data = zlib.decompress(data)
            mask &= ~0x02
        if mask:
            raise NotImplementedError(f"unsupported MPQ compression mask {mask:#x}")
        return data

    def extract(self, name: str) -> bytes:
        block_index = self.find(name)
        if block_index is None:
            raise KeyError(name)
        pos, compressed_size, file_size, flags = self.block_table[block_index]
        raw = self.data[self.base + pos:self.base + pos + compressed_size]
        if flags & self.FILE_ENCRYPTED:
            raise NotImplementedError("encrypted replay member is not supported by this fixture probe")
        if not (flags & (self.FILE_COMPRESS | self.FILE_IMPLODE)):
            return raw[:file_size]
        if flags & self.FILE_SINGLE_UNIT:
            return self._decompress(raw, file_size)[:file_size]

        sector_count = (file_size + self.sector_size - 1) // self.sector_size
        table_count = sector_count + 1 + (1 if flags & self.FILE_SECTOR_CRC else 0)
        offsets = list(struct.unpack_from("<" + "I" * table_count, raw, 0))
        out = bytearray()
        for i in range(sector_count):
            chunk = raw[offsets[i]:offsets[i + 1]]
            expected = min(self.sector_size, file_size - len(out))
            if len(chunk) < expected:
                chunk = self._decompress(chunk, expected)
            out.extend(chunk[:expected])
        return bytes(out[:file_size])


class VersionedReader:
    """Generic decoder for Blizzard's self-describing versioned data."""

    def __init__(self, data: bytes):
        self.data = data
        self.i = 0

    def u8(self) -> int:
        value = self.data[self.i]
        self.i += 1
        return value

    def take(self, n: int) -> bytes:
        value = self.data[self.i:self.i + n]
        if len(value) != n:
            raise EOFError("versioned data truncated")
        self.i += n
        return value

    def vint(self) -> int:
        b = self.u8()
        negative = b & 1
        value = (b >> 1) & 0x3F
        shift = 6
        while b & 0x80:
            b = self.u8()
            value |= (b & 0x7F) << shift
            shift += 7
        return -value if negative else value

    def parse(self) -> Any:
        t = self.u8()
        if t == 0x00:  # array
            return [self.parse() for _ in range(self.vint())]
        if t == 0x01:  # bitarray
            bits = self.vint()
            return {"_bitarray_bits": bits, "data": self.take((bits + 7) // 8).hex()}
        if t == 0x02:  # blob
            raw = self.take(self.vint())
            try:
                value = raw.decode("utf-8")
            except UnicodeDecodeError:
                value = raw.hex()
            return {"_blob": value}
        if t == 0x03:  # choice
            tag = self.vint()
            return {"_choice": tag, "value": self.parse()}
        if t == 0x04:  # optional
            return self.parse() if self.u8() else None
        if t == 0x05:  # struct
            result: Dict[str, Any] = {}
            for _ in range(self.vint()):
                tag = self.vint()
                result[str(tag)] = self.parse()
            return result
        if t == 0x06:
            return self.u8()
        if t == 0x07:
            return struct.unpack("<I", self.take(4))[0]
        if t == 0x08:
            return struct.unpack("<Q", self.take(8))[0]
        if t == 0x09:
            return self.vint()
        raise ValueError(f"unknown versioned type {t:#x} at offset {self.i - 1}")


def blob(value: Any) -> Optional[str]:
    return value.get("_blob") if isinstance(value, dict) else None


def stat_pairs(rows: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    if not rows:
        return result
    for row in rows:
        try:
            key = blob(row["0"]["0"])
            if key is not None:
                result[key] = row["1"]
        except (KeyError, TypeError):
            continue
    return result


def decode_header(replay_bytes: bytes) -> Dict[str, Any]:
    if replay_bytes[:4] != b"MPQ\x1b":
        raise ValueError("expected MPQ user-data header")
    _user_size, _mpq_offset, header_size = struct.unpack_from("<III", replay_bytes, 4)
    r = VersionedReader(replay_bytes[16:16 + header_size])
    value = r.parse()
    if r.i != header_size:
        raise ValueError("replay header did not decode exactly")
    return value


def decode_tracker_stream(data: bytes) -> List[Dict[str, Any]]:
    r = VersionedReader(data)
    game_loop = 0
    out = []
    while r.i < len(data):
        delta = r.parse()
        event_id = r.parse()
        payload = r.parse()
        if not isinstance(delta, dict) or "_choice" not in delta or not isinstance(delta.get("value"), int):
            raise ValueError(f"unexpected tracker delta at byte {r.i}")
        if not isinstance(event_id, int):
            raise ValueError(f"unexpected tracker event id at byte {r.i}")
        game_loop += delta["value"]
        out.append({"loop": game_loop, "event_id": event_id, "payload": payload})
    return out


def signed_i32(value: int) -> int:
    value &= MASK32
    return value - (1 << 32) if value >= (1 << 31) else value


def unsigned_i32(value: int) -> int:
    return value & MASK32


def fixed_signed(value: int) -> float:
    return signed_i32(value) / FIXED_SCALE


def fixed_unsigned_reconstructed(value: int) -> float:
    return unsigned_i32(value) / FIXED_SCALE


def hms(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    if h:
        return f"{h}:{m:02d}:{s:06.3f}"
    return f"{m}:{s:06.3f}"


def read_hero_xp_curve(compact_path: Path) -> Dict[str, Any]:
    data = json.loads(compact_path.read_text(encoding="utf-8"))
    records = data.get("objects", {}).get("HeroXPCurve") or []
    if len(records) != 1:
        raise ValueError(f"expected exactly one HeroXPCurve record, found {len(records)}")
    rec = records[0]
    slots = []
    for d in rec.get("descendants", []):
        if d.get("depth") != 1 or d.get("tag") != "VeterancyLevelArray":
            continue
        ordinal = d.get("same_tag_ordinal")
        attrs = d.get("attrs") or {}
        raw = attrs.get("MinVeterancyXP")
        slots.append({"slot_ordinal": ordinal, "min_veterancy_xp": None if raw is None else int(raw)})
    slots.sort(key=lambda x: x["slot_ordinal"])
    thresholds = [x["min_veterancy_xp"] for x in slots if x["min_veterancy_xp"] is not None]
    cumulative = []
    total = 0
    for value in thresholds:
        total += value
        cumulative.append(total)
    return {
        "object_id": "HeroXPCurve",
        "xml_type": rec.get("xml_type"),
        "slot_count": len(slots),
        "slots": slots,
        "transition_thresholds": thresholds,
        "cumulative_thresholds": cumulative,
        "cumulative_xp_to_display_level_30": total,
    }


def score_stat_values(score_payload: Dict[str, Any], stat_name: str) -> Optional[List[Optional[int]]]:
    for stat in score_payload.get("0") or []:
        if blob(stat.get("0")) != stat_name:
            continue
        values: List[Optional[int]] = []
        for player_series in stat.get("1") or []:
            if isinstance(player_series, list) and player_series:
                values.append(player_series[-1].get("0"))
            else:
                values.append(None)
        return values
    return None


def analyze(replay_path: Path, compact_path: Path) -> Dict[str, Any]:
    replay_bytes = replay_path.read_bytes()
    replay_hash = hashlib.sha256(replay_bytes).hexdigest()
    header = decode_header(replay_bytes)
    archive = MPQArchive(replay_path)
    tracker_data = archive.extract("replay.tracker.events")
    tracker_hash = hashlib.sha256(tracker_data).hexdigest()
    events = decode_tracker_stream(tracker_data)
    curve = read_hero_xp_curve(compact_path)

    version = header.get("1") or {}
    build_a = version.get("4")
    build_b = header.get("6")
    build = build_a if build_a == build_b else build_b
    elapsed_loops = int(header.get("3"))

    player_team: Dict[int, int] = {}
    player_hero: Dict[int, str] = {}
    levelups: List[Dict[str, Any]] = []
    periodic: List[Dict[str, Any]] = []
    scores: List[Dict[str, Any]] = []

    for e in events:
        if e["event_id"] == 10:  # SStatGameEvent in this build
            payload = e["payload"]
            event_name = blob(payload.get("0"))
            if event_name == "PlayerInit":
                ints = stat_pairs(payload.get("2"))
                if "PlayerID" in ints and "Team" in ints:
                    player_team[int(ints["PlayerID"])] = int(ints["Team"])
            elif event_name == "PlayerSpawned":
                strings = stat_pairs(payload.get("1"))
                ints = stat_pairs(payload.get("2"))
                if "PlayerID" in ints and "Hero" in strings:
                    hero_value = strings["Hero"]
                    player_hero[int(ints["PlayerID"])] = blob(hero_value) or str(hero_value)
            elif event_name == "LevelUp":
                ints = stat_pairs(payload.get("2"))
                pid = int(ints["PlayerID"])
                levelups.append({
                    "loop": e["loop"],
                    "seconds": e["loop"] / 16.0,
                    "time": hms(e["loop"] / 16.0),
                    "player_id": pid,
                    "team": player_team.get(pid),
                    "level": int(ints["Level"]),
                })
            elif event_name == "PeriodicXPBreakdown":
                ints = stat_pairs(payload.get("2"))
                fixed = stat_pairs(payload.get("3"))
                components = {}
                total_unsigned = 0.0
                total_signed = 0.0
                for key in XP_COMPONENTS:
                    raw = int(fixed.get(key, 0))
                    signed = fixed_signed(raw)
                    unsigned = fixed_unsigned_reconstructed(raw)
                    components[key] = {
                        "raw_serialized_integer": raw,
                        "signed_fixed": signed,
                        "u32_reconstructed_fixed": unsigned,
                    }
                    total_signed += signed
                    total_unsigned += unsigned
                periodic.append({
                    "loop": e["loop"],
                    "seconds": e["loop"] / 16.0,
                    "time": hms(e["loop"] / 16.0),
                    "team": int(ints["Team"]),
                    "team_level": int(ints["TeamLevel"]),
                    "xp_components": components,
                    "xp_total_signed_stat_view": total_signed,
                    "xp_total_u32_reconstructed": total_unsigned,
                })
        elif e["event_id"] == 11:  # SScoreResultEvent in this build
            scores.append(e)

    team_players: Dict[int, List[int]] = {}
    for pid, team in sorted(player_team.items()):
        team_players.setdefault(team, []).append(pid)

    team_level_summary = {}
    for team, pids in sorted(team_players.items()):
        team_levels = [x for x in levelups if x["player_id"] in pids]
        max_level = max((x["level"] for x in team_levels), default=1)
        by_level = {}
        for x in team_levels:
            by_level.setdefault(x["level"], []).append(x)
        synchronized = all(
            len({r["loop"] for r in rows}) == 1 and len(rows) == len(pids)
            for level, rows in by_level.items() if level >= 2
        )
        team_level_summary[str(team)] = {
            "players": pids,
            "max_level": max_level,
            "levelup_events_level_2_plus_synchronized_across_team": synchronized,
            "levelup_levels_observed": sorted(by_level),
        }

    # Team 1 is the extreme team in this fixture. Use one representative player
    # only after verifying synchronized team-level events.
    extreme_team = 1
    extreme_players = team_players[extreme_team]
    representative = extreme_players[0]
    rep_levels = [x for x in levelups if x["player_id"] == representative]
    team_periodic = [x for x in periodic if x["team"] == extreme_team]
    periodic_loops = [x["loop"] for x in team_periodic]

    cumulative = curve["cumulative_thresholds"]

    def bracket_for(loop: int) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        i = bisect_left(periodic_loops, loop)
        if i <= 0 or i >= len(team_periodic):
            raise ValueError(f"no enclosing periodic XP snapshots for loop {loop}")
        return team_periodic[i - 1], team_periodic[i]

    first_cycle = []
    first_offset_intervals = []
    for row in rep_levels:
        level = row["level"]
        if 2 <= level <= 30:
            expected = cumulative[level - 2]
            before, after = bracket_for(row["loop"])
            lo = before["xp_total_u32_reconstructed"] - expected
            hi = after["xp_total_u32_reconstructed"] - expected
            first_offset_intervals.append((lo, hi))
            first_cycle.append({
                "level": level,
                "loop": row["loop"],
                "time": row["time"],
                "gamedata_cumulative_threshold": expected,
                "xp_before": before["xp_total_u32_reconstructed"],
                "xp_after": after["xp_total_u32_reconstructed"],
                "threshold_inside_bracket": before["xp_total_u32_reconstructed"] <= expected <= after["xp_total_u32_reconstructed"],
            })

    first_offset_intersection = [
        max(x[0] for x in first_offset_intervals),
        min(x[1] for x in first_offset_intervals),
    ]

    second_cycle = []
    second_offset_intervals = []
    for row in rep_levels:
        level = row["level"]
        if 31 <= level <= 59:
            curve_index = level - 31
            if curve_index >= len(cumulative):
                break
            curve_cum = cumulative[curve_index]
            before, after = bracket_for(row["loop"])
            lo = before["xp_total_u32_reconstructed"] - curve_cum
            hi = after["xp_total_u32_reconstructed"] - curve_cum
            second_offset_intervals.append((lo, hi))
            second_cycle.append({
                "level": level,
                "loop": row["loop"],
                "time": row["time"],
                "reused_gamedata_cumulative_threshold": curve_cum,
                "xp_before": before["xp_total_u32_reconstructed"],
                "xp_after": after["xp_total_u32_reconstructed"],
                "implied_common_offset_interval": [lo, hi],
            })

    second_offset_intersection = [
        max(x[0] for x in second_offset_intervals),
        min(x[1] for x in second_offset_intervals),
    ]
    pow20 = 1 << 20
    for item in second_cycle:
        expected = pow20 + item["reused_gamedata_cumulative_threshold"]
        item["two_pow_20_plus_threshold"] = expected
        item["two_pow_20_hypothesis_inside_bracket"] = item["xp_before"] <= expected <= item["xp_after"]

    level30 = next(x for x in rep_levels if x["level"] == 30)
    level31 = next(x for x in rep_levels if x["level"] == 31)
    before31, after31 = bracket_for(level31["loop"])

    # Find first periodic MinionXP signed fixed-point overflow edge.
    minion_overflow = None
    for a, b in zip(team_periodic, team_periodic[1:]):
        ra = a["xp_components"]["MinionXP"]["raw_serialized_integer"]
        rb = b["xp_components"]["MinionXP"]["raw_serialized_integer"]
        if signed_i32(ra) >= 0 and signed_i32(rb) < 0:
            minion_overflow = {
                "before": {
                    "loop": a["loop"], "time": a["time"],
                    **a["xp_components"]["MinionXP"],
                },
                "after": {
                    "loop": b["loop"], "time": b["time"],
                    **b["xp_components"]["MinionXP"],
                },
                "signed_boundary_raw": 1 << 31,
                "signed_boundary_fixed": (1 << 31) / FIXED_SCALE,
                "full_i32_modulus_fixed": (1 << 32) / FIXED_SCALE,
            }
            break

    # Final scoreboard: player slot order is PlayerID 1..10 for this standard replay.
    final_score = scores[-1]["payload"] if scores else {}
    exp_values = score_stat_values(final_score, "ExperienceContribution") or []
    final_levels = score_stat_values(final_score, "Level") or []
    score_rows = []
    for idx, value in enumerate(exp_values[:10], start=1):
        if value is None:
            continue
        score_rows.append({
            "player_id": idx,
            "hero": player_hero.get(idx),
            "experience_contribution_raw": value,
            "experience_contribution_signed_i32": signed_i32(value),
            "final_score_level": final_levels[idx - 1] if idx - 1 < len(final_levels) else None,
        })
    nazeebo_score = next((x for x in score_rows if x["hero"] == "HeroWitchDoctor"), None)

    team2_last = [x for x in periodic if x["team"] == 2][-1]
    next_cycle_level31_threshold_hypothesis = pow20 + cumulative[0]

    all_first_match = all(x["threshold_inside_bracket"] for x in first_cycle)
    all_second_pow20_match = all(x["two_pow_20_hypothesis_inside_bracket"] for x in second_cycle)

    result = {
        "schema": "pyhots_hero_scaling_extreme_replay_fixture_v0_2_1",
        "fixture": {
            "replay_file": replay_path.name,
            "replay_sha256": replay_hash,
            "tracker_sha256": tracker_hash,
            "build": build,
            "elapsed_game_loops": elapsed_loops,
            "duration_seconds": elapsed_loops / 16.0,
            "duration_hms": hms(elapsed_loops / 16.0),
            "tracker_event_count": len(events),
        },
        "players": {
            "team_by_player_id": {str(k): v for k, v in sorted(player_team.items())},
            "hero_by_player_id": {str(k): v for k, v in sorted(player_hero.items())},
        },
        "team_level_summary": team_level_summary,
        "hero_xp_curve": curve,
        "normal_levels_2_30_alignment": {
            "representative_player": representative,
            "transition_count": len(first_cycle),
            "all_thresholds_inside_replay_xp_brackets": all_first_match,
            "common_offset_intersection": first_offset_intersection,
            "zero_inside_common_offset_intersection": first_offset_intersection[0] <= 0 <= first_offset_intersection[1],
            "transitions": first_cycle,
        },
        "post_30_levels_31_59_alignment": {
            "representative_player": representative,
            "transition_count": len(second_cycle),
            "observed_first_post_30_level": 31,
            "observed_last_post_30_level": max(x["level"] for x in second_cycle),
            "reused_curve_common_offset_intersection": second_offset_intersection,
            "two_pow_20": pow20,
            "two_pow_20_inside_common_offset_intersection": second_offset_intersection[0] <= pow20 <= second_offset_intersection[1],
            "all_two_pow_20_plus_curve_thresholds_inside_replay_brackets": all_second_pow20_match,
            "transitions": second_cycle,
            "engine_mechanism": "UNRESOLVED_EXACT_CAUSE_OFFSET_IS_REPLAY_CONSISTENT_WITH_2_POW_20",
        },
        "key_level_events": {
            "level_30": level30,
            "level_31": level31,
            "level_31_xp_bracket": {
                "before_loop": before31["loop"],
                "before_time": before31["time"],
                "before_xp_u32_reconstructed": before31["xp_total_u32_reconstructed"],
                "after_loop": after31["loop"],
                "after_time": after31["time"],
                "after_xp_u32_reconstructed": after31["xp_total_u32_reconstructed"],
                "two_pow_20": pow20,
                "two_pow_20_plus_first_curve_threshold": next_cycle_level31_threshold_hypothesis,
            },
            "team_2_last_periodic": {
                "loop": team2_last["loop"],
                "time": team2_last["time"],
                "team_level": team2_last["team_level"],
                "xp_total_u32_reconstructed": team2_last["xp_total_u32_reconstructed"],
                "margin_to_two_pow_20_plus_first_curve_threshold": next_cycle_level31_threshold_hypothesis - team2_last["xp_total_u32_reconstructed"],
            },
        },
        "signed_stat_overflow": {
            "periodic_minion_xp": minion_overflow,
            "final_nazeebo_experience_contribution": nazeebo_score,
        },
        "runtime_scaling_boundary": {
            "displayed_level_progression_31_plus": "REPLAY_PROVEN",
            "hero_xp_curve_levels_2_30_bracket_alignment": "REPLAY_GAMEDATA_PROVEN",
            "post_30_reused_curve_plus_common_offset_alignment": "REPLAY_GAMEDATA_PROVEN",
            "exact_post_30_engine_wrap_mechanism": "UNRESOLVED",
            "hero_hp_scaling_application_31_plus": "UNRESOLVED",
            "hero_resource_scaling_application_31_plus": "UNRESOLVED",
            "basic_attack_scaling_application_31_plus": "UNRESOLVED",
            "ability_damage_heal_shield_scaling_application_31_plus": "UNRESOLVED",
            "stat_counter_overflow_implies_combat_overflow": False,
        },
        "gates": {
            "BUILD_91093": build == 91093,
            "DURATION_OVER_3_HOURS": elapsed_loops / 16.0 > 3 * 3600,
            "TEAM_1_REACHES_59": team_level_summary.get("1", {}).get("max_level") == 59,
            "TEAM_2_STAYS_30": team_level_summary.get("2", {}).get("max_level") == 30,
            "NORMAL_2_30_CURVE_ALIGNMENT": all_first_match and first_offset_intersection[0] <= 0 <= first_offset_intersection[1],
            "POST_30_31_59_CURVE_REUSE_ALIGNMENT": all_second_pow20_match,
            "TWO_POW_20_INSIDE_POST_30_OFFSET_INTERVAL": second_offset_intersection[0] <= pow20 <= second_offset_intersection[1],
            "MINION_XP_SIGNED_FIXED_OVERFLOW": bool(minion_overflow),
            "NAZEEBO_SCORE_I32_OVERFLOW": bool(nazeebo_score and nazeebo_score["experience_contribution_signed_i32"] < 0),
            "COMBAT_SCALING_REMAINS_FAIL_CLOSED": True,
        },
    }
    result["overall"] = "PASS" if all(result["gates"].values()) else "FAIL"
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--replay", required=True, type=Path)
    ap.add_argument("--hero-global-compact", required=True, type=Path)
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()
    result = analyze(args.replay, args.hero_global_compact)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"overall": result["overall"], "gates": result["gates"]}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
