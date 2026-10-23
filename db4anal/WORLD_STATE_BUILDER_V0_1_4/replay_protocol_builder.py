from __future__ import annotations

import argparse
import bisect
import hashlib
import importlib.util
import math
import os
import re
import sys
import time
import types
import shutil
import subprocess
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Optional

try:
    import orjson
except ImportError as exc:  # fail early: bulk mode relies on orjson speed
    raise SystemExit("Pyhots Replay Protocol Builder v1.4.1 requires orjson. Install with: pip install orjson") from exc

PROTOCOL_VERSION = "1.4.1"
SHARD_SCHEMA_VERSION = "1.4"
IDENTITY_INDEX_VERSION = "1.4"
CORE_KEY_TO_SLOT = {29: "Q", 35: "W", 17: "E", 30: "R", 16: "D"}
PRESS_STATE = 8
RELEASE_STATE = 0
DEFAULT_PAIR_WINDOW = 0.125
DEFAULT_OBSERVABLE_SIGNATURE_SAMPLES = 64
DEFAULT_OBSERVABLE_VALUE_SAMPLES = 128

# Large event streams must never be traversed just to discover build metadata.
HEAVY_STREAM_NAMES = {
    "gameevents", "trackerevents", "messageevents", "attributeevents", "statevents",
    "details_events", "events", "raw_events",
}
BUILD_KEYS = {
    "databuild": 0,
    "m_databuild": 0,
    "replaybuild": 1,
    "gamebuild": 1,
    "basebuild": 2,
    "m_basebuild": 2,
    "build": 3,
}


def load_json(path: Path) -> Any:
    data = path.read_bytes()
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return orjson.loads(data)


def dumps_json(value: Any, pretty: bool = True) -> bytes:
    option = orjson.OPT_NON_STR_KEYS
    if pretty:
        option |= orjson.OPT_INDENT_2
    return orjson.dumps(value, option=option)


def save_json(path: Path, value: Any, pretty: bool = True) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_bytes(dumps_json(value, pretty=pretty))
    os.replace(tmp, path)


def safe_float(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def safe_int(v: Any) -> Optional[int]:
    x = safe_float(v)
    if x is None or abs(x - round(x)) > 1e-9:
        return None
    return int(round(x))


def parse_timestamp(v: Any) -> Optional[float]:
    if v is None:
        return None
    text = str(v).strip()
    parts = text.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600.0 + int(parts[1]) * 60.0 + float(parts[2])
        x = float(text)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def parse_embedded_json(v: Any) -> Any:
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, (bytes, bytearray)):
        try:
            return orjson.loads(v)
        except orjson.JSONDecodeError:
            return {}
    if not isinstance(v, str):
        return {}
    try:
        return orjson.loads(v)
    except orjson.JSONDecodeError:
        return {}


def norm(s: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(s or "").lower())


def stable_hash(value: Any) -> str:
    blob = orjson.dumps(value, option=orjson.OPT_SORT_KEYS | orjson.OPT_NON_STR_KEYS)
    return hashlib.sha256(blob).hexdigest()[:16]


def file_fingerprint(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _metadata_walk(obj: Any, path: str = "$", depth: int = 0, max_depth: int = 7) -> Iterable[tuple[str, str, Any]]:
    """Bounded metadata walk. It deliberately skips large event lists."""
    if depth > max_depth:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            sk = str(k)
            p = f"{path}.{sk}"
            yield p, sk, v
            nk = sk.lower().replace("-", "_")
            if nk in HEAVY_STREAM_NAMES:
                continue
            if isinstance(v, list) and len(v) > 64:
                continue
            yield from _metadata_walk(v, p, depth + 1, max_depth)
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:32]):
            yield from _metadata_walk(v, f"{path}[{i}]", depth + 1, max_depth)


def extract_replay_build(replay: dict) -> dict:
    """Fast, best-effort metadata extraction. Never scans full event streams and never invents a build."""
    if not isinstance(replay, dict):
        return {"build": None, "status": "UNKNOWN", "source_path": None}

    found: list[tuple[int, int, str, int]] = []

    # Fast path: common metadata roots first.
    preferred_roots = (
        "Header", "ReplayHeader", "header", "Metadata", "ReplayMetadata",
        "Details", "InitData", "ReplayDetails", "GameDescription",
    )
    for root_name in preferred_roots:
        root = replay.get(root_name)
        if root is None:
            continue
        for path, key, value in _metadata_walk(root, f"$.{root_name}", max_depth=6):
            nk = key.lower().replace("-", "_")
            if nk not in BUILD_KEYS:
                continue
            iv = safe_int(value)
            if iv is not None and iv > 0:
                found.append((BUILD_KEYS[nk], -len(path), path, iv))

    # Top-level scalars can also carry a build.
    for key, value in replay.items():
        nk = str(key).lower().replace("-", "_")
        if nk in BUILD_KEYS:
            iv = safe_int(value)
            if iv is not None and iv > 0:
                found.append((BUILD_KEYS[nk], -len(str(key)), f"$.{key}", iv))

    # Bounded fallback over metadata-sized objects only.
    if not found:
        for path, key, value in _metadata_walk(replay, max_depth=5):
            nk = key.lower().replace("-", "_")
            if nk not in BUILD_KEYS:
                continue
            iv = safe_int(value)
            if iv is not None and iv > 0:
                found.append((BUILD_KEYS[nk], -len(path), path, iv))

    if not found:
        return {"build": None, "status": "UNKNOWN", "source_path": None}
    found.sort()
    _, _, path, build = found[0]
    return {"build": build, "status": "OBSERVED", "source_path": path}


def is_decoded_replay_json(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("GameEvents"), list)
        and isinstance(value.get("Players"), list)
    )


def replay_players(replay: dict) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in replay.get("Players", []) or []:
        if not isinstance(p, dict):
            continue
        pid = p.get("PlayerToonId") or p.get("PlayerId") or p.get("Player")
        if pid is None:
            continue
        hero_obj = p.get("PlayerHero") if isinstance(p.get("PlayerHero"), dict) else {}
        hero = hero_obj.get("HeroName") or p.get("Hero") or p.get("HeroName")
        # HeroesDecode exposes several hero identifiers. HeroUnitId is the tracker/GameData
        # unit identity and is the correct identity bridge for World State. HeroId can differ
        # from the final in-match hero in modes such as ARAM, so preserve it separately.
        hero_unit_id = hero_obj.get("HeroUnitId") or p.get("HeroUnitId")
        hero_link_id = hero_obj.get("HeroId") or p.get("HeroId")
        hero_attribute_id = hero_obj.get("HeroAttributeId") or p.get("HeroAttributeId")
        hero_id = hero_unit_id or hero_link_id
        talents = []
        for t in p.get("HeroTalents", []) or []:
            if not isinstance(t, dict):
                continue
            talents.append({
                "talent_id": t.get("TalentNameId") or t.get("TalentId"),
                "time": parse_timestamp(t.get("Timestamp") or t.get("TimeStamp")),
                "slot_id": t.get("TalentSlotId"),
            })
        out[str(pid)] = {
            "player_id": str(pid),
            "name": p.get("Name"),
            "hero": hero,
            "hero_id": hero_id,
            "hero_unit_id": hero_unit_id,
            "hero_link_id": hero_link_id,
            "hero_attribute_id": hero_attribute_id,
            "team": p.get("Team"),
            "talents": talents,
        }
    return out


def compact_ability_schema(schema: dict, source_sha256: Optional[str] = None) -> dict:
    """Strip the 100+ MB combat schema down to identity data needed by replay workers."""
    out = {"metadata": {"kind": "PYHOTS_REPLAY_IDENTITY_INDEX", "version": IDENTITY_INDEX_VERSION, "source_sha256": source_sha256}, "heroes": {}}
    heroes = schema.get("heroes", {}) if isinstance(schema, dict) else {}
    for hero_name, hero in heroes.items():
        if not isinstance(hero, dict):
            continue
        compact_hero = {
            "name": hero.get("name"), "hero_key": hero.get("hero_key"), "unit_id": hero.get("unit_id"),
            "attribute_id": hero.get("attribute_id"), "hyperlink_id": hero.get("hyperlink_id"),
            "abilities": [], "talents": [],
        }
        for a in hero.get("abilities", []) or []:
            if not isinstance(a, dict):
                continue
            row = {k: a.get(k) for k in (
                "ability_id", "ability_type", "button_id", "name", "category",
                "source", "parent_link_id", "link_id", "record_key"
            ) if a.get(k) is not None}
            replay_v1 = a.get("replay_v1") if isinstance(a.get("replay_v1"), dict) else {}
            spatial = replay_v1.get("spatial") if isinstance(replay_v1.get("spatial"), dict) else {}
            if spatial.get("semantic_type") is not None:
                row["runtime_semantic"] = spatial.get("semantic_type")
            if spatial.get("ability_role") is not None:
                row["ability_role"] = spatial.get("ability_role")
            compact_hero["abilities"].append(row)
        for t in hero.get("talents", []) or []:
            if not isinstance(t, dict):
                continue
            compact_hero["talents"].append({k: t.get(k) for k in (
                "talent_id", "ability_id", "ability_type", "button_id", "level", "name"
            ) if t.get(k) is not None})
        out["heroes"][hero_name] = compact_hero
    return out


class AbilitySchemaIndex:
    """Canonical identity resolver over Pyhots abilities_complete_v1.16+ schema."""

    def __init__(self, schema: dict):
        self.schema = schema
        self.hero_alias: dict[str, str] = {}
        self.by_slot: dict[tuple[str, str], list[dict]] = defaultdict(list)
        self.talents: dict[tuple[str, str], dict] = {}
        self.ability_by_id: dict[tuple[str, str], dict] = {}
        self.hero_unit_id: dict[str, str] = {}
        heroes = schema.get("heroes", {}) if isinstance(schema, dict) else {}
        for hero_name, hero in heroes.items():
            if not isinstance(hero, dict):
                continue
            aliases = [hero_name, hero.get("name"), hero.get("hero_key"), hero.get("unit_id"), hero.get("attribute_id"), hero.get("hyperlink_id")]
            if hero.get("unit_id"):
                self.hero_unit_id[hero_name] = str(hero.get("unit_id"))
            for alias in aliases:
                if alias:
                    self.hero_alias[norm(alias)] = hero_name
            for ability in hero.get("abilities", []) or []:
                if not isinstance(ability, dict):
                    continue
                slot = self._slot_of(ability)
                if ability.get("ability_id"):
                    self.ability_by_id[(hero_name, str(ability.get("ability_id")))] = ability
                if slot:
                    self.by_slot[(hero_name, slot)].append(ability)
            for talent in hero.get("talents", []) or []:
                if not isinstance(talent, dict) or not talent.get("talent_id"):
                    continue
                self.talents[(hero_name, str(talent["talent_id"]))] = talent

    @staticmethod
    def _slot_of(a: dict) -> Optional[str]:
        t = str(a.get("ability_type") or "").strip().lower()
        if t in {"q", "w", "e"}:
            return t.upper()
        if t == "heroic":
            return "R"
        if t == "trait":
            return "D"
        if t == "z":
            return "Z"
        if t == "b":
            return "B"
        return None

    def canonical_hero(self, hero: Any, hero_id: Any = None) -> Optional[str]:
        for value in (hero, hero_id):
            if value and norm(value) in self.hero_alias:
                return self.hero_alias[norm(value)]
        return None

    def selected_heroic(self, hero_name: str, picked_talents: list[dict]) -> Optional[str]:
        # Most heroes choose a Heroic at level 10, but this is not universal (Varian is
        # the canonical counterexample). Select the earliest picked Heroic talent and
        # ignore later Heroic upgrades rather than hardcoding a level.
        candidates: list[tuple[int, str]] = []
        for picked in picked_talents:
            tid = picked.get("talent_id")
            if not tid:
                continue
            t = self.talents.get((hero_name, str(tid)))
            if not t or str(t.get("ability_type") or "").lower() != "heroic" or not t.get("ability_id"):
                continue
            level_text = str(t.get("level") or "")
            match = re.search(r"(\d+)", level_text)
            level = int(match.group(1)) if match else 10_000
            candidates.append((level, str(t["ability_id"])))
        if not candidates:
            return None
        earliest = min(level for level, _ in candidates)
        ability_ids = {ability_id for level, ability_id in candidates if level == earliest}
        return next(iter(ability_ids)) if len(ability_ids) == 1 else None

    def active_talents(self, hero_name: str, picked_talents: list[dict]) -> list[dict]:
        out = []
        for picked in picked_talents:
            tid = picked.get("talent_id")
            if not tid:
                continue
            t = self.talents.get((hero_name, str(tid)))
            if not t:
                continue
            if str(t.get("ability_type") or "").lower() != "active":
                continue
            ability_id = t.get("ability_id")
            if not ability_id or str(ability_id) == ":PASSIVE:":
                continue
            out.append({**picked, "talent": t})
        return out

    @staticmethod
    def _parent_ability_id(a: dict) -> Optional[str]:
        parent = a.get("parent_link_id")
        if not parent:
            return None
        text = str(parent)
        return text.split("|", 1)[0] if text else None

    @staticmethod
    def _runtime_variant_role(a: dict) -> str:
        semantic = str(a.get("runtime_semantic") or "").upper()
        source = str(a.get("source") or "").upper()
        if semantic == "CANCEL":
            return "CANCEL"
        if semantic == "RECAST":
            return "RECAST"
        if source == "HERODATA_SUBABILITY" or a.get("parent_link_id"):
            return "SUBABILITY"
        return "BASE"

    def slot_candidates(self, hero_name: str, slot: str) -> list[dict]:
        grouped: dict[str, list[dict]] = defaultdict(list)
        for a in self.by_slot.get((hero_name, slot), []):
            if a.get("ability_id") == ":PASSIVE:" or not a.get("ability_id"):
                continue
            grouped[str(a.get("ability_id"))].append(a)
        out = []
        for ability_id, rows in grouped.items():
            first = rows[0]
            buttons = sorted({str(a.get("button_id")) for a in rows if a.get("button_id")})
            out.append({
                "ability_id": ability_id, "ability_name": first.get("name"),
                "button_id": first.get("button_id"), "button_variants": buttons,
                "source": first.get("source"),
                "parent_ability_id": self._parent_ability_id(first),
                "runtime_semantic": first.get("runtime_semantic"),
                "variant_role": self._runtime_variant_role(first),
            })
        return out

    def ability_identity(self, hero_name: str, ability_id: str) -> Optional[dict]:
        return self.ability_by_id.get((hero_name, str(ability_id)))

    def resolve_core_slot(self, hero_name: str, slot: str, picked_talents: list[dict]) -> tuple[Optional[dict], str]:
        candidates = [a for a in self.by_slot.get((hero_name, slot), []) if a.get("ability_id") != ":PASSIVE:"]
        by_ability_id: dict[str, list[dict]] = defaultdict(list)
        for a in candidates:
            if a.get("ability_id"):
                by_ability_id[str(a.get("ability_id"))].append(a)
        if slot == "R":
            selected = self.selected_heroic(hero_name, picked_talents)
            if selected and selected in by_ability_id:
                # Multiple hotbar/button variants (e.g. D.Va Bunny Hop On/Off) are one
                # canonical ability, not multiple Heroic choices.
                return by_ability_id[selected][0], "SELECTED_HEROIC"
            if len(by_ability_id) == 1:
                return next(iter(by_ability_id.values()))[0], "UNIQUE_SLOT"
            return None, "AMBIGUOUS_HEROIC" if by_ability_id else "SLOT_NOT_FOUND"
        if len(by_ability_id) == 1:
            return next(iter(by_ability_id.values()))[0], "UNIQUE_SLOT"
        return None, "AMBIGUOUS_SLOT" if by_ability_id else "SLOT_NOT_FOUND"


