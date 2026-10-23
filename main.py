from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import re
import tempfile
import urllib.request
import zipfile
import uuid
from pathlib import Path
from typing import Iterable

ORCHESTRATOR_VERSION = "0.7"
STAGES = [
    "ability",
    "replay_protocol",
    "world_state",
    "map",
    "unit",
    "static_map",
    "static_geometry",
    "hero",
    "integrity",
]

DEFAULT_CONFIG = {
    "build": "97771",
    "workers": 4,
    "paths": {
        "db4anal": "db4anal",
        "replays": "parsers_db/stormreps",
        "s2ma": "parsers_db/S2MA",
        "map_assets": "parsers_db/MAP-ASSETS",
        "heroesdecode": "auto",
        "gamedata_cache": "parsers_db/_gamedata_cache",
        "gamedata_extract_root": "auto"
    },
    "source": {
        "repository": "jamiephan/HeroesOfTheStorm_Gamedata"
    },
    "hero": {
        "run_build_matched_version_pipeline": True
    }
}

CANONICAL = {
    "ability": ["ability/abilities_complete.json"],
    "replay_protocol": ["replay_protocol/replay_protocol_db.json", "replay_protocol/replay_observables_db.json"],
    "map": ["map/map_db.json"],
    "unit": ["unit/unit_db.json", "catalog/catalog_index.json"],
    "hero": ["hero/hero_unit_db.json"],
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def merge_dict(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def ensure_default_config(path: Path) -> dict:
    if not path.exists():
        save_json(path, DEFAULT_CONFIG)
        return DEFAULT_CONFIG
    return merge_dict(DEFAULT_CONFIG, load_json(path))


def resolve_path(root: Path, value: str | None) -> Path | None:
    if value is None:
        return None
    p = Path(value).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def normalize_full_version(value: str) -> str | None:
    v = str(value).strip().lstrip("v")
    return v if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v) else None


def http_get_bytes(url: str, *, timeout: int = 60, attempts: int = 3) -> bytes:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"Pyhots-Orchestrator/{ORCHESTRATOR_VERSION}"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:
            last = exc
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"download failed: {url}: {last}")