def _event_type(e: dict) -> str:
    raw = str(e.get("GameEventType") or e.get("TrackerEventType") or e.get("MessageEventType") or e.get("EventType") or e.get("_event") or "")
    return raw.rsplit(".", 1)[-1]


def _event_time(e: dict) -> Optional[float]:
    t = parse_timestamp(e.get("TimeStamp") if e.get("TimeStamp") is not None else e.get("Timestamp"))
    if t is not None:
        return t
    gl = safe_int(e.get("_gameloop"))
    return (gl / 16.0) if gl is not None else None


def _event_player(e: dict) -> Optional[str]:
    pid = e.get("Player")
    if pid is not None:
        return str(pid)
    uid = e.get("_userid")
    if isinstance(uid, dict):
        uid = uid.get("m_userId")
    return str(uid) if uid is not None else None


def parse_key_event(e: dict) -> Optional[dict]:
    if _event_type(e).lower() not in {"striggerkeypressedevent", "striggerkeypressed"}:
        return None
    t = _event_time(e)
    pid = _event_player(e)
    data = parse_embedded_json(e.get("Data")) if "Data" in e else e
    if not isinstance(data, dict):
        return None
    key = safe_int(data.get("0"))
    state = safe_int(data.get("1"))
    if key is None:
        for k in ("m_key", "m_keyIndex", "m_keyCode", "key"):
            if k in data:
                key = safe_int(data.get(k)); break
    if state is None:
        for k in ("m_state", "m_keyState", "state"):
            if k in data:
                state = safe_int(data.get(k)); break
    if t is None or pid is None or key is None or state is None:
        return None
    return {"time": t, "player_id": pid, "key_code": key, "state": state}


def parse_cmd_event(e: dict) -> Optional[dict]:
    if _event_type(e).lower() not in {"scmdevent", "scmd"}:
        return None
    t = _event_time(e)
    pid = _event_player(e)
    if t is None or pid is None:
        return None

    if "Data" in e:
        data = parse_embedded_json(e.get("Data"))
        if not isinstance(data, dict):
            return None
        d1 = data.get("1")
        abil_link = cmd_index = None
        if isinstance(d1, dict):
            abil_link = safe_int(d1.get("0"))
            cmd_index = safe_int(d1.get("1"))
        return {
            "time": t, "player_id": pid, "cmd_flags": safe_int(data.get("0")),
            "abil_link": abil_link, "cmd_index": cmd_index, "target_payload": data.get("2"),
            "sequence": safe_int(data.get("3")),
            "ability_shaped": abil_link is not None and cmd_index is not None,
        }

    abil = e.get("m_abil") if isinstance(e.get("m_abil"), dict) else {}
    abil_link = safe_int(abil.get("m_abilLink"))
    cmd_index = safe_int(abil.get("m_abilCmdIndex"))
    return {
        "time": t,
        "player_id": pid,
        "cmd_flags": safe_int(e.get("m_cmdFlags")),
        "abil_link": abil_link,
        "cmd_index": cmd_index,
        "target_payload": e.get("m_data"),
        "sequence": safe_int(e.get("m_sequence")),
        "ability_shaped": abil_link is not None and cmd_index is not None,
    }


def _decode_bytes(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, dict):
        return {str(_decode_bytes(k)): _decode_bytes(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_decode_bytes(v) for v in value]
    if isinstance(value, tuple):
        return [_decode_bytes(v) for v in value]
    return value



def resolve_heroesdecode_command(explicit: Optional[str] = None) -> list[str]:
    """Resolve HeroesDecode without bundling any executable.

    Supported installations:
      * --heroesdecode-path C:\\...\\heroesdecode.exe
      * --heroesdecode-path C:\\...\\heroesdecode.dll (run through dotnet)
      * dotnet global tool (dotnet-heroes-decode / dotnet heroes-decode)
      * standalone heroesdecode / heroes-decode on PATH
    """
    if explicit:
        raw = str(explicit).strip()
        candidate = Path(raw).expanduser()
        if candidate.exists():
            candidate = candidate.resolve()
            if candidate.suffix.lower() == ".dll":
                dotnet = shutil.which("dotnet")
                if not dotnet:
                    raise RuntimeError("HEROESDECODE_DOTNET_NOT_FOUND: .dll decoder requires dotnet on PATH")
                return [dotnet, str(candidate)]
            return [str(candidate)]
        found = shutil.which(raw)
        if found:
            return [found]
        raise RuntimeError(f"HEROESDECODE_NOT_FOUND: --heroesdecode-path does not exist and is not on PATH: {raw}")

    for name in ("dotnet-heroes-decode", "heroesdecode", "heroes-decode"):
        found = shutil.which(name)
        if found:
            return [found]
    dotnet = shutil.which("dotnet")
    if dotnet:
        # Official dotnet global-tool invocation documented by HeroesDecode.
        return [dotnet, "heroes-decode"]
    raise RuntimeError(
        "HEROESDECODE_NOT_FOUND: install the HeroesDecode .NET global tool or pass "
        "--heroesdecode-path to heroesdecode.exe/heroesdecode.dll"
    )


def check_heroesdecode_command(command: list[str], timeout: float = 30.0) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            [*command, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    text_out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    text_out = text_out.strip()
    if proc.returncode != 0:
        return False, f"exit={proc.returncode}: {text_out[-1000:]}"
    return True, text_out[-1000:] or "OK"


def parse_heroesdecode_build_output(text: str) -> Optional[int]:
    """Parse build from HeroesDecode console output, e.g. [2.55.8.97771]."""
    if not text:
        return None
    matches = re.findall(r"(?:^|[^0-9])(\d+)\.(\d+)\.(\d+)\.(\d+)(?:[^0-9]|$)", text)
    if matches:
        return int(matches[-1][3])
    match = re.search(r"\b(?:Build|Version)\s*:\s*(?:\d+\.){0,3}(\d{5,6})\b", text, re.I)
    return int(match.group(1)) if match else None


def parse_heroesdecode_map_output(text: str) -> tuple[Optional[str], Optional[str]]:
    """Parse ``Map: Title [InternalMapId]`` from normal HeroesDecode output."""
    if not text:
        return None, None
    match = re.search(r"^\s*Map\s*:\s*(.*?)\s*\[([^]\r\n]+)\]\s*$", text, re.I | re.M)
    if not match:
        return None, None
    title = match.group(1).strip() or None
    map_id = match.group(2).strip() or None
    return title, map_id


def _heroesdecode_json_candidates(output_dir: Path) -> list[Path]:
    return sorted(
        (p for p in output_dir.rglob("*.json") if p.is_file()),
        key=lambda p: (p.stat().st_mtime_ns, p.stat().st_size),
        reverse=True,
    )


def decode_stormreplay_heroesdecode(
    path: Path,
    command: list[str],
    timeout: float = 180.0,
    keep_decoded_dir: Optional[Path] = None,
) -> dict:
    """Decode one .StormReplay through HeroesDecode/StormReplayParser.

    HeroesDecode is deliberately treated as an external decoder. Pyhots never bundles
    or modifies its binaries. The produced JSON is fed into exactly the same parser used
    for legacy HeroesDecode JSON, keeping one normalization path for both inputs.
    """
    with tempfile.TemporaryDirectory(prefix="pyhots_heroesdecode_") as td:
        outdir = Path(td)
        cmd = [
            *command,
            "get-json",
            "--replay-path", str(path),
            "--has-tracker-events",
            "--has-game-events",
            "--include-all-message-events",
            "--no-json-display",
            "--output-directory", str(outdir),
        ]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"HEROESDECODE_TIMEOUT: exceeded {timeout}s for {path.name}") from exc
        except Exception as exc:
            raise RuntimeError(f"HEROESDECODE_EXEC_FAILED: {type(exc).__name__}: {exc}") from exc

        if proc.returncode != 0:
            detail = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()[-4000:]
            raise RuntimeError(f"HEROESDECODE_FAILED: exit={proc.returncode}: {detail}")

        replay = None
        selected_json = None
        errors = []
        for candidate in _heroesdecode_json_candidates(outdir):
            try:
                value = load_json(candidate)
            except Exception as exc:
                errors.append(f"{candidate.name}: {type(exc).__name__}: {exc}")
                continue
            if is_decoded_replay_json(value):
                replay = value
                selected_json = candidate
                break
        if replay is None:
            listing = [p.name for p in _heroesdecode_json_candidates(outdir)]
            raise RuntimeError(
                "HEROESDECODE_NO_REPLAY_JSON: decoder exited successfully but no HeroesDecode replay JSON "
                f"was found. json_files={listing[:20]} parse_errors={errors[:5]}"
            )

        build_info = extract_replay_build(replay)
        base_build = build_info.get("build")
        map_name = replay.get("__map_name__")
        map_id = replay.get("__map_id__")
        summary_text = ""
        # HeroesDecode get-json does not reliably expose build/map metadata in the JSON.
        # We already needed one lightweight summary invocation for build provenance; use
        # the normal summary (not --result-only, which intentionally hides map info) so
        # the same invocation supplies both Version and Map: Title [MapId].
        if base_build is None or map_name is None or map_id is None:
            summary_cmd = [*command, "--replay-path", str(path)]
            try:
                summary_proc = subprocess.run(
                    summary_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=min(timeout, 60.0),
                    check=False,
                )
                summary_text = ((summary_proc.stdout or "") + "\n" + (summary_proc.stderr or "")).strip()
                if summary_proc.returncode == 0:
                    if base_build is None:
                        base_build = parse_heroesdecode_build_output(summary_text)
                    parsed_map_name, parsed_map_id = parse_heroesdecode_map_output(summary_text)
                    map_name = map_name or parsed_map_name
                    map_id = map_id or parsed_map_id
            except Exception:
                # Build/map provenance is useful, but decoded event data remains valid.
                pass

        if keep_decoded_dir is not None and selected_json is not None:
            keep_decoded_dir.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12]
            target = keep_decoded_dir / f"{path.stem}.{digest}.heroesdecode.json"
            shutil.copy2(selected_json, target)

        replay["__source_format__"] = "STORMREPLAY_HEROESDECODE"
        replay["__storm_decoder__"] = "HeroesDecode"
        if base_build is not None:
            replay["__base_build__"] = int(base_build)
            if build_info.get("build") is None:
                replay["__base_build_status__"] = "OBSERVED_HEROESDECODE_SUMMARY"
                replay["__base_build_source__"] = "HeroesDecode summary"
        if map_name is not None:
            replay["__map_name__"] = map_name
        if map_id is not None:
            replay["__map_id__"] = map_id
        if summary_text:
            replay["__heroesdecode_summary__"] = summary_text[-4000:]
        return replay


def _heroprotocol_versions_dir() -> Path:
    """Locate installed heroprotocol protocol files without importing heroprotocol.versions.

    Blizzard's heroprotocol.versions still imports the removed stdlib ``imp`` module.
    Pyhots deliberately bypasses that module so raw replay decoding works on modern
    Python versions (including 3.12, 3.13 and 3.14).
    """
    spec = importlib.util.find_spec("heroprotocol")
    if spec is None:
        raise ImportError("No module named 'heroprotocol'")
    locations = list(spec.submodule_search_locations or [])
    if locations:
        package_dir = Path(locations[0])
    elif spec.origin:
        package_dir = Path(spec.origin).resolve().parent
    else:
        raise ImportError("Cannot locate installed heroprotocol package")
    versions_dir = package_dir / "versions"
    if not versions_dir.is_dir():
        raise ImportError(f"heroprotocol versions directory not found: {versions_dir}")
    return versions_dir


def _load_heroprotocol_protocol(protocol_path: Path):
    """Load one generated Blizzard protocol module directly via importlib."""
    if not protocol_path.is_file():
        raise ImportError(f"Protocol module not found: {protocol_path.name}")
    # Protocol files use absolute imports (heroprotocol.decoders), not relative imports,
    # so loading them directly avoids executing heroprotocol.versions/__init__.py.
    module_name = f"_pyhots_{protocol_path.stem}_{abs(hash(str(protocol_path.resolve())))}"
    existing = sys.modules.get(module_name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(module_name, protocol_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {protocol_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def _latest_heroprotocol_protocol():
    versions_dir = _heroprotocol_versions_dir()
    candidates = []
    for path in versions_dir.glob("protocol*.py"):
        match = re.fullmatch(r"protocol(\d+)\.py", path.name)
        if match:
            candidates.append((int(match.group(1)), path))
    if not candidates:
        raise ImportError(f"No protocol*.py files found in {versions_dir}")
    _, path = max(candidates, key=lambda item: item[0])
    return _load_heroprotocol_protocol(path)


def _build_heroprotocol_protocol(base_build: int):
    versions_dir = _heroprotocol_versions_dir()
    path = versions_dir / f"protocol{int(base_build)}.py"
    if not path.is_file():
        available = []
        for candidate in versions_dir.glob("protocol*.py"):
            match = re.fullmatch(r"protocol(\d+)\.py", candidate.name)
            if match:
                available.append(int(match.group(1)))
        latest = max(available) if available else None
        raise ImportError(
            f"Unsupported replay build {base_build}: {path.name} is not installed"
            + (f" (latest installed protocol build: {latest})" if latest is not None else "")
        )
    return _load_heroprotocol_protocol(path)


def check_stormreplay_dependencies() -> tuple[bool, list[str]]:
    """Return availability of raw .StormReplay dependencies with precise diagnostics."""
    missing: list[str] = []
    try:
        import mpyq  # noqa: F401
    except ImportError as exc:
        missing.append(f"mpyq>=0.2.5 ({exc})")
    try:
        _heroprotocol_versions_dir()
        # Also import a generated protocol module; this verifies heroprotocol.decoders
        # and six without ever importing the legacy heroprotocol.versions module.
        _latest_heroprotocol_protocol()
    except Exception as exc:
        missing.append(f"heroprotocol protocol loader ({exc.__class__.__name__}: {exc})")
    return (not missing, missing)

def decode_stormreplay(path: Path) -> dict:
    """Decode a raw .StormReplay using Blizzard heroprotocol, if installed.

    This uses the programmatic API (no shell/subprocess). Raw protocol events are kept
    close to their native shape; parse_key_event/parse_cmd_event understand both the
    legacy JSON wrapper and native heroprotocol fields.
    """
    try:
        import mpyq  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            "RAW_STORMREPLAY_MISSING_MPYQ: install with: "
            "python -m pip install --upgrade mpyq>=0.2.5"
        ) from exc
    try:
        latest_protocol = _latest_heroprotocol_protocol()
    except Exception as exc:
        raise RuntimeError(
            "RAW_STORMREPLAY_HEROPROTOCOL_LOAD_FAILED: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc

    archive = mpyq.MPQArchive(str(path))
    header_content = archive.header["user_data_header"]["content"]
    header = latest_protocol.decode_replay_header(header_content)
    base_build = int(header["m_version"]["m_baseBuild"])
    try:
        protocol = _build_heroprotocol_protocol(base_build)
    except Exception as exc:
        raise RuntimeError(
            "RAW_STORMREPLAY_UNSUPPORTED_BUILD: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc

    def read(name: str) -> Optional[bytes]:
        try:
            return archive.read_file(name)
        except Exception:
            return None

    details_blob = read("replay.details")
    init_blob = read("replay.initData")
    game_blob = read("replay.game.events")
    message_blob = read("replay.message.events")
    tracker_blob = read("replay.tracker.events")

    details = protocol.decode_replay_details(details_blob) if details_blob else {}
    initdata = protocol.decode_replay_initdata(init_blob) if init_blob else {}
    gameevents = list(protocol.decode_replay_game_events(game_blob)) if game_blob else []
    messageevents = list(protocol.decode_replay_message_events(message_blob)) if message_blob else []
    trackerevents = list(protocol.decode_replay_tracker_events(tracker_blob)) if tracker_blob else []

    # Map native user ids to detail rows by player name where possible. This mapping is
    # evidence, not an assumption: unmatched users stay absent and therefore unresolved.
    detail_players = []
    for p in (details.get("m_playerList", []) if isinstance(details, dict) else []) or []:
        if not isinstance(p, dict):
            continue
        detail_players.append({
            "name": _decode_bytes(p.get("m_name")),
            "hero": _decode_bytes(p.get("m_hero")),
            "team": p.get("m_teamId"),
            "toon": _decode_bytes(p.get("m_toon")),
            "working_slot": p.get("m_workingSetSlotId"),
        })

    user_initial = []
    try:
        user_initial = initdata["m_syncLobbyState"]["m_userInitialData"] or []
    except Exception:
        user_initial = []
    by_name = {str(p.get("name")): p for p in detail_players if p.get("name")}
    players = []
    used = set()
    for uid, u in enumerate(user_initial):
        if not isinstance(u, dict):
            continue
        name = _decode_bytes(u.get("m_name"))
        dp = by_name.get(str(name))
        if not dp:
            continue
        used.add(str(name))
        players.append({
            "PlayerToonId": str(uid), "Name": name,
            "PlayerHero": {"HeroName": dp.get("hero"), "HeroId": None},
            "Team": dp.get("team"), "HeroTalents": [],
        })
    # Fallback detail rows are still useful for observables, but use working slot only
    # when no user-id mapping was found. Canonical commands may remain unresolved.
    if not players:
        for dp in detail_players:
            pid = dp.get("working_slot")
            if pid is None:
                continue
            players.append({
                "PlayerToonId": str(pid), "Name": dp.get("name"),
                "PlayerHero": {"HeroName": dp.get("hero"), "HeroId": None},
                "Team": dp.get("team"), "HeroTalents": [],
            })

    return _decode_bytes({
        "Header": header,
        "Details": details,
        "InitData": initdata,
        "Players": players,
        "GameEvents": gameevents,
        "MessageEvents": messageevents,
        "TrackerEvents": trackerevents,
        "__source_format__": "STORMREPLAY_HEROPROTOCOL",
        "__base_build__": base_build,
    })


def pair_key_and_cmd_events(replay: dict, pair_window: float = DEFAULT_PAIR_WINDOW) -> list[dict]:
    """Linear-time pairing after per-player sort.

    v1 scanned all commands for every key (quadratic). With sorted streams, the earliest
    unused command within the non-negative window is also the nearest valid command.
    """
    keys: dict[str, list[dict]] = defaultdict(list)
    cmds: dict[str, list[dict]] = defaultdict(list)
    for e in replay.get("GameEvents", []) or []:
        if not isinstance(e, dict):
            continue
        typ = e.get("GameEventType")
        if typ == "sTriggerKeyPressedEvent":
            k = parse_key_event(e)
            if k and k["state"] == PRESS_STATE:
                keys[k["player_id"]].append(k)
        elif typ == "sCmdEvent":
            c = parse_cmd_event(e)
            if c and c["ability_shaped"]:
                cmds[c["player_id"]].append(c)

    result = []
    for pid, pkeys in keys.items():
        pkeys.sort(key=lambda x: x["time"])
        pcmds = cmds.get(pid, [])
        pcmds.sort(key=lambda x: x["time"])
        ci = 0
        for key in pkeys:
            lower = key["time"] - 0.001
            upper = key["time"] + pair_window
            while ci < len(pcmds) and pcmds[ci]["time"] < lower:
                ci += 1
            if ci >= len(pcmds):
                break
            cmd = pcmds[ci]
            if cmd["time"] > upper:
                continue
            ci += 1
            result.append({
                "time": key["time"],
                "cmd_time": cmd["time"],
                "dt": round(cmd["time"] - key["time"], 6),
                "player_id": pid,
                "key_code": key["key_code"],
                "slot": CORE_KEY_TO_SLOT.get(key["key_code"]),
                "cmd_flags": cmd["cmd_flags"],
                "abil_link": cmd["abil_link"],
                "cmd_index": cmd["cmd_index"],
                "target_payload": cmd["target_payload"],
                "sequence": cmd["sequence"],
            })
    return result


def confidence_for_binding(matches: int, total: int) -> str:
    ratio = matches / total if total else 0.0
    if matches >= 5 and ratio >= 0.85:
        return "HIGH"
    if matches >= 3 and ratio >= 0.65:
        return "MEDIUM"
    return "LOW"


def _data_index(data: Any, index: int) -> Any:
    if isinstance(data, (list, tuple)):
        return data[index] if 0 <= index < len(data) else None
    if isinstance(data, dict):
        if index in data:
            return data[index]
        return data.get(str(index))
    return None


def parse_dynamic_button_swap_event(e: dict) -> Optional[dict]:
    if _event_type(e) != "sDynamicButtonSwapEvent":
        return None
    t = _event_time(e)
    pid = _event_player(e)
    data = e.get("Data") if e.get("Data") is not None else e.get("m_data")
    if t is None or pid is None:
        return None
    return {
        "time": t,
        "player_id": pid,
        # Keep the values opaque. Their semantic meaning is not assumed by v1.4.
        "swap_token": _data_index(data, 0),
        "from_button": _data_index(data, 1),
        "to_button": _data_index(data, 2),
    }


def _tracker_tag(data: Any) -> Optional[tuple[int, int]]:
    a = safe_int(_data_index(data, 0))
    b = safe_int(_data_index(data, 1))
    return (a, b) if a is not None and b is not None else None


def build_runtime_state_evidence(replay: dict, players: dict[str, dict], schema: AbilitySchemaIndex) -> dict:
    """Build a fail-closed runtime-state timeline from explicit replay evidence.

    The state layer intentionally does not infer gameplay semantics from opaque button-swap
    ids. Unit type transitions are attached to a player only when a tracker unit can first be
    matched unambiguously to that player's canonical hero unit.
    """
    hero_to_players: dict[str, list[str]] = defaultdict(list)
    for pid, player in players.items():
        hero = schema.canonical_hero(player.get("hero"), player.get("hero_id"))
        if hero:
            hero_to_players[hero].append(pid)

    tag_owner: dict[tuple[int, int], str] = {}
    current_unit_type: dict[str, str] = {}
    unit_type_timeline: dict[str, list[dict]] = defaultdict(list)
    state_events: list[dict] = []

    tracker_events = sorted(
        (e for e in (replay.get("TrackerEvents", []) or []) if isinstance(e, dict)),
        key=lambda e: (_event_time(e) if _event_time(e) is not None else float("inf")),
    )
    for e in tracker_events:
        typ = _event_type(e)
        data = e.get("Data")
        t = _event_time(e)
        if t is None:
            continue
        if typ == "unitBornEvent":
            tag = _tracker_tag(data)
            unit_type = _data_index(data, 2)
            hero = schema.canonical_hero(unit_type)
            if tag and hero and len(hero_to_players.get(hero, [])) == 1:
                pid = hero_to_players[hero][0]
                tag_owner[tag] = pid
                current_unit_type[pid] = str(unit_type)
                rec = {"time": t, "player_id": pid, "kind": "UNIT_TYPE", "unit_type": str(unit_type), "source": "unitBornEvent"}
                unit_type_timeline[pid].append(rec)
                state_events.append(rec)
        elif typ == "unitTypeChangeEvent":
            tag = _tracker_tag(data)
            unit_type = _data_index(data, 2)
            if not tag or unit_type is None:
                continue
            pid = tag_owner.get(tag)
            if pid is None:
                # If a base hero type appears after a transformation, establish ownership only
                # when exactly one player in this replay uses that hero.
                hero = schema.canonical_hero(unit_type)
                if hero and len(hero_to_players.get(hero, [])) == 1:
                    pid = hero_to_players[hero][0]
                    tag_owner[tag] = pid
            if pid is not None:
                current_unit_type[pid] = str(unit_type)
                rec = {"time": t, "player_id": pid, "kind": "UNIT_TYPE", "unit_type": str(unit_type), "source": "unitTypeChangeEvent"}
                unit_type_timeline[pid].append(rec)
                state_events.append(rec)

    swaps: dict[str, list[dict]] = defaultdict(list)
    for e in replay.get("GameEvents", []) or []:
        if not isinstance(e, dict):
            continue
        rec = parse_dynamic_button_swap_event(e)
        if rec:
            swaps[rec["player_id"]].append(rec)
            state_events.append({**rec, "kind": "DYNAMIC_BUTTON_SWAP"})
    for rows in swaps.values():
        rows.sort(key=lambda x: x["time"])
    for rows in unit_type_timeline.values():
        rows.sort(key=lambda x: x["time"])
    state_events.sort(key=lambda x: (x.get("time", 0.0), str(x.get("player_id", ""))))
    return {
        "unit_type_timeline": dict(unit_type_timeline),
        "dynamic_button_swaps": dict(swaps),
        "events": state_events,
    }


def _state_at(timeline: list[dict], t: float, field: str) -> Any:
    if not timeline:
        return None
    times = [float(x.get("time", 0.0)) for x in timeline]
    i = bisect.bisect_right(times, t) - 1
    return timeline[i].get(field) if i >= 0 else None


def _swap_near(swaps: list[dict], t: float, window: float = 0.30) -> bool:
    if not swaps:
        return False
    times = [float(x.get("time", 0.0)) for x in swaps]
    i = bisect.bisect_left(times, t - window)
    return i < len(times) and times[i] <= t + window


def resolve_runtime_ambiguous_bindings(
    players: dict[str, dict],
    schema: AbilitySchemaIndex,
    replay_name: str,
    replay_build: Optional[int],
    pairs: list[dict],
    unresolved_rows: list[dict],
    runtime_state: dict,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Resolve structurally proven parent/subability variants without hero-specific rules.

    v1.4 only resolves a slot when all of the following hold:
      * exactly two canonical candidates exist;
      * one is a BASE ability;
      * the other explicitly has parent_link_id pointing to that BASE ability;
      * at least two repeated numeric identities exist in replay evidence;
      * the first stable identity is the base (subability cannot exist before its parent);
      * the second identity is observed after the base at least once.

    Everything else remains unresolved. DynamicButtonSwap and unitTypeChange evidence is
    preserved as provenance and never used as an undocumented numeric-id decoder.
    """
    pair_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for p in pairs:
        if p.get("slot"):
            pair_groups[(p["player_id"], p["slot"])].append(p)
    for rows in pair_groups.values():
        rows.sort(key=lambda x: x["time"])

    resolved: list[dict] = []
    remaining: list[dict] = []
    diagnostics: list[dict] = []
    for row in unresolved_rows:
        if row.get("reason") != "AMBIGUOUS_SLOT":
            remaining.append(row)
            continue
        pid, slot = str(row.get("player_id")), str(row.get("slot"))
        player = players.get(pid) or {}
        hero = row.get("hero") or schema.canonical_hero(player.get("hero"), player.get("hero_id"))
        candidates = schema.slot_candidates(str(hero), slot) if hero else []
        if len(candidates) != 2:
            remaining.append(row)
            continue
        bases = [c for c in candidates if c.get("variant_role") == "BASE"]
        if len(bases) != 1:
            remaining.append(row)
            continue
        base = bases[0]
        children = [c for c in candidates if c.get("parent_ability_id") == base.get("ability_id")]
        if len(children) != 1:
            remaining.append(row)
            continue
        child = children[0]

        slot_pairs = pair_groups.get((pid, slot), [])
        counter = Counter((p.get("abil_link"), p.get("cmd_index")) for p in slot_pairs)
        total = sum(counter.values())
        # Only repeated identities are eligible for stateful semantic mapping. This removes
        # the many one-off accidental pairings seen in large replay corpora.
        repeated = [(ident, n) for ident, n in counter.most_common() if n >= 2]
        if len(repeated) < 2:
            remaining.append(row)
            continue
        selected_identities = {repeated[0][0], repeated[1][0]}
        seq = [p for p in slot_pairs if (p.get("abil_link"), p.get("cmd_index")) in selected_identities]
        if len({(p.get("abil_link"), p.get("cmd_index")) for p in seq}) != 2:
            remaining.append(row)
            continue
        base_ident = (seq[0].get("abil_link"), seq[0].get("cmd_index"))
        child_ident = next(x for x in selected_identities if x != base_ident)

        transitions = Counter()
        prev = None
        transition_times: list[float] = []
        for p in seq:
            ident = (p.get("abil_link"), p.get("cmd_index"))
            if prev is not None and ident != prev:
                transitions[(prev, ident)] += 1
                if prev == base_ident and ident == child_ident:
                    transition_times.append(float(p["time"]))
            prev = ident
        base_to_child = transitions[(base_ident, child_ident)]
        child_to_base = transitions[(child_ident, base_ident)]
        # Require a repeated round-trip state pattern. A single nearby unrelated command can
        # otherwise masquerade as a recast/cancel (the large corpus contains exactly this
        # kind of accidental pairing). DynamicButtonSwap is retained as corroboration but is
        # not required because its numeric payload remains intentionally opaque.
        if base_to_child < 2 or child_to_base < 1:
            remaining.append(row)
            diagnostics.append({
                "replay": replay_name, "replay_build": replay_build, "player_id": pid,
                "hero": hero, "slot": slot,
                "classification": "PARENT_SUBABILITY_EVIDENCE_INSUFFICIENT",
                "base_ability_id": base.get("ability_id"), "subability_id": child.get("ability_id"),
                "base_to_child_transitions": base_to_child, "child_to_base_transitions": child_to_base,
            })
            continue

        swaps = runtime_state.get("dynamic_button_swaps", {}).get(pid, []) or []
        swap_hits = sum(1 for t in transition_times if _swap_near(swaps, t))
        unit_timeline = runtime_state.get("unit_type_timeline", {}).get(pid, []) or []
        identity_contexts: dict[tuple[Any, Any], Counter] = defaultdict(Counter)
        for p in seq:
            ident = (p.get("abil_link"), p.get("cmd_index"))
            unit_type = _state_at(unit_timeline, float(p["time"]), "unit_type")
            if unit_type:
                identity_contexts[ident][str(unit_type)] += 1

        confidence = "HIGH"
        method = "RUNTIME_PARENT_SUBABILITY_SEQUENCE"
        if swap_hits:
            method += "+BUTTON_SWAP_EVIDENCE"
        mapping = [(base_ident, base), (child_ident, child)]
        for ident, ability in mapping:
            matches = counter[ident]
            resolved.append({
                "replay": replay_name,
                "replay_build": replay_build,
                "player_id": pid,
                "player_name": player.get("name"),
                "hero": hero,
                "slot": slot,
                "key_code": row.get("key_code"),
                "abil_link": ident[0],
                "cmd_index": ident[1],
                "matches": matches,
                "total_slot_pairs": total,
                "ratio": round(matches / total, 6) if total else 0.0,
                "confidence": confidence,
                "canonical_resolution_method": method,
                "runtime_context": f"RUNTIME_VARIANT:{ability.get('ability_id')}",
                "ability_id": ability.get("ability_id"),
                "ability_name": ability.get("ability_name"),
                "button_id": ability.get("button_id"),
                "category": "RUNTIME_VARIANT",
                "numeric_variant_rank": next((i for i, (x, _) in enumerate(counter.most_common(), 1) if x == ident), None),
                "runtime_variant_role": ability.get("variant_role"),
                "runtime_parent_ability_id": ability.get("parent_ability_id"),
                "runtime_evidence": {
                    "base_to_child_transitions": base_to_child,
                    "child_to_base_transitions": child_to_base,
                    "dynamic_button_swap_hits": swap_hits,
                    "unit_type_contexts": dict(identity_contexts.get(ident, {})),
                },
                "source": "CURRENT_REPLAY_RUNTIME_STATE",
            })
        extra_repeated = [(ident, n) for ident, n in repeated[2:] if n >= 2]
        if extra_repeated:
            residual = {**row}
            residual["reason"] = "RUNTIME_NUMERIC_VARIANT_UNMAPPED"
            residual["resolved_ability_ids"] = [base.get("ability_id"), child.get("ability_id")]
            residual["numeric_candidates"] = [
                {"abil_link": ident[0], "cmd_index": ident[1], "matches": n,
                 "ratio": round(n / total, 6) if total else 0.0}
                for ident, n in extra_repeated
            ]
            remaining.append(residual)

        diagnostics.append({
            "replay": replay_name,
            "replay_build": replay_build,
            "player_id": pid,
            "hero": hero,
            "slot": slot,
            "classification": "PARENT_SUBABILITY_VARIANT_RESOLVED",
            "base_ability_id": base.get("ability_id"),
            "subability_id": child.get("ability_id"),
            "base_numeric": {"abil_link": base_ident[0], "cmd_index": base_ident[1]},
            "subability_numeric": {"abil_link": child_ident[0], "cmd_index": child_ident[1]},
            "base_to_child_transitions": base_to_child,
            "dynamic_button_swap_hits": swap_hits,
            "confidence": confidence,
        })
    return resolved, remaining, diagnostics



def cross_validate_runtime_variants(
    observations: list[dict],
    unresolved_rows: list[dict],
    diagnostics: list[dict],
) -> tuple[list[dict], list[dict], list[dict], dict]:
    """Cross-replay validation for v1.4 runtime parent/subability proposals.

    Local round-trip patterns are useful candidate evidence, but a nearby generic command
    (notably numeric identities such as 26/111 in the current corpus) can repeat often enough
    inside one replay to imitate a recast/cancel state machine.  v1.4.1 therefore promotes a
    runtime family only when its oriented numeric pair is reproducible across replays of the
    same build+hero+slot and the numeric identities are not globally promiscuous within that
    build.  Everything else fails closed back to unresolved.
    """
    runtime_obs = [o for o in observations if str(o.get("source") or "") == "CURRENT_REPLAY_RUNTIME_STATE"]
    direct_obs = [o for o in observations if str(o.get("source") or "") != "CURRENT_REPLAY_RUNTIME_STATE"]

    # Numeric identities that occur for many different heroes in one build are command-layer
    # collisions, not discriminative ability identities.  Threshold 3 is intentionally
    # conservative: two-hero reuse remains evidence, while corpus-wide 26/111-style ids are
    # rejected.
    numeric_heroes: dict[tuple, set[str]] = defaultdict(set)
    numeric_abilities: dict[tuple, set[str]] = defaultdict(set)
    for o in observations:
        if o.get("replay_build") is None or o.get("abil_link") is None:
            continue
        nk = (o.get("replay_build"), o.get("abil_link"), o.get("cmd_index"))
        if o.get("hero"):
            numeric_heroes[nk].add(str(o.get("hero")))
        if o.get("ability_id"):
            numeric_abilities[nk].add(str(o.get("ability_id")))
    promiscuous = {
        nk for nk in set(numeric_heroes) | set(numeric_abilities)
        if len(numeric_heroes.get(nk, set())) >= 3 or len(numeric_abilities.get(nk, set())) >= 5
    }

    resolved_diags = [d for d in diagnostics if d.get("classification") == "PARENT_SUBABILITY_VARIANT_RESOLVED"]
    other_diags = [d for d in diagnostics if d.get("classification") != "PARENT_SUBABILITY_VARIANT_RESOLVED"]
    families: dict[tuple, list[dict]] = defaultdict(list)
    for d in resolved_diags:
        fam = (
            d.get("replay_build"), d.get("hero"), d.get("slot"),
            d.get("base_ability_id"), d.get("subability_id"),
        )
        families[fam].append(d)

    accepted_runtime_keys: set[tuple] = set()
    out_diags = list(other_diags)
    rejected_unresolved: list[dict] = []
    stats = Counter()

    for fam, rows in families.items():
        build, hero, slot, base_id, child_id = fam
        pair_counts: Counter = Counter()
        pair_replays: dict[tuple, set[str]] = defaultdict(set)
        rejected_promiscuous_rows = 0
        for d in rows:
            b = d.get("base_numeric") or {}
            c = d.get("subability_numeric") or {}
            bp = (b.get("abil_link"), b.get("cmd_index"))
            cp = (c.get("abil_link"), c.get("cmd_index"))
            if (build, bp[0], bp[1]) in promiscuous or (build, cp[0], cp[1]) in promiscuous:
                rejected_promiscuous_rows += 1
                continue
            pair = (bp, cp)
            pair_counts[pair] += 1
            pair_replays[pair].add(str(d.get("replay")))

        accepted_pair = None
        reason = None
        if not pair_counts:
            reason = "PROMISCUOUS_NUMERIC_IDENTITY"
        else:
            ranked = pair_counts.most_common()
            best_pair, best_n = ranked[0]
            second_n = ranked[1][1] if len(ranked) > 1 else 0
            total_clean = sum(pair_counts.values())
            best_share = best_n / total_clean if total_clean else 0.0
            distinct_replays = len(pair_replays.get(best_pair, set()))
            # Cross-replay reproducibility plus a clear orientation majority.  A 65% share and
            # >=2 replay margin accepts patterns like Falstad 9:4 while rejecting 1:1 flips.
            if distinct_replays >= 2 and best_n >= 2 and best_share >= 0.65 and (best_n - second_n) >= 2:
                accepted_pair = best_pair
            elif distinct_replays < 2:
                reason = "CROSS_REPLAY_EVIDENCE_INSUFFICIENT"
            else:
                reason = "CROSS_REPLAY_ORIENTATION_AMBIGUOUS"

        for d in rows:
            b = d.get("base_numeric") or {}
            c = d.get("subability_numeric") or {}
            pair = ((b.get("abil_link"), b.get("cmd_index")), (c.get("abil_link"), c.get("cmd_index")))
            if accepted_pair is not None and pair == accepted_pair:
                d2 = dict(d)
                d2["classification"] = "PARENT_SUBABILITY_VARIANT_CONFIRMED_CROSS_REPLAY"
                d2["cross_replay_family_support"] = pair_counts[accepted_pair]
                d2["cross_replay_family_total_clean"] = sum(pair_counts.values())
                d2["cross_replay_distinct_replays"] = len(pair_replays[accepted_pair])
                out_diags.append(d2)
                for ident, aid in ((accepted_pair[0], base_id), (accepted_pair[1], child_id)):
                    accepted_runtime_keys.add((
                        str(d.get("replay")), str(d.get("player_id")), build, hero, slot,
                        ident[0], ident[1], aid,
                    ))
                stats["confirmed_groups"] += 1
            else:
                d2 = dict(d)
                numeric_promiscuous = any(
                    (build, x.get("abil_link"), x.get("cmd_index")) in promiscuous
                    for x in (b, c)
                )
                if numeric_promiscuous:
                    d2["classification"] = "PARENT_SUBABILITY_VARIANT_REJECTED_PROMISCUOUS_NUMERIC"
                    stats["rejected_promiscuous_groups"] += 1
                elif accepted_pair is not None:
                    d2["classification"] = "PARENT_SUBABILITY_VARIANT_REJECTED_NONCONSENSUS_PAIR"
                    stats["rejected_nonconsensus_groups"] += 1
                else:
                    d2["classification"] = "PARENT_SUBABILITY_VARIANT_REJECTED_" + str(reason or "GLOBAL_EVIDENCE_INSUFFICIENT")
                    stats["rejected_insufficient_groups"] += 1
                if accepted_pair is not None:
                    d2["accepted_family_pair"] = {
                        "base": {"abil_link": accepted_pair[0][0], "cmd_index": accepted_pair[0][1]},
                        "subability": {"abil_link": accepted_pair[1][0], "cmd_index": accepted_pair[1][1]},
                    }
                out_diags.append(d2)

                rejected_unresolved.append({
                    "replay": d.get("replay"),
                    "replay_build": build,
                    "player_id": d.get("player_id"),
                    "hero": hero,
                    "slot": slot,
                    "reason": "RUNTIME_VARIANT_GLOBAL_EVIDENCE_INSUFFICIENT",
                    "base_ability_id": base_id,
                    "subability_id": child_id,
                    "numeric_candidates": [
                        {"abil_link": b.get("abil_link"), "cmd_index": b.get("cmd_index"), "proposed_ability_id": base_id},
                        {"abil_link": c.get("abil_link"), "cmd_index": c.get("cmd_index"), "proposed_ability_id": child_id},
                    ],
                    "global_reason": d2["classification"],
                })

    filtered_runtime = []
    for o in runtime_obs:
        k = (
            str(o.get("replay")), str(o.get("player_id")), o.get("replay_build"), o.get("hero"), o.get("slot"),
            o.get("abil_link"), o.get("cmd_index"), o.get("ability_id"),
        )
        if k in accepted_runtime_keys:
            o2 = dict(o)
            o2["confidence"] = "HIGH"
            o2["canonical_resolution_method"] = str(o2.get("canonical_resolution_method") or "") + "+CROSS_REPLAY_VALIDATED"
            filtered_runtime.append(o2)
    stats["accepted_runtime_observations"] = len(filtered_runtime)
    stats["rejected_runtime_observations"] = len(runtime_obs) - len(filtered_runtime)
    stats["promiscuous_numeric_identities"] = len(promiscuous)
    stats["runtime_families"] = len(families)
    return direct_obs + filtered_runtime, unresolved_rows + rejected_unresolved, out_diags, dict(stats)

def resolve_core_bindings(players: dict[str, dict], schema: AbilitySchemaIndex, replay_name: str, replay_build: Optional[int], pairs: list[dict]) -> tuple[list[dict], list[dict]]:
    """Resolve canonical slot once, but retain *all* observed numeric variants for that slot.

    v1.1 kept only the dominant abil_link/cmd_index per replay+slot. That hid legitimate
    runtime numeric variants and made them look like cross-replay conflicts later.
    """
    counts: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for p in pairs:
        slot = p.get("slot")
        if slot:
            counts[(p["player_id"], slot)][(p["abil_link"], p["cmd_index"])] += 1

    resolved, unresolved = [], []
    slot_to_key = {v: k for k, v in CORE_KEY_TO_SLOT.items()}
    for (pid, slot), counter in counts.items():
        player = players.get(pid)
        if not player:
            unresolved.append({
                "replay": replay_name, "replay_build": replay_build, "player_id": pid,
                "slot": slot, "key_code": slot_to_key.get(slot), "reason": "PLAYER_UNRESOLVED",
                "numeric_candidates": [[a, c, n] for (a, c), n in counter.most_common()],
            })
            continue
        hero_name = schema.canonical_hero(player.get("hero"), player.get("hero_id"))
        if not hero_name:
            unresolved.append({
                "replay": replay_name, "replay_build": replay_build, "player_id": pid,
                "player_name": player.get("name"), "slot": slot, "key_code": slot_to_key.get(slot),
                "reason": "HERO_UNRESOLVED",
                "raw_hero": player.get("hero"), "raw_hero_id": player.get("hero_id"),
                "numeric_candidates": [[a, c, n] for (a, c), n in counter.most_common()],
            })
            continue

        total = sum(counter.values())
        ability, method = schema.resolve_core_slot(hero_name, slot, player.get("talents", []))
        if not ability:
            unresolved.append({
                "replay": replay_name, "replay_build": replay_build, "player_id": pid,
                "player_name": player.get("name"), "hero": hero_name, "slot": slot,
                "key_code": slot_to_key.get(slot), "reason": method,
                "canonical_candidates": schema.slot_candidates(hero_name, slot),
                "numeric_candidates": [
                    {"abil_link": a, "cmd_index": c, "matches": n, "ratio": round(n / total, 6) if total else 0.0}
                    for (a, c), n in counter.most_common()
                ],
            })
            continue

        for rank, ((abil_link, cmd_index), matches) in enumerate(counter.most_common(), start=1):
            resolved.append({
                "replay": replay_name,
                "replay_build": replay_build,
                "player_id": pid,
                "player_name": player.get("name"),
                "hero": hero_name,
                "slot": slot,
                "key_code": slot_to_key.get(slot),
                "abil_link": abil_link,
                "cmd_index": cmd_index,
                "matches": matches,
                "total_slot_pairs": total,
                "ratio": round(matches / total, 6) if total else 0.0,
                "confidence": confidence_for_binding(matches, total),
                "canonical_resolution_method": method,
                "runtime_context": (f"SELECTED_HEROIC:{ability.get('ability_id')}" if slot == "R" and method == "SELECTED_HEROIC" else None),
                "ability_id": ability.get("ability_id"),
                "ability_name": ability.get("name"),
                "button_id": ability.get("button_id"),
                "category": ability.get("category"),
                "numeric_variant_rank": rank,
            })
    return resolved, unresolved


def resolve_active_talent_candidates(players: dict[str, dict], schema: AbilitySchemaIndex, replay_name: str, replay_build: Optional[int], pairs: list[dict]) -> list[dict]:
    noncore: dict[str, list[dict]] = defaultdict(list)
    for p in pairs:
        if p.get("slot") is None:
            noncore[p["player_id"]].append(p)
    result = []
    for pid, player in players.items():
        hero_name = schema.canonical_hero(player.get("hero"), player.get("hero_id"))
        if not hero_name:
            continue
        for picked in schema.active_talents(hero_name, player.get("talents", [])):
            pick_time = picked.get("time")
            if pick_time is None:
                continue
            talent = picked["talent"]
            before = Counter((p["key_code"], p["abil_link"], p["cmd_index"]) for p in noncore.get(pid, []) if p["time"] < pick_time)
            after = Counter((p["key_code"], p["abil_link"], p["cmd_index"]) for p in noncore.get(pid, []) if p["time"] >= pick_time)
            new_identities = [(identity, count) for identity, count in after.most_common() if count >= 2 and before[identity] == 0]
            if not new_identities:
                continue
            best, count = new_identities[0]
            tied = len(new_identities) > 1 and new_identities[1][1] == count
            result.append({
                "replay": replay_name,
                "replay_build": replay_build,
                "player_id": pid,
                "player_name": player.get("name"),
                "hero": hero_name,
                "talent_id": talent.get("talent_id"),
                "talent_name": talent.get("name"),
                "talent_pick_time": pick_time,
                "active_ability_id": talent.get("ability_id"),
                "active_button_id": talent.get("button_id"),
                "key_code_candidate": best[0],
                "abil_link_candidate": best[1],
                "cmd_index_candidate": best[2],
                "matches_after_pick": count,
                "status": "AMBIGUOUS_CANDIDATE" if tied else "CANDIDATE",
                "confidence": "MEDIUM" if count >= 3 and not tied else "LOW",
                "reason": "NEW_NONCORE_NUMERIC_COMMAND_AFTER_SELECTED_ACTIVE_TALENT",
            })
    return result


def event_signature(value: Any, depth: int = 0) -> Any:
    if depth >= 4:
        return type(value).__name__
    if isinstance(value, dict):
        return {str(k): event_signature(v, depth + 1) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, list):
        return [event_signature(value[0], depth + 1)] if value else []
    return type(value).__name__


def _primitive_leaves(value: Any, path: str = "$", depth: int = 0, max_depth: int = 5) -> Iterable[tuple[str, Any]]:
    if depth > max_depth:
        return
    if isinstance(value, dict):
        for k, v in value.items():
            yield from _primitive_leaves(v, f"{path}.{k}", depth + 1, max_depth)
    elif isinstance(value, list):
        for i, v in enumerate(value[:16]):
            yield from _primitive_leaves(v, f"{path}[]", depth + 1, max_depth)
    elif isinstance(value, (str, int, float, bool)) or value is None:
        yield path, value


def _semantic_value_key(path: str, value: Any) -> bool:
    # Strings are especially valuable in StatGameEvent / unit events even when the old
    # decoder replaced field names with numeric keys. Numeric values are retained only
    # for paths that look identity/command-like to avoid collecting coordinate noise.
    if isinstance(value, str):
        return 0 < len(value) <= 160
    p = path.lower()
    hints = ("flag", "type", "unit", "upgrade", "talent", "button", "link", "index", "id", "state", "cmd")
    return isinstance(value, (int, bool)) and any(h in p for h in hints)


def inventory_replay_observables(
    replay: dict,
    replay_name: str,
    replay_build: Optional[int],
    signature_samples: int = DEFAULT_OBSERVABLE_SIGNATURE_SAMPLES,
    value_samples: int = DEFAULT_OBSERVABLE_VALUE_SAMPLES,
) -> dict:
    """Inventory event shapes plus bounded semantic value samples.

    The value inventory is deliberately bounded. Its purpose is to discover protocol
    semantics (stat names, unit types, upgrade/talent identifiers, command flags, etc.),
    not to duplicate the replay or collect every coordinate.
    """
    streams = {}
    for stream_name, events in replay.items():
        if not isinstance(events, list) or not events:
            continue
        sample = events[: min(len(events), 12)]
        if not sample or not all(isinstance(x, dict) for x in sample):
            continue
        by_type: dict[str, dict] = {}
        for e in events:
            if not isinstance(e, dict):
                continue
            event_type = str(e.get("GameEventType") or e.get("TrackerEventType") or e.get("MessageEventType") or e.get("EventType") or e.get("_event") or "UNKNOWN")
            rec = by_type.setdefault(event_type, {
                "count": 0, "top_level_fields": set(), "data_signatures": Counter(), "signature_samples": 0,
                "semantic_values": defaultdict(Counter), "value_samples": 0,
            })
            rec["count"] += 1
            rec["top_level_fields"].update(str(k) for k in e.keys())
            data = None
            if "Data" in e:
                data = parse_embedded_json(e.get("Data"))
            elif any(str(k).startswith("m_") for k in e):
                data = {k: v for k, v in e.items() if str(k).startswith("m_")}

            if rec["signature_samples"] < signature_samples and data not in ({}, None):
                sig = orjson.dumps(event_signature(data), option=orjson.OPT_SORT_KEYS).decode("utf-8")
                rec["data_signatures"][sig] += 1
                rec["signature_samples"] += 1

            if rec["value_samples"] < value_samples:
                payload = data if data not in ({}, None) else e
                for path, value in _primitive_leaves(payload):
                    if _semantic_value_key(path, value):
                        key = str(value)
                        if len(rec["semantic_values"][path]) < 128 or key in rec["semantic_values"][path]:
                            rec["semantic_values"][path][key] += 1
                # Explicit command flags are useful even in numeric-key legacy Data.
                if event_type.endswith("sCmdEvent") or event_type.endswith("SCmdEvent"):
                    cmd = parse_cmd_event(e)
                    if cmd and cmd.get("cmd_flags") is not None:
                        rec["semantic_values"]["$.__cmd_flags__"][str(cmd["cmd_flags"])] += 1
                    if cmd and cmd.get("abil_link") is not None:
                        rec["semantic_values"]["$.__abil_link__"][str(cmd["abil_link"])] += 1
                rec["value_samples"] += 1

        out_types = {}
        for typ, rec in by_type.items():
            sem = {}
            for path, counts in rec["semantic_values"].items():
                sem[path] = [{"value": v, "count": n} for v, n in counts.most_common(32)]
            out_types[typ] = {
                "count": rec["count"],
                "top_level_fields": sorted(rec["top_level_fields"]),
                "data_signatures": [{"signature": orjson.loads(sig), "count": n} for sig, n in rec["data_signatures"].most_common(10)],
                "signature_samples": rec["signature_samples"],
                "semantic_value_samples": rec["value_samples"],
                "semantic_values": sem,
            }
        streams[stream_name] = out_types
    return {"replay": replay_name, "replay_build": replay_build, "streams": streams}


def normalize_core_command_events(players: dict[str, dict], schema: AbilitySchemaIndex, pairs: list[dict], bindings: list[dict], replay_name: str, replay_build: Optional[int]) -> list[dict]:
    """Keep only Q/W/E/R/D commands and resolve by numeric identity when variants coexist."""
    by_numeric: dict[tuple[str, str, Any, Any], dict] = {}
    by_slot_rows: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for b in bindings:
        by_numeric[(b["player_id"], b["slot"], b.get("abil_link"), b.get("cmd_index"))] = b
        by_slot_rows[(b["player_id"], b["slot"])].append(b)
    slot_fallback: dict[tuple[str, str], dict] = {}
    for key, rows in by_slot_rows.items():
        ability_ids = {r.get("ability_id") for r in rows if r.get("ability_id")}
        if len(ability_ids) == 1:
            slot_fallback[key] = rows[0]
    out = []
    for p in sorted((x for x in pairs if x.get("slot")), key=lambda x: (x["time"], x["player_id"])):
        player = players.get(p["player_id"], {})
        hero = schema.canonical_hero(player.get("hero"), player.get("hero_id")) or player.get("hero")
        b = by_numeric.get((p["player_id"], p["slot"], p.get("abil_link"), p.get("cmd_index")))
        if b is None:
            b = slot_fallback.get((p["player_id"], p["slot"]))
        out.append({
            "event_type": "ABILITY_COMMAND",
            "time": p["time"],
            "replay": replay_name,
            "replay_build": replay_build,
            "player_id": p["player_id"],
            "player_name": player.get("name"),
            "hero": hero,
            "key_code": p["key_code"],
            "slot": p.get("slot"),
            "raw_ability": {"abil_link": p["abil_link"], "cmd_index": p["cmd_index"], "cmd_flags": p["cmd_flags"]},
            "target_payload": p.get("target_payload"),
            "sequence": p.get("sequence"),
            "pairing": {"dt": p["dt"], "confidence": "HIGH" if p["dt"] <= 0.02 else "MEDIUM"},
            "canonical": ({"ability_id": b.get("ability_id"), "ability_name": b.get("ability_name"), "button_id": b.get("button_id"), "confidence": b.get("confidence"), "method": b.get("canonical_resolution_method"), "runtime_context": b.get("runtime_context")} if b else None),
            "status": "RESOLVED_CORE" if b else "UNRESOLVED_CORE_VARIANT",
        })
    return out


def summarize_noncore_pairs(players: dict[str, dict], schema: AbilitySchemaIndex, pairs: list[dict], replay_name: str, replay_build: Optional[int]) -> list[dict]:
    counts = Counter()
    for p in pairs:
        if p.get("slot") is not None:
            continue
        player = players.get(p["player_id"], {})
        hero = schema.canonical_hero(player.get("hero"), player.get("hero_id")) or player.get("hero")
        counts[(p["player_id"], hero, p.get("key_code"), p.get("abil_link"), p.get("cmd_index"), p.get("cmd_flags"))] += 1
    return [
        {
            "replay": replay_name, "replay_build": replay_build, "player_id": pid, "hero": hero,
            "key_code": key, "abil_link": abil, "cmd_index": cmd, "cmd_flags": flags, "count": count,
        }
        for (pid, hero, key, abil, cmd, flags), count in counts.most_common()
    ]


def import_legacy_seed(path: Path) -> dict:
    d = load_json(path)
    obs = []
    for r in d.get("core_bindings", []) or []:
        obs.append({
            "source": "LEGACY_EMPIRICAL_REPORT",
            "replay": r.get("replay"),
            "replay_build": None,
            "player_id": r.get("player_id"),
            "hero": r.get("hero"),
            "slot": r.get("slot"),
            "key_code": r.get("key_code"),
            "abil_link": r.get("abil_link"),
            "cmd_index": r.get("cmd_index"),
            "matches": r.get("matches", 0),
            "total_slot_pairs": r.get("total_slot_pairs", 0),
            "ratio": r.get("ratio"),
            "confidence": r.get("replay_confidence") or r.get("confidence"),
            "ability_id": r.get("ability_id"),
            "ability_name": r.get("ability_name"),
            "button_id": r.get("button_id"),
            "category": r.get("category"),
            "canonical_resolution_method": r.get("canonical_resolution_method"),
        })
    return {
        "metadata": {
            "source": str(path),
            "build_status": "UNKNOWN_IN_LEGACY_REPORT",
            "warning": "Imported observations are evidence only; numeric links are not treated as globally stable across builds.",
        },
        "observations": obs,
        "active_talent_candidates": d.get("active_talent_candidates", []) or [],
        "unresolved": d.get("unresolved", []) or [],
    }


def _runtime_context_for_observation(o: dict) -> Optional[str]:
    context = o.get("runtime_context")
    if context:
        return str(context)
    if (
        o.get("slot") == "R"
        and o.get("ability_id")
        and o.get("canonical_resolution_method") == "SELECTED_HEROIC"
    ):
        return f"SELECTED_HEROIC:{o.get('ability_id')}"
    return None


def _binding_row_trust(g: dict) -> tuple[bool, str]:
    """Promote only direct HIGH/MEDIUM evidence or repeated rank-1 consensus.

    Failed/ignored key presses can accidentally pair with the next unrelated sCmdEvent.
    Those artifacts overwhelmingly appear as one-off LOW variants.  We keep them in
    diagnostic aggregates, but do not let them enter the authoritative protocol DB.
    """
    direct = int(g["confidences"].get("HIGH", 0)) + int(g["confidences"].get("MEDIUM", 0)) > 0
    if direct:
        return True, "DIRECT_HIGH_MEDIUM_EVIDENCE"
    consensus = (
        len(g["rank1_replays"]) >= 2
        and len(g["replays"]) >= 2
        and int(g["matches"]) >= 10
    )
    if consensus:
        return True, "CROSS_REPLAY_RANK1_CONSENSUS"
    return False, "LOW_CONFIDENCE_ONLY"


def aggregate_active_talent_bindings(active_candidates: list[dict]) -> dict:
    """Aggregate active-talent candidates and reject generic command identities.

    A post-pick command is not enough to prove an active-talent binding. Generic commands
    such as movement/mount actions can also first appear after a talent pick.  A numeric
    identity is therefore trusted only when, within one build, it is not claimed by
    multiple different active abilities. Key codes are retained as observed hotbar/user
    variants rather than treated as a global constant.
    """
    numeric_to_abilities: dict[tuple, set[str]] = defaultdict(set)
    for c in active_candidates:
        aid = c.get("active_ability_id")
        if aid and c.get("abil_link_candidate") is not None and c.get("cmd_index_candidate") is not None:
            numeric_to_abilities[(c.get("replay_build"), c.get("abil_link_candidate"), c.get("cmd_index_candidate"))].add(str(aid))

    groups: dict[tuple, dict] = {}
    for c in active_candidates:
        key = (
            c.get("replay_build"), c.get("hero"), c.get("talent_id"), c.get("active_ability_id"),
            c.get("active_button_id"), c.get("abil_link_candidate"), c.get("cmd_index_candidate"),
        )
        g = groups.setdefault(key, {
            "replay_build": c.get("replay_build"), "hero": c.get("hero"), "talent_id": c.get("talent_id"),
            "talent_name": c.get("talent_name"), "active_ability_id": c.get("active_ability_id"),
            "active_button_id": c.get("active_button_id"), "abil_link": c.get("abil_link_candidate"),
            "cmd_index": c.get("cmd_index_candidate"), "replays": set(), "matches": 0,
            "key_codes": Counter(), "confidences": Counter(),
        })
        if c.get("replay"):
            g["replays"].add(str(c.get("replay")))
        g["matches"] += int(c.get("matches_after_pick") or 0)
        if c.get("key_code_candidate") is not None:
            g["key_codes"][str(c.get("key_code_candidate"))] += 1
        if c.get("confidence"):
            g["confidences"][str(c.get("confidence"))] += 1

    trusted, diagnostics = [], []
    for g in groups.values():
        identity = (g["replay_build"], g["abil_link"], g["cmd_index"])
        claimed = sorted(numeric_to_abilities.get(identity, set()))
        collision = len(claimed) > 1
        distinct_replays = len(g["replays"])
        if collision:
            status = "REJECTED_GENERIC_NUMERIC_COLLISION"
            confidence = "LOW"
            reason = "NUMERIC_IDENTITY_CANDIDATE_FOR_MULTIPLE_ACTIVE_ABILITIES"
        elif distinct_replays >= 2 and g["matches"] >= 4:
            status = "CONFIRMED_ACTIVE_BINDING"
            confidence = "HIGH"
            reason = "UNIQUE_NUMERIC_IDENTITY_REPEATED_ACROSS_REPLAYS"
        elif g["confidences"].get("MEDIUM", 0) > 0:
            status = "PROVISIONAL_ACTIVE_BINDING"
            confidence = "MEDIUM"
            reason = "UNIQUE_NUMERIC_IDENTITY_SINGLE_REPLAY_OR_LIMITED_EVIDENCE"
        else:
            status = "DIAGNOSTIC_ACTIVE_CANDIDATE"
            confidence = "LOW"
            reason = "INSUFFICIENT_REPEATED_EVIDENCE"

        row = {
            "replay_build": g["replay_build"], "hero": g["hero"], "talent_id": g["talent_id"],
            "talent_name": g["talent_name"], "active_ability_id": g["active_ability_id"],
            "active_button_id": g["active_button_id"], "abil_link": g["abil_link"], "cmd_index": g["cmd_index"],
            "distinct_replays": distinct_replays, "matches_after_pick": g["matches"],
            "observed_key_codes": [
                {"key_code": int(k) if str(k).lstrip("-").isdigit() else k, "replays": n}
                for k, n in g["key_codes"].most_common()
            ],
            "candidate_confidence_counts": dict(g["confidences"]),
            "numeric_identity_claimed_by": claimed,
            "status": status, "confidence": confidence, "reason": reason,
        }
        (trusted if confidence in {"HIGH", "MEDIUM"} else diagnostics).append(row)

    trusted.sort(key=lambda r: (r["replay_build"] is None, r["replay_build"] or 0, str(r["hero"]), str(r["talent_id"]), r["abil_link"] or -1))
    diagnostics.sort(key=lambda r: (r["replay_build"] is None, r["replay_build"] or 0, str(r["hero"]), str(r["talent_id"]), r["abil_link"] or -1))
    return {
        "bindings": trusted,
        "diagnostics": diagnostics,
        "confirmed": sum(r["status"] == "CONFIRMED_ACTIVE_BINDING" for r in trusted),
        "provisional": sum(r["status"] == "PROVISIONAL_ACTIVE_BINDING" for r in trusted),
        "rejected_or_low": len(diagnostics),
    }


def aggregate_protocol_db(observations: Iterable[dict], active_candidates: list[dict], source_replays: list[dict], pair_window: float) -> dict:
    groups: dict[tuple, dict] = {}
    for o in observations:
        runtime_context = _runtime_context_for_observation(o)
        key = (
            o.get("replay_build"), o.get("hero"), o.get("slot"), o.get("key_code"), runtime_context,
            o.get("abil_link"), o.get("cmd_index"), o.get("ability_id"),
        )
        g = groups.setdefault(key, {
            "replay_build": o.get("replay_build"), "hero": o.get("hero"), "slot": o.get("slot"), "key_code": o.get("key_code"),
            "runtime_context": runtime_context, "abil_link": o.get("abil_link"), "cmd_index": o.get("cmd_index"),
            "ability_id": o.get("ability_id"), "ability_name": o.get("ability_name"), "button_id": o.get("button_id"),
            "observations": 0, "matches": 0, "slot_pairs": 0, "replays": set(), "rank1_replays": set(),
            "confidences": Counter(), "sources": set(), "methods": set(),
            "runtime_variant_roles": set(), "runtime_parent_ability_ids": set(),
        })
        g["observations"] += 1
        g["matches"] += int(o.get("matches") or 0)
        g["slot_pairs"] += int(o.get("total_slot_pairs") or 0)
        if o.get("replay"):
            g["replays"].add(str(o["replay"]))
            if o.get("numeric_variant_rank") == 1:
                g["rank1_replays"].add(str(o["replay"]))
        if o.get("confidence"):
            g["confidences"][str(o["confidence"])] += 1
        g["sources"].add(str(o.get("source") or "CURRENT_REPLAY"))
        if o.get("canonical_resolution_method"):
            g["methods"].add(str(o.get("canonical_resolution_method")))
        if o.get("runtime_variant_role"):
            g["runtime_variant_roles"].add(str(o.get("runtime_variant_role")))
        if o.get("runtime_parent_ability_id"):
            g["runtime_parent_ability_ids"].add(str(o.get("runtime_parent_ability_id")))

    trusted_bindings, diagnostic_bindings = [], []
    trusted_observation_keys = set()
    for key, g in groups.items():
        ratio = g["matches"] / g["slot_pairs"] if g["slot_pairs"] else None
        trusted, trust_reason = _binding_row_trust(g)
        build_known = g["replay_build"] is not None
        if trusted:
            if trust_reason == "CROSS_REPLAY_RANK1_CONSENSUS":
                status = "CONFIRMED_BY_CROSS_REPLAY_CONSENSUS" if build_known else "EMPIRICAL_BUILD_UNKNOWN_CONSENSUS"
            elif build_known and len(g["replays"]) >= 2 and g["matches"] >= 10 and (ratio or 0) >= 0.70:
                status = "CONFIRMED_BUILD_SCOPED"
            elif build_known:
                status = "PROVISIONAL_BUILD_SCOPED"
            else:
                status = "EMPIRICAL_BUILD_UNKNOWN"
        else:
            status = "DIAGNOSTIC_LOW_CONFIDENCE_ONLY"

        row = {
            "replay_build": g["replay_build"], "hero": g["hero"], "slot": g["slot"], "key_code": g["key_code"],
            "runtime_context": g["runtime_context"], "abil_link": g["abil_link"], "cmd_index": g["cmd_index"],
            "ability_id": g["ability_id"], "ability_name": g["ability_name"], "button_id": g["button_id"],
            "observation_count": g["observations"], "distinct_replays": len(g["replays"]),
            "rank1_replays": len(g["rank1_replays"]), "matches": g["matches"], "slot_pairs": g["slot_pairs"],
            "ratio": round(ratio, 6) if ratio is not None else None, "confidence_counts": dict(g["confidences"]),
            "sources": sorted(g["sources"]),
            "canonical_resolution_methods": sorted(g["methods"]),
            "runtime_variant_roles": sorted(g["runtime_variant_roles"]),
            "runtime_parent_ability_ids": sorted(g["runtime_parent_ability_ids"]),
            "status": status, "trust_reason": trust_reason,
        }
        if trusted:
            trusted_bindings.append(row)
            trusted_observation_keys.add(key)
        else:
            diagnostic_bindings.append(row)

    sort_key = lambda x: (x["replay_build"] is None, x["replay_build"] or 0, str(x["hero"]), str(x["slot"]), str(x.get("runtime_context") or ""), x["abil_link"] or -1, x["cmd_index"] or -1)
    trusted_bindings.sort(key=sort_key)
    diagnostic_bindings.sort(key=sort_key)

    # Variant/conflict analysis is performed ONLY on trusted evidence and within the
    # same runtime context. Different selected heroics are expected runtime variants.
    context_groups: dict[tuple, list[dict]] = defaultdict(list)
    heroic_slot_groups: dict[tuple, list[dict]] = defaultdict(list)
    for b in trusted_bindings:
        context_groups[(b["replay_build"], b["hero"], b["slot"], b["key_code"], b.get("runtime_context"))].append(b)
        if b.get("slot") == "R":
            heroic_slot_groups[(b["replay_build"], b["hero"], b["slot"], b["key_code"])].append(b)

    numeric_variants, canonical_variants = [], []
    build_unknown_collisions, true_conflicts, heroic_choice_variants = [], [], []
    for k, rows in context_groups.items():
        if len(rows) <= 1:
            continue
        numeric_ids = {(r.get("abil_link"), r.get("cmd_index")) for r in rows}
        ability_ids = {r.get("ability_id") for r in rows if r.get("ability_id")}
        base = {
            "replay_build": k[0], "hero": k[1], "slot": k[2], "key_code": k[3], "runtime_context": k[4],
            "variants": [
                {"abil_link": r.get("abil_link"), "cmd_index": r.get("cmd_index"), "ability_id": r.get("ability_id"),
                 "ability_name": r.get("ability_name"), "matches": r.get("matches"), "distinct_replays": r.get("distinct_replays"),
                 "status": r.get("status")}
                for r in rows
            ],
        }
        if len(numeric_ids) > 1 and len(ability_ids) == 1:
            numeric_variants.append({**base, "classification": "NUMERIC_VARIANT_SAME_ABILITY"})
        if len(ability_ids) > 1:
            canonical_variants.append({**base, "classification": "CANONICAL_VARIANT_SAME_RUNTIME_CONTEXT"})

        by_numeric: dict[tuple, set] = defaultdict(set)
        for r in rows:
            if r.get("ability_id"):
                by_numeric[(r.get("abil_link"), r.get("cmd_index"))].add(r.get("ability_id"))
        collisions = [
            {"abil_link": n[0], "cmd_index": n[1], "ability_ids": sorted(ids)}
            for n, ids in by_numeric.items() if len(ids) > 1
        ]
        if collisions:
            rec = {**base, "numeric_collisions": collisions}
            if k[0] is None:
                rec["classification"] = "BUILD_UNKNOWN_COLLISION"
                build_unknown_collisions.append(rec)
            else:
                rec["classification"] = "TRUE_CONFLICT_SAME_RUNTIME_CONTEXT"
                true_conflicts.append(rec)

    for k, rows in heroic_slot_groups.items():
        contexts = defaultdict(list)
        for r in rows:
            contexts[r.get("runtime_context")].append(r)
        ability_ids = {r.get("ability_id") for r in rows if r.get("ability_id")}
        if len(ability_ids) <= 1:
            continue
        heroic_choice_variants.append({
            "replay_build": k[0], "hero": k[1], "slot": k[2], "key_code": k[3],
            "classification": "SELECTED_HEROIC_CHOICE_VARIANT",
            "choices": [
                {
                    "runtime_context": context,
                    "ability_ids": sorted({r.get("ability_id") for r in rs if r.get("ability_id")}),
                    "numeric_variants": [
                        {"abil_link": r.get("abil_link"), "cmd_index": r.get("cmd_index"), "matches": r.get("matches"), "distinct_replays": r.get("distinct_replays")}
                        for r in rs
                    ],
                }
                for context, rs in sorted(contexts.items(), key=lambda kv: str(kv[0]))
            ],
        })

    active = aggregate_active_talent_bindings(active_candidates)

    return {
        "metadata": {
            "protocol_db_version": PROTOCOL_VERSION,
            "core_key_to_slot": {str(k): v for k, v in CORE_KEY_TO_SLOT.items()},
            "press_state": PRESS_STATE, "release_state": RELEASE_STATE,
            "pair_window_seconds": pair_window, "numeric_binding_scope": "BUILD_AND_RUNTIME_CONTEXT_SCOPED",
            "fail_closed": True, "json_engine": "orjson",
            "trusted_binding_policy": {
                "direct": "at least one HIGH or MEDIUM replay-level observation",
                "consensus": "LOW-only identity must be rank-1 in >=2 replays and have >=10 matched presses",
                "diagnostic": "all remaining LOW-only identities are excluded from core_bindings",
            },
            "notes": [
                "abil_link/cmd_index are raw replay protocol identities, not canonical ability IDs.",
                "core_bindings contains trusted evidence only; LOW-only noise is exported separately.",
                "Multiple numeric identities for one canonical ability are NUMERIC_VARIANT_SAME_ABILITY, not conflicts.",
                "Different selected heroics on R are SELECTED_HEROIC_CHOICE_VARIANT, not conflicts.",
                "TRUE_CONFLICT requires one build + hero + slot + runtime context + numeric identity mapping to multiple canonical abilities.",
                "Active-talent numeric identities claimed by multiple active abilities are rejected as generic command collisions.",
                "Runtime parent/subability mappings require cross-replay oriented-pair consensus; promiscuous numeric identities fail closed.",
            ],
        },
        "source_replays": source_replays,
        "core_bindings": trusted_bindings,
        "diagnostic_low_bindings": diagnostic_bindings,
        "trusted_observation_keys": trusted_observation_keys,
        "active_talent_bindings": active["bindings"],
        "active_talent_diagnostics": active["diagnostics"],
        "active_talent_summary": {k: active[k] for k in ("confirmed", "provisional", "rejected_or_low")},
        "numeric_variants": numeric_variants,
        "canonical_variants": canonical_variants,
        "heroic_choice_variants": heroic_choice_variants,
        "build_unknown_collisions": build_unknown_collisions,
        "true_conflicts": true_conflicts,
        "conflicts": true_conflicts,
    }


def discover_replay_inputs(explicit: list[str], replay_dirs: list[str], recursive: bool, output_dir: Path) -> list[Path]:
    files: dict[str, Path] = {}
    out_resolved = output_dir.resolve()
    allowed = {".json", ".stormreplay"}
    for raw in explicit:
        p = Path(raw).expanduser()
        if p.is_file() and p.suffix.lower() in allowed:
            files[str(p.resolve())] = p.resolve()
    for raw_dir in replay_dirs:
        root = Path(raw_dir).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            continue
        iterator = root.rglob("*") if recursive else root.glob("*")
        for p in iterator:
            if not p.is_file() or p.suffix.lower() not in allowed:
                continue
            rp = p.resolve()
            try:
                rp.relative_to(out_resolved)
                continue
            except ValueError:
                pass
            files[str(rp)] = rp
    return [files[k] for k in sorted(files)]


# v1.1 API compatibility for tests/scripts that imported the old helper.
def discover_replay_jsons(explicit: list[str], replay_dirs: list[str], recursive: bool, output_dir: Path) -> list[Path]:
    return [p for p in discover_replay_inputs(explicit, replay_dirs, recursive, output_dir) if p.suffix.lower() == ".json"]


def shard_name_for_path(path: Path) -> str:
    return hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:24] + ".json"


def load_manifest(path: Path) -> dict:
    if not path.exists():
        return {"version": PROTOCOL_VERSION, "files": {}}
    try:
        d = load_json(path)
        if isinstance(d, dict) and isinstance(d.get("files"), dict):
            return d
    except Exception:
        pass
    return {"version": PROTOCOL_VERSION, "files": {}}


def save_manifest(path: Path, manifest: dict) -> None:
    manifest["version"] = PROTOCOL_VERSION
    manifest["updated_unix"] = time.time()
    save_json(path, manifest)


def process_replay_object(
    replay: dict,
    schema: AbilitySchemaIndex,
    replay_name: str,
    replay_path: str,
    pair_window: float,
    signature_samples: int,
    value_samples: int = DEFAULT_OBSERVABLE_VALUE_SAMPLES,
    command_events_mode: str = "none",
) -> dict:
    build_info = extract_replay_build(replay)
    if build_info.get("build") is None and replay.get("__base_build__"):
        build_info = {
            "build": safe_int(replay.get("__base_build__")),
            "status": replay.get("__base_build_status__") or "OBSERVED_RAW_HEADER",
            "source_path": replay.get("__base_build_source__") or "$.__base_build__",
        }
    build = build_info["build"]
    players = replay_players(replay)
    pairs = pair_key_and_cmd_events(replay, pair_window)
    runtime_state = build_runtime_state_evidence(replay, players, schema)
    resolved, missing = resolve_core_bindings(players, schema, replay_name, build, pairs)
    for r in resolved:
        r["source"] = "CURRENT_REPLAY"
    runtime_resolved, missing, runtime_diagnostics = resolve_runtime_ambiguous_bindings(
        players, schema, replay_name, build, pairs, missing, runtime_state
    )
    resolved.extend(runtime_resolved)
    candidates = resolve_active_talent_candidates(players, schema, replay_name, build, pairs)
    observables = inventory_replay_observables(replay, replay_name, build, signature_samples, value_samples)
    core_events = normalize_core_command_events(players, schema, pairs, resolved, replay_name, build) if command_events_mode == "core" else []
    noncore_summary = summarize_noncore_pairs(players, schema, pairs, replay_name, build)
    return {
        "metadata": {
            "replay": replay_name,
            "file": replay_path,
            "source_format": replay.get("__source_format__") or "DECODED_JSON",
            "build": build,
            "build_status": build_info["status"],
            "build_source_path": build_info["source_path"],
            "players": len(players),
            "paired_commands": len(pairs),
            "core_bindings": len(resolved),
            "core_unresolved": len(missing),
            "runtime_variant_resolved": len(runtime_resolved),
            "runtime_state_events": len(runtime_state.get("events", [])),
            "active_talent_candidates": len(candidates),
            "core_command_events": len(core_events),
            "raw_noncore_unique": len(noncore_summary),
        },
        "observations": resolved,
        "unresolved": missing,
        "active_talent_candidates": candidates,
        "runtime_variant_diagnostics": runtime_diagnostics,
        "runtime_state_events": runtime_state.get("events", []),
        "observables": observables,
        "core_command_events": core_events,
        "raw_noncore_summary": noncore_summary,
    }


_WORKER_SCHEMA: Optional[AbilitySchemaIndex] = None
_WORKER_PAIR_WINDOW = DEFAULT_PAIR_WINDOW
_WORKER_SIGNATURE_SAMPLES = DEFAULT_OBSERVABLE_SIGNATURE_SAMPLES
_WORKER_VALUE_SAMPLES = DEFAULT_OBSERVABLE_VALUE_SAMPLES
_WORKER_COMMAND_EVENTS_MODE = "none"
_WORKER_STORM_DECODER = "heroesdecode"
_WORKER_HEROESDECODE_COMMAND: list[str] = []
_WORKER_HEROESDECODE_TIMEOUT = 180.0
_WORKER_KEEP_DECODED_DIR: Optional[str] = None


def _worker_init(
    identity_index_path: str, pair_window: float, signature_samples: int, value_samples: int, command_events_mode: str,
    storm_decoder: str = "heroesdecode", heroesdecode_command: Optional[list[str]] = None,
    heroesdecode_timeout: float = 180.0, keep_decoded_dir: Optional[str] = None,
) -> None:
    global _WORKER_SCHEMA, _WORKER_PAIR_WINDOW, _WORKER_SIGNATURE_SAMPLES, _WORKER_VALUE_SAMPLES, _WORKER_COMMAND_EVENTS_MODE
    global _WORKER_STORM_DECODER, _WORKER_HEROESDECODE_COMMAND, _WORKER_HEROESDECODE_TIMEOUT, _WORKER_KEEP_DECODED_DIR
    _WORKER_SCHEMA = AbilitySchemaIndex(load_json(Path(identity_index_path)))
    _WORKER_PAIR_WINDOW = pair_window
    _WORKER_SIGNATURE_SAMPLES = signature_samples
    _WORKER_VALUE_SAMPLES = value_samples
    _WORKER_COMMAND_EVENTS_MODE = command_events_mode
    _WORKER_STORM_DECODER = storm_decoder
    _WORKER_HEROESDECODE_COMMAND = list(heroesdecode_command or [])
    _WORKER_HEROESDECODE_TIMEOUT = float(heroesdecode_timeout)
    _WORKER_KEEP_DECODED_DIR = keep_decoded_dir


def _worker_process_file(path_str: str, shard_path_str: str) -> dict:
    start = time.perf_counter()
    path = Path(path_str)
    shard_path = Path(shard_path_str)
    try:
        if path.suffix.lower() == ".stormreplay":
            if _WORKER_STORM_DECODER == "heroesdecode":
                if not _WORKER_HEROESDECODE_COMMAND:
                    raise RuntimeError("HEROESDECODE_COMMAND_NOT_INITIALIZED")
                replay = decode_stormreplay_heroesdecode(
                    path, _WORKER_HEROESDECODE_COMMAND, _WORKER_HEROESDECODE_TIMEOUT,
                    Path(_WORKER_KEEP_DECODED_DIR) if _WORKER_KEEP_DECODED_DIR else None,
                )
            elif _WORKER_STORM_DECODER == "heroprotocol":
                replay = decode_stormreplay(path)
            else:
                raise RuntimeError(f"UNKNOWN_STORM_DECODER: {_WORKER_STORM_DECODER}")
        else:
            replay = load_json(path)
            if not is_decoded_replay_json(replay):
                return {"status": "SKIPPED_NON_REPLAY_JSON", "file": path_str, "elapsed": time.perf_counter() - start}
        assert _WORKER_SCHEMA is not None
        result = process_replay_object(
            replay, _WORKER_SCHEMA, path.name, path_str, _WORKER_PAIR_WINDOW,
            _WORKER_SIGNATURE_SAMPLES, _WORKER_VALUE_SAMPLES, _WORKER_COMMAND_EVENTS_MODE,
        )
        save_json(shard_path, result, pretty=False)
        return {"status": "PROCESSED", "file": path_str, "shard": shard_path_str, "metadata": result["metadata"], "elapsed": time.perf_counter() - start}
    except Exception as exc:
        return {"status": "FAILED", "file": path_str, "error": f"{type(exc).__name__}: {exc}", "elapsed": time.perf_counter() - start}


def iter_shards(manifest: dict, cache_dir: Path) -> Iterable[tuple[str, dict]]:
    for file_key, rec in sorted(manifest.get("files", {}).items()):
        if rec.get("status") != "PROCESSED":
            continue
        shard_name = rec.get("shard")
        if not shard_name:
            continue
        path = cache_dir / shard_name
        if not path.exists():
            continue
        try:
            yield file_key, load_json(path)
        except Exception:
            continue


def aggregate_observables(shards: Iterable[dict]) -> dict:
    streams: dict[str, dict[str, dict]] = {}
    replay_summaries = []
    for shard in shards:
        obs = shard.get("observables", {}) or {}
        replay_summaries.append({
            "replay": obs.get("replay"),
            "replay_build": obs.get("replay_build"),
            "streams": {k: sum(int(vv.get("count", 0)) for vv in (v or {}).values()) for k, v in (obs.get("streams", {}) or {}).items()},
        })
        for stream_name, types in (obs.get("streams", {}) or {}).items():
            stream = streams.setdefault(stream_name, {})
            for typ, rec in (types or {}).items():
                tgt = stream.setdefault(typ, {
                    "count": 0, "top_level_fields": set(), "data_signatures": Counter(),
                    "signature_samples": 0, "semantic_values": defaultdict(Counter), "semantic_value_samples": 0,
                })
                tgt["count"] += int(rec.get("count") or 0)
                tgt["top_level_fields"].update(rec.get("top_level_fields", []) or [])
                tgt["signature_samples"] += int(rec.get("signature_samples") or 0)
                tgt["semantic_value_samples"] += int(rec.get("semantic_value_samples") or 0)
                for sig_rec in rec.get("data_signatures", []) or []:
                    key = orjson.dumps(sig_rec.get("signature"), option=orjson.OPT_SORT_KEYS).decode("utf-8")
                    tgt["data_signatures"][key] += int(sig_rec.get("count") or 0)
                for path, vals in (rec.get("semantic_values", {}) or {}).items():
                    for item in vals or []:
                        tgt["semantic_values"][path][str(item.get("value"))] += int(item.get("count") or 0)
    out_streams = {}
    for stream_name, types in streams.items():
        out_streams[stream_name] = {}
        for typ, rec in types.items():
            sem = {
                path: [{"value": v, "count": n} for v, n in counts.most_common(64)]
                for path, counts in sorted(rec["semantic_values"].items())
            }
            out_streams[stream_name][typ] = {
                "count": rec["count"],
                "top_level_fields": sorted(rec["top_level_fields"]),
                "signature_samples": rec["signature_samples"],
                "data_signatures": [{"signature": orjson.loads(k), "count": n} for k, n in rec["data_signatures"].most_common(20)],
                "semantic_value_samples": rec["semantic_value_samples"],
                "semantic_values": sem,
            }
    return {"version": PROTOCOL_VERSION, "replay_count": len(replay_summaries), "streams": out_streams, "replay_summaries": replay_summaries}



def _stream_json_array_object(path: Path, header: dict, array_specs: list[tuple[str, Iterable[dict]]]) -> None:
    """Write large JSON arrays without collecting all rows in RAM."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    with tmp.open("wb") as f:
        f.write(b"{")
        first_field = True
        for key, value in header.items():
            if not first_field:
                f.write(b",")
            first_field = False
            f.write(orjson.dumps(str(key)))
            f.write(b":")
            f.write(orjson.dumps(value, option=orjson.OPT_NON_STR_KEYS))
        for key, iterable in array_specs:
            if not first_field:
                f.write(b",")
            first_field = False
            f.write(orjson.dumps(str(key)))
            f.write(b":[")
            first = True
            for row in iterable:
                if not first:
                    f.write(b",")
                first = False
                f.write(orjson.dumps(row, option=orjson.OPT_NON_STR_KEYS))
            f.write(b"]")
        f.write(b"}")
    os.replace(tmp, path)


def rows_from_shards(manifest: dict, cache_dir: Path, field: str) -> Iterable[dict]:
    for _, shard in iter_shards(manifest, cache_dir):
        for row in shard.get(field, []) or []:
            yield row


def aggregate_raw_noncore(manifest: dict, cache_dir: Path) -> dict:
    counts = Counter()
    for row in rows_from_shards(manifest, cache_dir, "raw_noncore_summary"):
        key = (
            row.get("replay_build"), row.get("hero"), row.get("key_code"), row.get("abil_link"),
            row.get("cmd_index"), row.get("cmd_flags"),
        )
        counts[key] += int(row.get("count") or 0)
    rows = [
        {
            "replay_build": k[0], "hero": k[1], "key_code": k[2], "abil_link": k[3],
            "cmd_index": k[4], "cmd_flags": k[5], "count": n,
        }
        for k, n in counts.most_common()
    ]
    return {"version": PROTOCOL_VERSION, "unique_signatures": len(rows), "total_events": sum(counts.values()), "signatures": rows}


def _aggregate_unresolved(rows: Iterable[dict]) -> tuple[list[dict], dict]:
    all_rows = list(rows)
    reasons = Counter(str(r.get("reason") or "UNKNOWN") for r in all_rows)
    hero_slot = Counter((str(r.get("hero") or r.get("raw_hero") or "UNKNOWN"), str(r.get("slot") or "?")) for r in all_rows)
    summary = {
        "count": len(all_rows),
        "reason_counts": dict(reasons.most_common()),
        "hero_slot_counts": [
            {"hero": h, "slot": s, "count": n} for (h, s), n in hero_slot.most_common()
        ],
    }
    return all_rows, summary


def build_outputs(
    outdir: Path,
    manifest: dict,
    cache_dir: Path,
    legacy_seed_path: Optional[Path],
    pair_window: float,
    run_metrics: dict,
    command_events_mode: str,
) -> dict:
    source_replays = []
    active_candidates = []
    current_obs_count = 0
    current_unresolved_count = 0
    core_event_count = 0
    runtime_variant_resolved_count = 0
    runtime_state_event_count = 0

    current_observations = []
    current_unresolved = []
    runtime_variant_diagnostics = []
    for _, shard in iter_shards(manifest, cache_dir):
        meta = shard.get("metadata", {}) or {}
        source_replays.append(meta)
        obs = shard.get("observations", []) or []
        current_observations.extend(obs)
        current_obs_count += len(obs)
        unresolved = shard.get("unresolved", []) or []
        current_unresolved.extend(unresolved)
        current_unresolved_count += len(unresolved)
        active_candidates.extend(shard.get("active_talent_candidates", []) or [])
        runtime_variant_diagnostics.extend(shard.get("runtime_variant_diagnostics", []) or [])
        runtime_variant_resolved_count += int((meta or {}).get("runtime_variant_resolved") or 0)
        runtime_state_event_count += len(shard.get("runtime_state_events", []) or [])
        core_event_count += len(shard.get("core_command_events", []) or [])

    # v1.4.1: local state-machine candidates are only promoted after cross-replay
    # consistency validation. This is aggregation-only and deliberately keeps the v1.4
    # shard schema reusable, so an existing 151-replay cache need not be decoded again.
    current_observations, current_unresolved, runtime_variant_diagnostics, runtime_global_validation = cross_validate_runtime_variants(
        current_observations, current_unresolved, runtime_variant_diagnostics
    )
    current_obs_count = len(current_observations)
    current_unresolved_count = len(current_unresolved)
    runtime_variant_resolved_count = int(runtime_global_validation.get("accepted_runtime_observations") or 0)

    legacy = {"observations": [], "active_talent_candidates": [], "unresolved": []}
    if legacy_seed_path:
        legacy = import_legacy_seed(legacy_seed_path)
        active_candidates.extend(legacy["active_talent_candidates"])

    all_observations = list(legacy["observations"]) + current_observations
    db = aggregate_protocol_db(all_observations, active_candidates, source_replays, pair_window)
    trusted_keys = db.pop("trusted_observation_keys")
    diagnostic_low_bindings = db.pop("diagnostic_low_bindings")
    active_diagnostics = db.pop("active_talent_diagnostics")
    save_json(outdir / "replay_protocol_db.json", db)
    repeatable_low = [
        r for r in diagnostic_low_bindings
        if int(r.get("distinct_replays") or 0) >= 2 or int(r.get("matches") or 0) >= 3
    ]
    low_by_slot = Counter(str(r.get("slot") or "?") for r in diagnostic_low_bindings)
    low_by_build = Counter(str(r.get("replay_build") if r.get("replay_build") is not None else "UNKNOWN") for r in diagnostic_low_bindings)
    save_json(outdir / "low_confidence_bindings.json", {
        "version": PROTOCOL_VERSION,
        "count_total": len(diagnostic_low_bindings),
        "count_repeatable_retained": len(repeatable_low),
        "count_oneoff_omitted": len(diagnostic_low_bindings) - len(repeatable_low),
        "policy": "LOW-only identities are excluded from authoritative core_bindings; only repeatable/3+ match diagnostics are materialized here, full evidence remains in replay cache shards",
        "by_slot": dict(low_by_slot),
        "by_build": dict(low_by_build),
        "repeatable_bindings": repeatable_low,
    })
    save_json(outdir / "active_talent_diagnostics.json", {
        "version": PROTOCOL_VERSION,
        "count": len(active_diagnostics),
        "diagnostics": active_diagnostics,
    })

    def obs_group_key(o: dict) -> tuple:
        return (
            o.get("replay_build"), o.get("hero"), o.get("slot"), o.get("key_code"), _runtime_context_for_observation(o),
            o.get("abil_link"), o.get("cmd_index"), o.get("ability_id"),
        )

    accepted_current_evidence = {
        (str(o.get("replay")), str(o.get("player_id")), obs_group_key(o)) for o in current_observations
    }

    def evidence_obs() -> Iterable[dict]:
        for o in legacy["observations"]:
            if obs_group_key(o) in trusted_keys:
                yield o
        for o in rows_from_shards(manifest, cache_dir, "observations"):
            exact = (str(o.get("replay")), str(o.get("player_id")), obs_group_key(o))
            if exact in accepted_current_evidence and obs_group_key(o) in trusted_keys:
                yield o

    _stream_json_array_object(
        outdir / "binding_evidence.json",
        {"version": PROTOCOL_VERSION, "scope": "TRUSTED_BINDINGS_ONLY"},
        [("observations", evidence_obs())],
    )

    unresolved_rows, unresolved_summary = _aggregate_unresolved([
        *legacy["unresolved"], *current_unresolved,
    ])
    _stream_json_array_object(
        outdir / "unresolved_bindings.json",
        {"version": PROTOCOL_VERSION, **unresolved_summary},
        [("rows", unresolved_rows)],
    )

    save_json(outdir / "active_talent_evidence.json", {
        "version": PROTOCOL_VERSION,
        "count": len(active_candidates),
        "candidates": active_candidates,
    })

    raw_summary = aggregate_raw_noncore(manifest, cache_dir)
    save_json(outdir / "raw_command_summary.json", raw_summary)

    if command_events_mode == "core":
        _stream_json_array_object(
            outdir / "core_command_events.json",
            {"version": PROTOCOL_VERSION},
            [("events", rows_from_shards(manifest, cache_dir, "core_command_events"))],
        )
    else:
        stale = outdir / "core_command_events.json"
        if stale.exists():
            stale.unlink()

    # Remove v1.1's huge noisy output if reusing the same output directory.
    stale_v11 = outdir / "normalized_command_events.json"
    if stale_v11.exists():
        stale_v11.unlink()

    save_json(outdir / "runtime_variant_diagnostics.json", {
        "version": PROTOCOL_VERSION,
        "count": len(runtime_variant_diagnostics),
        "classifications": dict(Counter(str(x.get("classification") or "UNKNOWN") for x in runtime_variant_diagnostics)),
        "global_validation": runtime_global_validation,
        "rows": runtime_variant_diagnostics,
    })
    _stream_json_array_object(
        outdir / "runtime_state_evidence.json",
        {"version": PROTOCOL_VERSION, "count": runtime_state_event_count,
         "policy": "Opaque button swap ids and unit type changes are evidence only; no undocumented semantic mapping is assumed."},
        [("events", rows_from_shards(manifest, cache_dir, "runtime_state_events"))],
    )

    observables = aggregate_observables(shard for _, shard in iter_shards(manifest, cache_dir))
    save_json(outdir / "replay_observables_db.json", observables)

    statuses = Counter(rec.get("status", "UNKNOWN") for rec in manifest.get("files", {}).values())
    build_statuses = Counter(str((m or {}).get("build_status") or "UNKNOWN") for m in source_replays)
    source_formats = Counter(str((m or {}).get("source_format") or "UNKNOWN") for m in source_replays)
    known_builds = Counter(str(m.get("build")) for m in source_replays if m.get("build") is not None)
    summary = {
        "protocol_db_version": PROTOCOL_VERSION,
        "json_engine": "orjson",
        "source_replays": len(source_replays),
        "source_formats": dict(source_formats),
        "replay_build_statuses": dict(build_statuses),
        "known_builds": dict(known_builds),
        "manifest_files": len(manifest.get("files", {})),
        "manifest_status_counts": dict(statuses),
        "core_binding_rows": len(db["core_bindings"]),
        "diagnostic_low_binding_rows": len(diagnostic_low_bindings),
        "diagnostic_low_repeatable_rows": len(repeatable_low),
        "diagnostic_low_oneoff_omitted": len(diagnostic_low_bindings) - len(repeatable_low),
        "current_observations": current_obs_count,
        "legacy_observations": len(legacy["observations"]),
        "active_talent_candidates": len(active_candidates),
        "active_talent_bindings": len(db.get("active_talent_bindings", [])),
        "active_talent_confirmed": db.get("active_talent_summary", {}).get("confirmed", 0),
        "active_talent_provisional": db.get("active_talent_summary", {}).get("provisional", 0),
        "active_talent_rejected_or_low": db.get("active_talent_summary", {}).get("rejected_or_low", 0),
        "unresolved": current_unresolved_count + len(legacy["unresolved"]),
        "unresolved_reason_counts": unresolved_summary["reason_counts"],
        "runtime_variant_resolved_rows": runtime_variant_resolved_count,
        "runtime_variant_resolution_groups": len(runtime_variant_diagnostics),
        "runtime_variant_global_validation": runtime_global_validation,
        "runtime_state_events": runtime_state_event_count,
        "numeric_variant_groups": len(db["numeric_variants"]),
        "canonical_variant_groups": len(db["canonical_variants"]),
        "heroic_choice_variant_groups": len(db.get("heroic_choice_variants", [])),
        "build_unknown_collisions": len(db["build_unknown_collisions"]),
        "true_conflicts": len(db["true_conflicts"]),
        "raw_noncore_events_summarized": raw_summary["total_events"],
        "raw_noncore_unique_signatures": raw_summary["unique_signatures"],
        "command_events_mode": command_events_mode,
        "core_command_events": core_event_count if command_events_mode == "core" else 0,
        "run": run_metrics,
    }
    save_json(outdir / "summary.json", summary)

    failures = []
    skipped = []
    for file_key, rec in manifest.get("files", {}).items():
        if rec.get("status") == "FAILED":
            failures.append({"file": file_key, **rec})
        elif rec.get("status") == "SKIPPED_NON_REPLAY_JSON":
            skipped.append({"file": file_key, **rec})
    save_json(outdir / "failed_replays.json", {"count": len(failures), "files": failures})
    save_json(outdir / "skipped_json_files.json", {"count": len(skipped), "files": skipped})
    return summary


def default_workers() -> int:
    cpu = os.cpu_count() or 1
    return max(1, min(4, max(1, cpu // 2)))


def main() -> None:
    ap = argparse.ArgumentParser(description="Pyhots Replay Protocol Builder v1.4.1: cross-replay validated runtime variant resolution + HeroesDecode raw StormReplay mining.")
    ap.add_argument("--abilities-complete", required=True, help="Pyhots abilities_complete_v1.16+ JSON")
    ap.add_argument("--replay", action="append", default=[], help="Decoded replay JSON or raw .StormReplay; repeatable")
    ap.add_argument("--replay-dir", action="append", default=[], help="Folder containing replay JSON/.StormReplay files; repeatable")
    ap.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=True, help="Recursively scan --replay-dir (default: true)")
    ap.add_argument("--legacy-seed", help="Optional old replay_numeric_bindings.json as build-unknown evidence")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--pair-window", type=float, default=DEFAULT_PAIR_WINDOW)
    ap.add_argument("--workers", type=int, default=default_workers(), help="Parallel replay workers. Start with 4; lower if RAM is limited.")
    ap.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True, help="Reuse cached per-replay shards when input/config are unchanged")
    ap.add_argument("--force", action="store_true", help="Reprocess all discovered replay inputs")
    ap.add_argument("--observable-signature-samples", type=int, default=DEFAULT_OBSERVABLE_SIGNATURE_SAMPLES, help="Max nested Data shape samples per event type/replay")
    ap.add_argument("--observable-value-samples", type=int, default=DEFAULT_OBSERVABLE_VALUE_SAMPLES, help="Max events/type/replay sampled for semantic values")
    ap.add_argument("--command-events", choices=("none", "core"), default="none", help="Per-event output. Default none keeps database mining compact; use core for selected replay sets.")
    ap.add_argument("--storm-decoder", choices=("heroesdecode", "heroprotocol"), default="heroesdecode", help="Decoder for raw .StormReplay. Default HeroesDecode; heroprotocol is legacy/experimental.")
    ap.add_argument("--heroesdecode-path", help="Optional path/command for HeroesDecode (.exe, .dll, or command on PATH). Auto-detected when omitted.")
    ap.add_argument("--heroesdecode-timeout", type=float, default=180.0, help="Timeout in seconds per raw replay decode.")
    ap.add_argument("--keep-decoded-json", action="store_true", help="Keep HeroesDecode JSON under output/decoded_replays for debugging/comparison.")
    ap.add_argument("--max-replays", type=int, help="Debug/testing limit after discovery")
    args = ap.parse_args()

    outdir = Path(args.output_dir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    cache_dir = outdir / ".replay_cache"
    shard_dir = cache_dir / "replays"
    shard_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = cache_dir / "manifest.json"
    manifest = load_manifest(manifest_path)

    abilities_path = Path(args.abilities_complete).expanduser().resolve()
    if not abilities_path.exists():
        raise SystemExit(f"abilities file not found: {abilities_path}")
    legacy_seed_path = Path(args.legacy_seed).expanduser().resolve() if args.legacy_seed else None
    if legacy_seed_path and not legacy_seed_path.exists():
        raise SystemExit(f"legacy seed not found: {legacy_seed_path}")

    abilities_sha256 = file_sha256(abilities_path)

    # Build/load a compact replay identity index once. Workers no longer each parse the
    # ~135 MB combat schema, which is especially important on Windows spawn workers.
    identity_index_path = cache_dir / f"ability_identity_index_v{IDENTITY_INDEX_VERSION.replace('.', '_')}_{abilities_sha256[:16]}.json"
    if not identity_index_path.exists():
        print("Building compact ability identity index (one-time for this Ability DB)...")
        full_schema = load_json(abilities_path)
        compact = compact_ability_schema(full_schema, abilities_sha256)
        save_json(identity_index_path, compact, pretty=False)
        del full_schema, compact

    cache_signature = stable_hash({
        "protocol_version": SHARD_SCHEMA_VERSION,
        "abilities_sha256": abilities_sha256,
        "pair_window": args.pair_window,
        "observable_signature_samples": args.observable_signature_samples,
        "observable_value_samples": args.observable_value_samples,
        "command_events_mode": args.command_events,
        "storm_decoder": args.storm_decoder,
        "heroesdecode_path": args.heroesdecode_path,
        "heroesdecode_timeout": args.heroesdecode_timeout,
        "keep_decoded_json": bool(args.keep_decoded_json),
    })
    previous_signature = manifest.get("cache_signature")
    cache_config_changed = previous_signature not in (None, cache_signature)
    manifest["cache_signature"] = cache_signature
    manifest["cache_config"] = {
        "protocol_version": PROTOCOL_VERSION,
        "shard_schema_version": SHARD_SCHEMA_VERSION,
        "abilities_file": str(abilities_path),
        "abilities_sha256": abilities_sha256,
        "identity_index": str(identity_index_path),
        "pair_window": args.pair_window,
        "observable_signature_samples": args.observable_signature_samples,
        "observable_value_samples": args.observable_value_samples,
        "command_events_mode": args.command_events,
        "storm_decoder": args.storm_decoder,
        "heroesdecode_path": args.heroesdecode_path,
        "heroesdecode_timeout": args.heroesdecode_timeout,
        "keep_decoded_json": bool(args.keep_decoded_json),
    }

    discovered = discover_replay_inputs(args.replay, args.replay_dir, args.recursive, outdir)
    if args.max_replays is not None:
        discovered = discovered[: max(0, args.max_replays)]
    if not discovered:
        raise SystemExit("No replay inputs found. Use --replay FILE or --replay-dir FOLDER (.json/.StormReplay).")

    json_inputs = sum(1 for p in discovered if p.suffix.lower() == ".json")
    storm_inputs = sum(1 for p in discovered if p.suffix.lower() == ".stormreplay")
    heroesdecode_command: list[str] = []
    heroesdecode_version = None
    keep_decoded_dir = str(outdir / "decoded_replays") if args.keep_decoded_json else None
    if storm_inputs:
        if args.storm_decoder == "heroesdecode":
            try:
                heroesdecode_command = resolve_heroesdecode_command(args.heroesdecode_path)
            except Exception as exc:
                raise SystemExit(str(exc)) from exc
            ok, version_text = check_heroesdecode_command(heroesdecode_command)
            if not ok:
                raise SystemExit(
                    "HeroesDecode raw decoder is not runnable: " + version_text +
                    ". Install/update with: dotnet tool install --global HeroesDecode "
                    "or pass --heroesdecode-path to a HeroesDecode executable/dll."
                )
            heroesdecode_version = version_text
        else:
            deps_ok, missing_deps = check_stormreplay_dependencies()
            if not deps_ok:
                raise SystemExit(
                    "Raw .StormReplay with --storm-decoder heroprotocol requires: " + ", ".join(missing_deps)
                )

        runtime_sig = heroesdecode_version if args.storm_decoder == "heroesdecode" else "HEROPROTOCOL_RUNTIME"
        cache_signature = stable_hash({"base_cache_signature": cache_signature, "storm_decoder_runtime": runtime_sig})
        cache_config_changed = previous_signature not in (None, cache_signature)
        manifest["cache_signature"] = cache_signature
        manifest["cache_config"]["storm_decoder_runtime"] = runtime_sig

    run_start = time.perf_counter()
    to_process: list[tuple[Path, Path, str]] = []
    reused = 0
    for path in discovered:
        key = str(path.resolve())
        fp = file_fingerprint(path)
        shard_name = shard_name_for_path(path)
        shard_path = shard_dir / shard_name
        old = manifest["files"].get(key)
        can_reuse = (
            args.resume and not args.force and not cache_config_changed and old
            and old.get("fingerprint") == fp
            and (
                (old.get("status") == "PROCESSED" and shard_path.exists())
                or old.get("status") == "SKIPPED_NON_REPLAY_JSON"
            )
        )
        if can_reuse:
            reused += 1
            continue
        to_process.append((path, shard_path, fp))

    processed_now = failed_now = skipped_now = 0
    workers = max(1, int(args.workers))
    print(f"Discovered: {len(discovered)} ({json_inputs} JSON, {storm_inputs} StormReplay) | cached: {reused} | to process: {len(to_process)} | workers: {workers}")
    if storm_inputs:
        print(f"StormReplay decoder: {args.storm_decoder}" + (f" | HeroesDecode: {heroesdecode_version}" if heroesdecode_version else ""))

    if to_process:
        initargs = (
            str(identity_index_path), args.pair_window, args.observable_signature_samples,
            args.observable_value_samples, args.command_events, args.storm_decoder, heroesdecode_command,
            args.heroesdecode_timeout, keep_decoded_dir,
        )
        if workers == 1:
            _worker_init(*initargs)
            iterator = []
            for path, shard_path, fp in to_process:
                result = _worker_process_file(str(path), str(shard_path))
                iterator.append((path, shard_path, fp, result))
        else:
            iterator = []
            with ProcessPoolExecutor(max_workers=workers, initializer=_worker_init, initargs=initargs) as pool:
                future_map = {
                    pool.submit(_worker_process_file, str(path), str(shard_path)): (path, shard_path, fp)
                    for path, shard_path, fp in to_process
                }
                done = 0
                for future in as_completed(future_map):
                    path, shard_path, fp = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = {"status": "FAILED", "file": str(path), "error": f"WorkerError: {exc}", "elapsed": None}
                    iterator.append((path, shard_path, fp, result))
                    done += 1
                    if done % 25 == 0 or done == len(to_process):
                        print(f"Processed {done}/{len(to_process)} new/changed replay inputs...")

        for path, shard_path, fp, result in iterator:
            key = str(path.resolve())
            status = result.get("status")
            rec = {
                "fingerprint": fp,
                "status": status,
                "input_type": "STORMREPLAY" if path.suffix.lower() == ".stormreplay" else "JSON",
                "shard": shard_path.name if status == "PROCESSED" else None,
                "elapsed_seconds": result.get("elapsed"),
                "error": result.get("error"),
                "metadata": result.get("metadata"),
            }
            manifest["files"][key] = rec
            if status == "PROCESSED":
                processed_now += 1
            elif status == "FAILED":
                failed_now += 1
            elif status == "SKIPPED_NON_REPLAY_JSON":
                skipped_now += 1
        save_manifest(manifest_path, manifest)

    legacy_seed_sha256 = file_sha256(legacy_seed_path) if legacy_seed_path else None
    manifest_state = [
        (k, v.get("fingerprint"), v.get("status"), v.get("shard"))
        for k, v in sorted(manifest.get("files", {}).items())
    ]
    aggregate_signature = stable_hash({
        "cache_signature": cache_signature,
        "aggregate_protocol_version": PROTOCOL_VERSION,
        "legacy_seed_sha256": legacy_seed_sha256,
        "manifest_state": manifest_state,
    })

    run_metrics = {
        "discovered_inputs": len(discovered),
        "discovered_json_files": json_inputs,
        "discovered_stormreplays": storm_inputs,
        "reused_cached_replays": reused,
        "processed_now": processed_now,
        "failed_now": failed_now,
        "skipped_non_replay_json_now": skipped_now,
        "workers": workers,
        "elapsed_seconds": None,
        "resume": bool(args.resume),
        "force": bool(args.force),
        "cache_config_changed": cache_config_changed,
        "aggregate_rebuilt": True,
        "storm_decoder": args.storm_decoder if storm_inputs else None,
        "heroesdecode_version": heroesdecode_version,
    }

    required_outputs = [
        outdir / "replay_protocol_db.json", outdir / "binding_evidence.json",
        outdir / "unresolved_bindings.json", outdir / "active_talent_evidence.json",
        outdir / "low_confidence_bindings.json", outdir / "active_talent_diagnostics.json",
        outdir / "raw_command_summary.json", outdir / "replay_observables_db.json",
        outdir / "summary.json",
    ]
    if args.command_events == "core":
        required_outputs.append(outdir / "core_command_events.json")

    previous_summary = None
    if (outdir / "summary.json").exists():
        try:
            previous_summary = load_json(outdir / "summary.json")
        except Exception:
            previous_summary = None

    can_reuse_aggregate = (
        not to_process and not cache_config_changed and not args.force
        and previous_summary is not None
        and previous_summary.get("aggregate_signature") == aggregate_signature
        and all(p.exists() for p in required_outputs)
    )

    if can_reuse_aggregate:
        summary = previous_summary
        run_metrics["aggregate_rebuilt"] = False
        run_metrics["elapsed_seconds"] = round(time.perf_counter() - run_start, 3)
        summary["run"] = run_metrics
        summary["aggregate_signature"] = aggregate_signature
        save_json(outdir / "summary.json", summary)
    else:
        run_metrics["elapsed_seconds"] = round(time.perf_counter() - run_start, 3)
        summary = build_outputs(
            outdir, manifest, cache_dir=shard_dir, legacy_seed_path=legacy_seed_path,
            pair_window=args.pair_window, run_metrics=run_metrics, command_events_mode=args.command_events,
        )
        summary["aggregate_signature"] = aggregate_signature
        run_metrics["elapsed_seconds"] = round(time.perf_counter() - run_start, 3)
        summary["run"] = run_metrics
        save_json(outdir / "summary.json", summary)

    print(dumps_json(summary, pretty=True).decode("utf-8"))


if __name__ == "__main__":
    main()