def resolve_version(value: str, cache_root: Path, repository: str) -> str:
    full = normalize_full_version(value)
    if full:
        return full
    build = str(value).strip().lstrip("v")
    if not re.fullmatch(r"\d{4,7}", build):
        raise RuntimeError("version must look like 2.55.17.97771 or 97771")
    cache = cache_root / "VERSIONS.md"
    url = f"https://raw.githubusercontent.com/{repository}/master/VERSIONS.md"
    try:
        data = http_get_bytes(url).decode("utf-8", errors="replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(data, encoding="utf-8")
    except Exception:
        if not cache.exists():
            raise
        data = cache.read_text(encoding="utf-8", errors="replace")
    matches = sorted(set(re.findall(rf"\b(\d+\.\d+\.\d+\.{re.escape(build)})\b", data)))
    if len(matches) != 1:
        raise RuntimeError(f"build {build}: expected one exact GameData version, found {matches}")
    return matches[0]


def default_extract_root(config_value: str | None, cache_root: Path) -> Path:
    if config_value and config_value != "auto":
        return Path(config_value).expanduser().resolve()
    if os.name == "nt":
        return Path(tempfile.gettempdir()).resolve() / "PyhotsGD"
    return cache_root / "extracted"


def win_long_path(path: Path) -> str:
    value = str(path.resolve())
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return value
    if value.startswith("\\\\"):
        return "\\\\?\\UNC\\" + value[2:]
    return "\\\\?\\" + value


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        shutil.rmtree(win_long_path(path))
    else:
        shutil.rmtree(path)


def safe_extract_github_zip(archive: Path, dest: Path) -> Path:
    marker = dest / ".pyhots_extract_complete_v07"
    if marker.exists():
        return dest
    if dest.exists():
        remove_tree(dest)
    os.makedirs(win_long_path(dest), exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        roots = []
        for info in infos:
            raw = info.filename.replace("\\", "/").lstrip("/")
            if raw:
                parts = Path(raw).parts
                if parts:
                    roots.append(parts[0])
        prefix = roots[0] if roots and all(x == roots[0] for x in roots) else None
        for info in infos:
            raw = info.filename.replace("\\", "/").lstrip("/")
            if not raw:
                continue
            parts = list(Path(raw).parts)
            if prefix and parts and parts[0] == prefix:
                parts = parts[1:]
            if not parts:
                continue
            if any(part in ("", ".", "..") for part in parts):
                raise RuntimeError(f"unsafe zip member: {info.filename}")
            target = dest.joinpath(*parts)
            if info.is_dir() or raw.endswith("/"):
                os.makedirs(win_long_path(target), exist_ok=True)
                continue
            os.makedirs(win_long_path(target.parent), exist_ok=True)
            with z.open(info) as src, open(win_long_path(target), "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
    marker.write_text("ok\n", encoding="utf-8")
    return dest


def download_gamedata_archive(repository: str, full_version: str, cache_root: Path) -> Path:
    tag = f"v{full_version}"
    archive = cache_root / "archives" / f"{tag}.zip"
    if archive.exists() and archive.stat().st_size > 0:
        print(f"[gamedata] archive cache hit: {archive}")
        return archive
    archive.parent.mkdir(parents=True, exist_ok=True)
    url = f"https://codeload.github.com/{repository}/zip/refs/tags/{tag}"
    tmp = archive.with_suffix(".zip.part")
    print(f"[gamedata] download {url}")
    req = urllib.request.Request(url, headers={"User-Agent": f"Pyhots-Orchestrator/{ORCHESTRATOR_VERSION}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as w:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                w.write(chunk); got += len(chunk)
                if total:
                    print(f"\r  {got/1048576:.1f}/{total/1048576:.1f} MiB", end="", flush=True)
                else:
                    print(f"\r  {got/1048576:.1f} MiB", end="", flush=True)
        print()
        os.replace(tmp, archive)
    except Exception:
        try: tmp.unlink(missing_ok=True)
        except Exception: pass
        raise
    return archive


def prepare_gamedata_source(ctx: dict, *, dry_run: bool = False) -> Path:
    if dry_run:
        return ctx["extract_root"] / f"v{ctx['full_version']}" / "mods"
    archive = download_gamedata_archive(ctx["repository"], ctx["full_version"], ctx["gamedata_cache"])
    source = safe_extract_github_zip(archive, ctx["extract_root"] / f"v{ctx['full_version']}")
    mods = require_dir(source / "mods", f"GameData mods for {ctx['full_version']}")
    provenance = {
        "version": ctx["full_version"],
        "tag": f"v{ctx['full_version']}",
        "repository": ctx["repository"],
        "archive": str(archive),
        "archive_sha256": sha256(archive),
        "source_root": str(source),
        "mods_root": str(mods),
    }
    save_json(ctx["gamedata_cache"] / "provenance" / f"v{ctx['full_version']}.json", provenance)
    return mods


def qcmd(cmd: list[str]) -> str:
    return subprocess.list2cmdline(cmd)


def run_cmd(cmd: list[str], cwd: Path, *, dry_run: bool = False) -> None:
    print(f"\n[cwd] {cwd}")
    print(f"[run] {qcmd(cmd)}")
    if dry_run:
        return
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    cp = subprocess.run(cmd, cwd=str(cwd), env=env)
    if cp.returncode != 0:
        raise RuntimeError(f"command failed with exit code {cp.returncode}: {qcmd(cmd)}")


def require_file(path: Path, label: str) -> Path:
    if not path.is_file():
        raise RuntimeError(f"{label} not found: {path}")
    return path


def require_dir(path: Path, label: str) -> Path:
    if not path.is_dir():
        raise RuntimeError(f"{label} not found: {path}")
    return path


def retry_io(fn, *, label: str, attempts: int = 12, initial_delay: float = 0.25):
    delay = initial_delay
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (PermissionError, OSError) as exc:
            last = exc
            if attempt >= attempts:
                break
            print(f"[retry] {label}: {exc} | retry {attempt}/{attempts} in {delay:.2f}s")
            time.sleep(delay)
            delay = min(delay * 1.7, 3.0)
    raise RuntimeError(f"{label} failed after {attempts} attempts: {last}") from last


def atomic_copy_file(src: Path, dst: Path) -> None:
    require_file(src, "builder output")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    def do_copy():
        with src.open("rb") as r, tmp.open("wb") as w:
            shutil.copyfileobj(r, w, length=1024 * 1024)
            w.flush(); os.fsync(w.fileno())
    retry_io(do_copy, label=f"read/copy {src.name}")
    retry_io(lambda: os.replace(tmp, dst), label=f"atomic publish {dst}")
    print(f"[publish] {src} -> {dst}")


def replace_dir_atomic(staging: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    old = dst.parent / f".{dst.name}.old-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if dst.exists():
        retry_io(lambda: os.replace(dst, old), label=f"retire old {dst}")
    try:
        retry_io(lambda: os.replace(staging, dst), label=f"publish directory {dst}")
    except Exception:
        if old.exists() and not dst.exists():
            os.replace(old, dst)
        raise
    if old.exists():
        retry_io(lambda: remove_tree(old), label=f"remove old {dst}")


def publish_files(stage_dir: Path, files: dict[str, Path]) -> None:
    staging = stage_dir.parent / f".{stage_dir.name}.new-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if staging.exists():
        remove_tree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for rel, src in files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            require_file(src, "builder output")
            retry_io(lambda s=src, t=target: shutil.copyfile(s, t), label=f"stage {src.name}")
        replace_dir_atomic(staging, stage_dir)
    finally:
        if staging.exists():
            remove_tree(staging)
    print(f"[publish] clean stage -> {stage_dir}")


def publish_stage_content(stage_dir: Path, files: dict[str, Path], dirs: dict[str, Path] | None = None) -> None:
    staging = stage_dir.parent / f".{stage_dir.name}.new-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if staging.exists():
        remove_tree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    try:
        for rel, src in files.items():
            target = staging / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            require_file(src, "builder output")
            retry_io(lambda s=src, t=target: shutil.copyfile(s, t), label=f"stage {src.name}")
        for rel, src in (dirs or {}).items():
            if not src.is_dir():
                continue
            target = staging / rel
            retry_io(lambda s=src, t=target: shutil.copytree(s, t), label=f"stage tree {src.name}")
        replace_dir_atomic(staging, stage_dir)
    finally:
        if staging.exists():
            remove_tree(staging)
    print(f"[publish] clean stage -> {stage_dir}")


def copy_file(src: Path, dst: Path) -> None:
    atomic_copy_file(src, dst)


def copy_tree(src: Path, dst: Path) -> None:
    if not src.is_dir():
        return
    staging = dst.parent / f".{dst.name}.new-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    if staging.exists():
        remove_tree(staging)
    retry_io(lambda: shutil.copytree(src, staging), label=f"stage tree {src.name}")
    replace_dir_atomic(staging, dst)
    print(f"[publish] {src} -> {dst}")


def replay_counts(replay_dir: Path) -> tuple[int, int]:
    raw = sum(1 for p in replay_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".stormreplay")
    js = sum(1 for p in replay_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".json")
    return raw, js


def context(root: Path, cfg: dict, version_arg: str | None = None) -> dict:
    paths = cfg["paths"]
    requested = str(version_arg or cfg.get("build") or "").strip()
    if not requested:
        raise RuntimeError("no game version/build selected")
    parsers = (root / "parsers_db").resolve()
    gamedata_cache = resolve_path(root, paths.get("gamedata_cache") or "parsers_db/_gamedata_cache")
    repository = str((cfg.get("source") or {}).get("repository") or "jamiephan/HeroesOfTheStorm_Gamedata")
    full_version = resolve_version(requested, gamedata_cache, repository)
    extract_cfg = paths.get("gamedata_extract_root", "auto")
    extract_root = default_extract_root(None if extract_cfg == "auto" else str(extract_cfg), gamedata_cache)
    p = {
        "root": root,
        "build": requested,
        "full_version": full_version,
        "workers": int(cfg.get("workers") or 4),
        "db": resolve_path(root, paths.get("db4anal") or "db4anal"),
        "replays": resolve_path(root, paths.get("replays") or "parsers_db/stormreps"),
        "s2ma": resolve_path(root, paths.get("s2ma") or "parsers_db/S2MA"),
        "map_assets": resolve_path(root, paths.get("map_assets") or "parsers_db/MAP-ASSETS"),
        "heroesdecode": paths.get("heroesdecode", "auto"),
        "parsers": parsers,
        "gamedata_cache": gamedata_cache,
        "extract_root": extract_root,
        "repository": repository,
    }
    p["mods"] = extract_root / f"v{full_version}" / "mods"
    p["ability_dir"] = parsers / "HOTS_ABILITYDB-parser"
    p["world_dir"] = parsers / "WORLD_STATE_BUILDER_V0_1_4"
    p["map_dir"] = parsers / "MAP_DB_BUILDER_V0_2"
    p["unit_dir"] = parsers / "UNIT_DB_BUILDER_V0_1_6"
    p["static_map_dir"] = parsers / "STATIC_MAP_PROBE_V0_1_1"
    p["static_geom_dir"] = parsers / "STATIC_GEOMETRY_PROBE_V0_9"
    p["hero_dir"] = parsers / "HERO_DB_V0_1_3"
    p["integrity_dir"] = parsers / "DB_INTEGRITY_GATE_V0_2"
    return p


def hd_args(ctx: dict) -> list[str]:
    value = ctx.get("heroesdecode")
    if value in (None, "", "auto"):
        return []
    path = resolve_path(ctx["root"], str(value))
    return ["--heroesdecode-path", str(path)]


def preflight(ctx: dict, start_at: str) -> None:
    db = ctx["db"]
    db.mkdir(parents=True, exist_ok=True)
    require_dir(ctx["parsers"], "parsers_db")
    start_idx = STAGES.index(start_at)

    if start_idx <= STAGES.index("replay_protocol"):
        require_dir(ctx["replays"], "replay directory")
        raw, js = replay_counts(ctx["replays"])
        print(f"[preflight] replay dir: {raw} .StormReplay, {js} .json")
        if raw == 0:
            raise RuntimeError(f"no .StormReplay files found in {ctx['replays']}")
        if js:
            print("[warning] replay directory also contains JSON files; original builders discover both .StormReplay and .json.")
    if start_idx <= STAGES.index("static_geometry") and ctx["s2ma"].exists():
        s2ma_count = sum(1 for p in ctx["s2ma"].rglob("*") if p.is_file())
        print(f"[preflight] S2MA/assets files: {s2ma_count}")


def stage_output_dirs(ctx: dict) -> dict[str, Path]:
    return {
        "replay_protocol": ctx["world_dir"] / "replay_protocol_output",
        "world_state": ctx["world_dir"] / "output",
        "map": ctx["map_dir"] / "output",
        "unit": ctx["unit_dir"] / "output",
        "static_map": ctx["static_map_dir"] / "output",
        "static_geometry": ctx["static_geom_dir"] / "output",
        "hero_static": ctx["hero_dir"] / "output_chain",
    }


def build_ability(ctx: dict, dry: bool) -> None:
    d = ctx["ability_dir"]
    cmd = [sys.executable, "build_abilities_database.py", "--mods-root", str(ctx["mods"]), "--step", "21"]
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    src = require_file(d / "output" / "abilities_complete.json", "Ability DB")
    files = {"abilities_complete.json": src}
    ai = d / "output" / "storm_catalog_index.json"
    if ai.exists():
        files["storm_catalog_index.json"] = ai
    publish_files(ctx["db"] / "ability", files)


def build_replay_protocol(ctx: dict, dry: bool) -> None:
    d = ctx["world_dir"]
    out = stage_output_dirs(ctx)["replay_protocol"]
    ability = require_file(ctx["db"] / "ability" / "abilities_complete.json", "db4anal Ability DB") if not dry else ctx["db"] / "ability" / "abilities_complete.json"
    cmd = [sys.executable, "replay_protocol_builder.py", "--abilities-complete", str(ability), "--replay-dir", str(ctx["replays"]), "--output-dir", str(out), "--workers", str(ctx["workers"])] + hd_args(ctx)
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    publish_files(ctx["db"] / "replay_protocol", {
        "replay_protocol_db.json": out / "replay_protocol_db.json",
        "replay_observables_db.json": out / "replay_observables_db.json",
    })


def build_world_state(ctx: dict, dry: bool) -> None:
    d = ctx["world_dir"]
    out = stage_output_dirs(ctx)["world_state"]
    ability = require_file(ctx["db"] / "ability" / "abilities_complete.json", "db4anal Ability DB") if not dry else ctx["db"] / "ability" / "abilities_complete.json"
    cmd = [sys.executable, "world_state_builder.py", "--replay-dir", str(ctx["replays"]), "--output-dir", str(out), "--abilities-complete", str(ability), "--workers", str(ctx["workers"])] + hd_args(ctx)
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    require_file(out / "summary.json", "World State summary")
    require_file(out / "integrity_report.json", "World State integrity")
    # World State corpus is replay-specific, so it remains under its builder. It is not a static Analyzer DB.


def build_map(ctx: dict, dry: bool) -> None:
    d = ctx["map_dir"]
    out = stage_output_dirs(ctx)["map"]
    ability = require_file(ctx["db"] / "ability" / "abilities_complete.json", "db4anal Ability DB") if not dry else ctx["db"] / "ability" / "abilities_complete.json"
    cmd = [sys.executable, "map_db_builder.py", "--replay-dir", str(ctx["replays"]), "--output-dir", str(out), "--abilities-complete", str(ability), "--workers", str(ctx["workers"])] + hd_args(ctx)
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    publish_files(ctx["db"] / "map", {"map_db.json": out / "map_db.json"})


def build_unit(ctx: dict, dry: bool) -> None:
    d = ctx["unit_dir"]
    out = stage_output_dirs(ctx)["unit"]
    map_db = require_file(ctx["db"] / "map" / "map_db.json", "db4anal Map DB") if not dry else ctx["db"] / "map" / "map_db.json"
    cmd = [sys.executable, "unit_db_builder.py", "--map-db", str(map_db), "--mods-root", str(ctx["mods"]), "--output-dir", str(out)]
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    publish_files(ctx["db"] / "unit", {"unit_db.json": out / "unit_db.json"})
    publish_files(ctx["db"] / "catalog", {"catalog_index.json": out / "catalog_index.json"})


def build_static_map(ctx: dict, dry: bool) -> None:
    d = ctx["static_map_dir"]
    out = stage_output_dirs(ctx)["static_map"]
    map_db = require_file(ctx["db"] / "map" / "map_db.json", "db4anal Map DB") if not dry else ctx["db"] / "map" / "map_db.json"
    catalog = require_file(ctx["db"] / "catalog" / "catalog_index.json", "db4anal canonical catalog") if not dry else ctx["db"] / "catalog" / "catalog_index.json"
    cmd = [sys.executable, "static_map_probe.py", "--mods-root", str(ctx["mods"]), "--map-db", str(map_db), "--output-dir", str(out), "--catalog-index", str(catalog)]
    if ctx["map_assets"] and ctx["map_assets"].is_dir() and any(ctx["map_assets"].iterdir()):
        cmd += ["--assets-root", str(ctx["map_assets"])]
    run_cmd(cmd, d, dry_run=dry)
    if not dry:
        require_file(out / "static_map_integrity_report.json", "Static Map integrity")


def build_static_geometry(ctx: dict, dry: bool) -> None:
    d = ctx["static_geom_dir"]
    out = stage_output_dirs(ctx)["static_geometry"]
    map_db = require_file(ctx["db"] / "map" / "map_db.json", "db4anal Map DB") if not dry else ctx["db"] / "map" / "map_db.json"
    catalog = require_file(ctx["db"] / "catalog" / "catalog_index.json", "db4anal canonical catalog") if not dry else ctx["db"] / "catalog" / "catalog_index.json"
    if not dry:
        require_dir(ctx["s2ma"], "S2MA root")
    cmd = [sys.executable, "static_geometry_probe.py", "--s2ma-root", str(ctx["s2ma"]), "--catalog-index", str(catalog), "--map-db", str(map_db), "--output-dir", str(out)]
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    # Only runtime/static geometry artifacts go to db4anal. Reports/audits remain in the builder output.
    runtime_files = [
        "movement_mechanics.json", "objects_on_conveyors.json", "shrub_cells.json", "shrub_placements.json",
        "terrain_pnp_mask.json", "pnp_subcell_layout.json", "cell_attribute_grids.json", "cliff_layers.json",
        "terrain_height.json", "static_vision_geometry.json", "static_accessibility.json", "map_package_manifest.json",
    ]
    files = {name: out / name for name in runtime_files if (out / name).is_file()}
    dirs = {name: out / name for name in ("cell_attribute_grids", "cliff_raw_grids", "terrain_height_grids") if (out / name).is_dir()}
    publish_stage_content(ctx["db"] / "static_map", files, dirs)


def compare_hero_static(chain_summary: Path, version_static: Path) -> None:
    a = load_json(chain_summary)
    b = load_json(version_static).get("summary") or {}
    keys = ["heroes", "forms", "unique_owned_entities", "chero_level_scaling_records", "typed_level_scaling_records", "typed_execution_unresolved", "overall"]
    diff = {k: {"chain": a.get(k), "version": b.get(k)} for k in keys if a.get(k) != b.get(k)}
    if diff:
        raise RuntimeError(f"Hero static DB mismatch between Unit-catalog chain and exact-version pipeline: {json.dumps(diff, ensure_ascii=False)}")
    print("[hero] Unit-catalog static Hero DB matches exact-version static baseline on frozen metrics")


def build_hero(ctx: dict, dry: bool, cfg: dict) -> None:
    d = ctx["hero_dir"]
    out = stage_output_dirs(ctx)["hero_static"]
    catalog = require_file(ctx["db"] / "catalog" / "catalog_index.json", "db4anal canonical catalog") if not dry else ctx["db"] / "catalog" / "catalog_index.json"
    compact = out / "hero_global_compact.json"
    cmd = [sys.executable, "hero_unit_db_builder.py", "--catalog-index", str(catalog), "--output-dir", str(out), "--compact-cache", str(compact)]
    run_cmd(cmd, d, dry_run=dry)
    if dry:
        return
    static_db = require_file(out / "hero_unit_db.json", "Hero static DB")

    files = {"hero_unit_db_static_v0_2.json": static_db}
    if not (cfg.get("hero") or {}).get("run_build_matched_version_pipeline", True):
        files["hero_unit_db.json"] = static_db
        publish_files(ctx["db"] / "hero", files)
        return

    # Exact-version Hero pipeline remains standalone; main only invokes its public CLI.
    run_cmd([sys.executable, "pyhots_version_builder.py", ctx["full_version"]], d, dry_run=False)
    build_root = d / "versioned_data" / "builds" / f"v{ctx['full_version']}" / "hero_unit_db"
    version_static = require_file(build_root / "hero_unit_db_static_v0_2.json", "exact-version static Hero DB")
    compare_hero_static(out / "summary.json", version_static)
    files["hero_unit_db.json"] = require_file(build_root / "hero_unit_db.json", "exact-version canonical Hero DB")
    scaling = build_root / "scaling_application_semantics.json"
    if scaling.is_file():
        files["scaling_application_semantics.json"] = scaling
    publish_files(ctx["db"] / "hero", files)


def build_integrity(ctx: dict, dry: bool) -> None:
    d = ctx["integrity_dir"]
    outs = stage_output_dirs(ctx)
    output = d / "integrity_gate_report.json"
    cmd = [sys.executable, "integrity_gate.py", "--protocol-output", str(outs["replay_protocol"]), "--world-state-output", str(outs["world_state"]), "--output", str(output)]
    run_cmd(cmd, d, dry_run=dry)
    if not dry:
        require_file(output, "Integrity Gate report")


BUILDERS = {
    "ability": build_ability,
    "replay_protocol": build_replay_protocol,
    "world_state": build_world_state,
    "map": build_map,
    "unit": build_unit,
    "static_map": build_static_map,
    "static_geometry": build_static_geometry,
}


CANONICAL_STAGE_DIRS = {
    "ability": {"ability"},
    "replay_protocol": {"replay_protocol"},
    "world_state": set(),
    "map": {"map"},
    "unit": {"unit", "catalog"},
    "static_map": set(),
    "static_geometry": {"static_map"},
    "hero": {"hero"},
    "integrity": set(),
}


def dirs_from_stage(start_at: str) -> set[str]:
    out = set()
    for stage in STAGES[STAGES.index(start_at):]:
        out.update(CANONICAL_STAGE_DIRS.get(stage, set()))
    return out


def clean_canonical_from(ctx: dict, start_at: str) -> None:
    db = ctx["db"]
    db.mkdir(parents=True, exist_ok=True)
    for name in sorted(dirs_from_stage(start_at)):
        path = db / name
        if path.exists():
            print(f"[clean] remove stale canonical stage: {path}")
            retry_io(lambda p=path: remove_tree(p), label=f"clean {path}")
    for name in ("manifest.json",):
        p = db / name
        if p.exists():
            p.unlink()


def current_db_version(db: Path) -> str | None:
    p = db / "version.json"
    if not p.is_file():
        return None
    try:
        return str(load_json(p).get("version") or "") or None
    except Exception:
        return None


def write_build_state(ctx: dict, status: str, completed: list[str], error: str | None = None) -> None:
    payload = {
        "schema": {"name": "PYHOTS_DB_BUILD_STATE", "version": "1"},
        "status": status,
        "target_build": ctx["build"],
        "target_version": ctx["full_version"],
        "orchestrator": ORCHESTRATOR_VERSION,
        "completed_stages": list(completed),
    }
    if error:
        payload["error"] = error
    save_json(ctx["db"] / "build_state.json", payload)


def manifest(ctx: dict, completed: Iterable[str]) -> dict:
    files = {}
    for p in sorted(ctx["db"].rglob("*")):
        if p.is_file() and p.name not in {"manifest.json"}:
            rel = str(p.relative_to(ctx["db"])).replace("\\", "/")
            files[rel] = {"sha256": sha256(p), "size": p.stat().st_size}
    return {
        "schema": {"name": "PYHOTS_ANALYZER_DB_MANIFEST", "version": ORCHESTRATOR_VERSION},
        "status": "PASS" if completed and completed[-1] == "integrity" else "BUILDING",
        "target_build": ctx["build"],
        "target_version": ctx["full_version"],
        "completed_stages": list(completed),
        "canonical_files": files,
        "contract": {
            "builders_are_standalone": True,
            "builder_source_modified_by_orchestrator": False,
            "db4anal_contains_runtime_canonical_outputs_only": True,
            "world_state_corpus_remains_in_builder_output": True,
        },
    }


def write_manifest(ctx: dict, completed: list[str]) -> None:
    save_json(ctx["db"] / "manifest.json", manifest(ctx, completed))
    save_json(ctx["db"] / "version.json", {"build": ctx["build"], "version": ctx["full_version"], "orchestrator": ORCHESTRATOR_VERSION})


def build_all(root: Path, cfg: dict, args) -> None:
    ctx = context(root, cfg, args.version)
    start = args.start_at
    stop = args.stop_after or STAGES[-1]
    if STAGES.index(stop) < STAGES.index(start):
        raise SystemExit("--stop-after must not precede --start-at")

    existing_version = current_db_version(ctx["db"])
    if existing_version and existing_version != ctx["full_version"] and start != "ability":
        raise RuntimeError(
            f"db4anal currently belongs to {existing_version}; requested {ctx['full_version']}. "
            "A different game version must start at ability so stale canonical data can be cleaned."
        )

    # GameData source preparation is infrastructure, not a database stage.
    needs_gamedata = any(STAGES.index(start) <= STAGES.index(x) <= STAGES.index(stop) for x in ("ability", "unit", "static_map"))
    if needs_gamedata:
        ctx["mods"] = prepare_gamedata_source(ctx, dry_run=args.dry_run)
        if not args.dry_run:
            require_dir(ctx["mods"], f"exact GameData mods {ctx['full_version']}")

    preflight(ctx, start)
    selected = STAGES[STAGES.index(start): STAGES.index(stop) + 1]
    print("\n=== PYHOTS THIN ORCHESTRATOR v%s ===" % ORCHESTRATOR_VERSION)
    print("Build:", ctx["build"], "=>", ctx["full_version"])
    print("Stages:", " -> ".join(selected))
    print("db4anal:", ctx["db"])
    print("GameData:", ctx["mods"])
    print("NOTE: builders remain standalone; main only supplies exact-version inputs and publishes canonical outputs.")

    completed = []
    t0 = time.time()
    if not args.dry_run:
        clean_canonical_from(ctx, start)
        save_json(ctx["db"] / "version.json", {"build": ctx["build"], "version": ctx["full_version"], "orchestrator": ORCHESTRATOR_VERSION})
        write_build_state(ctx, "BUILDING", completed)
    try:
        for stage in selected:
            print(f"\n{'='*72}\nSTAGE: {stage}\n{'='*72}")
            if stage == "hero":
                build_hero(ctx, args.dry_run, cfg)
            elif stage == "integrity":
                build_integrity(ctx, args.dry_run)
            else:
                BUILDERS[stage](ctx, args.dry_run)
            completed.append(stage)
            if not args.dry_run:
                write_manifest(ctx, completed)
                write_build_state(ctx, "PASS" if stage == "integrity" else "BUILDING", completed)
    except Exception as exc:
        if not args.dry_run:
            write_build_state(ctx, "FAILED", completed, str(exc))
        raise
    print(f"\nDONE in {time.time()-t0:.1f}s" if not args.dry_run else "\nDRY RUN COMPLETE")


def print_plan(root: Path, cfg: dict, version: str | None) -> None:
    ctx = context(root, cfg, version)
    print("Pyhots thin orchestration DAG:\n")
    print(f"GameData source {ctx['full_version']} -> exact-version mods (cached)")
    print("                         |")
    print("                         v")
    print("Ability DB -> db4anal/ability/abilities_complete.json")
    print("    |-> Replay Protocol (raw .StormReplay)")
    print("    |-> World State     (raw .StormReplay)")
    print("    `-> Map DB          (raw .StormReplay) -> db4anal/map/map_db.json")
    print("                                              |")
    print("                                              v")
    print("Unit DB -> db4anal/unit/unit_db.json + db4anal/catalog/catalog_index.json")
    print("             |-> Static Map")
    print("             |-> Static Geometry -> db4anal/static_map/*")
    print("             `-> Hero static -> exact-version Hero scaling -> db4anal/hero/hero_unit_db.json")
    print("Replay Protocol + World State -> Integrity Gate")
    print("\nNo builder consumes an orchestrator-created decoded replay format.")
    print("Each builder remains manually runnable with its original CLI.")
    print("\nResolved paths:")
    for k in ("replays", "mods", "s2ma", "map_assets", "db"):
        print(f"  {k:12} {ctx[k]}")


def print_status(root: Path, cfg: dict, version: str | None) -> None:
    ctx = context(root, cfg, version)
    print(f"Build {ctx['build']} ({ctx['full_version']})")
    print(f"db4anal: {ctx['db']}")
    state = ctx["db"] / "build_state.json"
    if state.is_file():
        info = load_json(state)
        print(f"state: {info.get('status')} | stored version: {info.get('target_version')} | completed: {', '.join(info.get('completed_stages') or [])}")
    for stage, rels in CANONICAL.items():
        ok = all((ctx["db"] / r).is_file() for r in rels)
        print(f"{stage:18} {'PASS' if ok else 'MISSING'}")
        for r in rels:
            p = ctx["db"] / r
            if p.exists():
                print(f"  {r}  {p.stat().st_size:,} bytes  sha256={sha256(p)[:16]}...")


def interactive(root: Path, cfg: dict) -> None:
    while True:
        print("\nPYHOTS MAIN v%s" % ORCHESTRATOR_VERSION)
        print("[1] Build all databases")
        print("[2] Plan / resolved paths")
        print("[3] Status")
        print("[0] Exit")
        x = input("Choose: ").strip()
        if x == "0": return
        if x == "2": print_plan(root, cfg, None); continue
        if x == "3": print_status(root, cfg, None); continue
        if x == "1":
            class A: pass
            default_version = str(cfg.get("build") or "")
            entered = input(f"Game version/build [{default_version}]: ").strip()
            a=A(); a.version=entered or default_version; a.start_at="ability"; a.stop_after=None; a.dry_run=False
            build_all(root, cfg, a)
            continue
        print("Unknown choice")


def main() -> None:
    root = Path(__file__).resolve().parent
    cfg_path = root / "pyhots_config.json"
    cfg = ensure_default_config(cfg_path)

    ap = argparse.ArgumentParser(description="Pyhots thin orchestrator: run frozen standalone DB builders and publish canonical runtime outputs to db4anal")
    sub = ap.add_subparsers(dest="command")

    b = sub.add_parser("build", help="run the existing standalone builders in dependency order")
    b.add_argument("--version", default=None, help="build/version, default from pyhots_config.json")
    b.add_argument("--start-at", choices=STAGES, default="ability")
    b.add_argument("--stop-after", choices=STAGES, default=None)
    b.add_argument("--dry-run", action="store_true", help="print exact standalone commands without running them")

    p = sub.add_parser("plan", help="show dependency DAG and resolved paths")
    p.add_argument("--version", default=None)
    s = sub.add_parser("status", help="show canonical db4anal files")
    s.add_argument("--version", default=None)

    ns = ap.parse_args()
    if ns.command == "build": build_all(root, cfg, ns)
    elif ns.command == "plan": print_plan(root, cfg, ns.version)
    elif ns.command == "status": print_status(root, cfg, ns.version)
    else: interactive(root, cfg)


if __name__ == "__main__":
    main()
