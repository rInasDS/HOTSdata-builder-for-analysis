#!/usr/bin/env python3
"""Pyhots version-aware Hero Unit DB pipeline v0.1.3.

Interactive/default workflow:
    version/tag -> download exact HeroesOfTheStorm_Gamedata archive -> verify provenance
    -> build a Hero-DB-compatible compact catalog directly from raw XML
    -> build Hero Unit DB -> run build-matched Galaxy scaling application probe.

Design constraints:
* fail closed: a missing tag, malformed archive, missing Galaxy script, or failed Hero DB
  integrity gate is never silently accepted;
* cache exact source archives and extracted trees by tag;
* preserve build provenance and SHA-256 hashes;
* do not infer runtime ownership/execution from broad reference reachability.

The compact XML indexer implements the subset of
`storm_catalog_index_v2_4_ancestor_context` required by Hero Unit DB. It retains
`VeterancyLevelArray` ancestor identity exactly because Hero scaling depends on it.
For the project baseline build 2.55.17.97771 an additional metric compatibility gate
is applied after Hero DB construction.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Iterable

from hero_unit_db_builder import build as build_hero_db
from json_backend import save
from engine_scaling_probe import probe as build_scaling_application_evidence
from scaling_runtime_promoter import promote as promote_runtime_scaling

TOOL_VERSION = "0.1.3"
REPOSITORY = "jamiephan/HeroesOfTheStorm_Gamedata"
VERSIONS_RAW_URL = f"https://raw.githubusercontent.com/{REPOSITORY}/master/VERSIONS.md"
ARCHIVE_URL_TEMPLATE = f"https://codeload.github.com/{REPOSITORY}/zip/refs/tags/{{tag}}"
GITHUB_ARCHIVE_FALLBACK = f"https://github.com/{REPOSITORY}/archive/refs/tags/{{tag}}.zip"

INCLUDE_PREFIX = (
    "CHero", "CTalent", "CUnit", "CWeapon", "CTurret", "CAbil", "CEffect",
    "CBehavior", "CValidator", "CAccumulator", "CArmor", "CUpgrade", "CMover",
    "CFootprint", "CRequirement",
)
EXCLUDED_PATH_TOKENS = (
    "heroesmapmods", "heroesbrawlmods", "singleplayermaps", "tutorial",
    "startingexperience",
)

BASELINE_EXPECTATIONS = {
    # Frozen v0.2 static baseline used by the project before the version manager existed.
    "2.55.17.97771": {
        "heroes": 90,
        "forms": 104,
        "unique_owned_entities": 129,
        "chero_level_scaling_records": 1641,
        "typed_level_scaling_records": 1641,
        "typed_execution_unresolved": 0,
        "overall": "PASS",
    },
}


def sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _http_get_bytes(url: str, *, timeout: int = 60, attempts: int = 3) -> bytes:
    last: Exception | None = None
    headers = {"User-Agent": f"Pyhots-Version-Builder/{TOOL_VERSION}"}
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:  # network errors are reported after bounded retries
            last = exc
            if attempt < attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"download failed: {url}: {last}")


def download_file(urls: list[str], dest: pathlib.Path, *, force: bool = False) -> str:
    if dest.exists() and dest.stat().st_size > 0 and not force:
        print(f"[download] cache hit: {dest}")
        return "CACHE"
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for url in urls:
        try:
            print(f"[download] {url}")
            headers = {"User-Agent": f"Pyhots-Version-Builder/{TOOL_VERSION}"}
            req = urllib.request.Request(url, headers=headers)
            tmp = dest.with_suffix(dest.suffix + ".part")
            with urllib.request.urlopen(req, timeout=120) as r, tmp.open("wb") as w:
                total = int(r.headers.get("Content-Length") or 0)
                got = 0
                while True:
                    chunk = r.read(1024 * 1024)
                    if not chunk:
                        break
                    w.write(chunk)
                    got += len(chunk)
                    if total:
                        print(f"\r  {got/1048576:.1f}/{total/1048576:.1f} MiB", end="", flush=True)
                    else:
                        print(f"\r  {got/1048576:.1f} MiB", end="", flush=True)
            print()
            os.replace(tmp, dest)
            return url
        except Exception as exc:
            last = exc
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"[download] failed: {exc}")
    raise RuntimeError(f"all archive download URLs failed: {last}")


def normalize_full_version(value: str) -> str | None:
    v = value.strip()
    if v.lower().startswith("v"):
        v = v[1:]
    return v if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", v) else None


def resolve_version(value: str, cache_root: pathlib.Path) -> str:
    full = normalize_full_version(value)
    if full:
        return full
    build = value.strip().lstrip("v")
    if not re.fullmatch(r"\d{4,7}", build):
        raise ValueError("version must look like 2.55.17.97771 or 97771")

    cache = cache_root / "VERSIONS.md"
    try:
        data = _http_get_bytes(VERSIONS_RAW_URL).decode("utf-8", errors="replace")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(data, encoding="utf-8")
    except Exception:
        if not cache.exists():
            raise
        data = cache.read_text(encoding="utf-8", errors="replace")

    matches = sorted(set(re.findall(rf"\b(\d+\.\d+\.\d+\.{re.escape(build)})\b", data)))
    if len(matches) != 1:
        raise RuntimeError(f"build {build}: expected one version in VERSIONS.md, found {matches}")
    return matches[0]


def _win_long_path(path: pathlib.Path) -> str:
    """Return an extended-length Windows path; no-op on other platforms."""
    s = str(path.resolve())
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        # UNC path -> \\?\UNC\server\share\...
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _rmtree_long_path(path: pathlib.Path) -> None:
    if not path.exists():
        return
    if os.name == "nt":
        shutil.rmtree(_win_long_path(path), ignore_errors=False)
    else:
        shutil.rmtree(path)


def default_extract_root(cache_root: pathlib.Path) -> pathlib.Path:
    """Use a short extraction root on Windows to avoid MAX_PATH failures.

    The downloaded archive remains in the project-local cache. Only the expanded
    source tree is placed under the short system temp path. Override with
    PYHOTS_EXTRACT_CACHE when desired.
    """
    override = os.environ.get("PYHOTS_EXTRACT_CACHE")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    if os.name == "nt":
        return pathlib.Path(tempfile.gettempdir()).resolve() / "PyhotsGD"
    return cache_root / "extracted"


def safe_extract_zip(archive: pathlib.Path, dest: pathlib.Path) -> pathlib.Path:
    """Safely extract a GitHub source ZIP, flattening its single top directory.

    Python's ZipFile.extractall() can still hit WinError 206 when the caller's
    project path is already long. We therefore write members ourselves using
    extended-length Windows paths and strip GitHub's repository-version prefix.
    """
    marker = dest / ".pyhots_extract_complete_v2"
    if marker.exists():
        return dest

    if dest.exists():
        _rmtree_long_path(dest)
    os.makedirs(_win_long_path(dest), exist_ok=True)

    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        first_parts = []
        for info in infos:
            name = info.filename.replace("\\", "/").lstrip("/")
            if not name:
                continue
            parts = pathlib.PurePosixPath(name).parts
            if parts:
                first_parts.append(parts[0])
        common_prefix = first_parts[0] if first_parts and all(x == first_parts[0] for x in first_parts) else None

        for info in infos:
            raw = info.filename.replace("\\", "/").lstrip("/")
            if not raw:
                continue
            parts = list(pathlib.PurePosixPath(raw).parts)
            if common_prefix and parts and parts[0] == common_prefix:
                parts = parts[1:]
            if not parts:
                continue
            if any(part in ("", ".", "..") for part in parts):
                raise RuntimeError(f"unsafe zip member: {info.filename}")
            # Reject symlinks from Unix-created ZIPs; GameData archive does not need them.
            unix_mode = (info.external_attr >> 16) & 0xFFFF
            if (unix_mode & 0o170000) == 0o120000:
                raise RuntimeError(f"symlink zip member is not allowed: {info.filename}")

            target = dest.joinpath(*parts)
            if info.is_dir() or raw.endswith("/"):
                os.makedirs(_win_long_path(target), exist_ok=True)
                continue
            os.makedirs(_win_long_path(target.parent), exist_ok=True)
            with z.open(info, "r") as src, open(_win_long_path(target), "wb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)

    marker.write_text("ok\n", encoding="utf-8")
    return dest


def mod_name_for(path: pathlib.Path) -> str:
    for part in path.parts:
        low = part.lower()
        if low.endswith(".stormmod"):
            return low
    return "UNRESOLVED_MOD"


def mod_priority(mod: str) -> tuple[int, str]:
    m = mod.lower()
    if m == "core.stormmod":
        return (0, m)
    if m == "heroesdata.stormmod":
        return (1, m)
    return (2, m)


def _keep_xml_type(tag: str) -> bool:
    return tag == "const" or any(tag.startswith(p) for p in INCLUDE_PREFIX)


def _desc_node(el: ET.Element, *, tag: str, child_ordinal: int, same_tag_ordinal: int,
               depth: int, path: list[str], ancestor_context: list[dict[str, Any]] | None) -> dict[str, Any]:
    node: dict[str, Any] = {
        "tag": tag,
        "attrs": dict(el.attrib),
        "child_ordinal": child_ordinal,
        "same_tag_ordinal": same_tag_ordinal,
        "depth": depth,
        "path": list(path),
    }
    if ancestor_context is not None:
        node["ancestor_context"] = ancestor_context
    return node


def parse_definition(el: ET.Element, *, file: pathlib.Path, mod: str, order: int) -> dict[str, Any] | None:
    oid = el.attrib.get("id")
    if not oid:
        return None
    xml_type = el.tag.split("}")[-1]
    children: list[dict[str, Any]] = []
    descendants: list[dict[str, Any]] = []

    direct_counts: collections.Counter[str] = collections.Counter()
    for child_ord, child in enumerate(list(el)):
        tag = child.tag.split("}")[-1]
        same = direct_counts[tag]
        direct_counts[tag] += 1
        c = {"tag": tag, "attrs": dict(child.attrib), "child_ordinal": child_ord, "same_tag_ordinal": same}
        children.append(c)

    def walk(parent: ET.Element, depth: int, path: list[str], vet_context: list[dict[str, Any]] | None) -> None:
        counts: collections.Counter[str] = collections.Counter()
        for child_ord, child in enumerate(list(parent)):
            tag = child.tag.split("}")[-1]
            same = counts[tag]
            counts[tag] += 1
            child_path = path + [tag]
            current_identity = {"tag": tag, "child_ordinal": child_ord, "same_tag_ordinal": same}
            if "index" in child.attrib:
                current_identity["index"] = child.attrib["index"]
            if vet_context is not None:
                child_vet_context = vet_context + [current_identity]
            elif depth == 1 and tag == "VeterancyLevelArray":
                child_vet_context = [current_identity]
            else:
                child_vet_context = None
            descendants.append(_desc_node(
                child, tag=tag, child_ordinal=child_ord, same_tag_ordinal=same,
                depth=depth, path=child_path, ancestor_context=child_vet_context,
            ))
            walk(child, depth + 1, child_path, child_vet_context)

    walk(el, 1, [], None)
    return {
        "xml_type": xml_type,
        "object_id": oid,
        "attrs": dict(el.attrib),
        "children": children,
        "descendants": descendants,
        "file": str(file.resolve()),
        "mod": mod,
        "order": order,
    }


def iter_xml_files(repo_root: pathlib.Path) -> list[pathlib.Path]:
    mods_root = repo_root / "mods"
    if not mods_root.exists():
        raise RuntimeError(f"mods directory missing: {mods_root}")
    files = [p for p in mods_root.rglob("*.xml") if p.is_file()]
    files.sort(key=lambda p: (mod_priority(mod_name_for(p)), str(p.relative_to(mods_root)).lower()))
    return files


def build_hero_compact_from_raw(repo_root: pathlib.Path, output: pathlib.Path, version: str) -> dict[str, Any]:
    objects: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    excluded = collections.Counter()
    parse_errors: list[dict[str, str]] = []
    order = 0
    kept_defs = 0
    files = iter_xml_files(repo_root)
    print(f"[catalog] XML files: {len(files)}")
    for idx, path in enumerate(files, 1):
        rel = str(path.relative_to(repo_root)).replace("\\", "/")
        low_rel = rel.lower()
        bad = next((x for x in EXCLUDED_PATH_TOKENS if x in low_rel), None)
        try:
            root = ET.parse(path).getroot()
        except Exception as exc:
            parse_errors.append({"file": rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        mod = mod_name_for(path)
        for el in list(root):
            order += 1  # preserve full-catalog ordering even for types omitted from Hero compact
            xml_type = el.tag.split("}")[-1]
            if not _keep_xml_type(xml_type):
                continue
            if bad:
                excluded[bad] += 1
                continue
            d = parse_definition(el, file=path, mod=mod, order=order)
            if d is None:
                continue
            objects[d["object_id"]].append(d)
            kept_defs += 1
        if idx % 250 == 0 or idx == len(files):
            print(f"\r[catalog] parsed {idx}/{len(files)} files; kept {kept_defs} defs", end="", flush=True)
    print()
    if parse_errors:
        raise RuntimeError("XML parse errors encountered; fail-closed: " + json.dumps(parse_errors[:10], ensure_ascii=False))

    compact = {
        "metadata": {
            "builder": "pyhots_version_raw_xml_compact_v0_1",
            "source_builder": "storm_catalog_index_v2_4_ancestor_context_compatible_direct_raw_xml",
            "source_ancestor_context_retained": True,
            "ancestor_context_retained": True,
            "source_version": version,
            "source_tag": f"v{version}",
            "source_repository": REPOSITORY,
            "global_provenance_filter": True,
            "definitions": kept_defs,
            "xml_files": len(files),
            "excluded_definition_counts": dict(sorted(excluded.items())),
            "ordering": "core.stormmod -> heroesdata.stormmod -> other mods lexicographic; XML paths lexicographic; top-level document order",
        },
        "objects": dict(sorted(objects.items())),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, compact)
    return compact


def find_case_insensitive(root: pathlib.Path, filename: str) -> pathlib.Path | None:
    want = filename.lower()
    hits = [p for p in root.rglob("*") if p.is_file() and p.name.lower() == want]
    if not hits:
        return None
    hits.sort(key=lambda p: (0 if "heroesdata.stormmod" in str(p).lower() else 1, len(str(p)), str(p).lower()))
    return hits[0]


def provenance_files(repo_root: pathlib.Path) -> dict[str, Any]:
    wanted = {"buildid.txt", "databuildid.txt", "source_build.txt"}
    out: dict[str, Any] = {}
    for p in repo_root.rglob("*"):
        if p.is_file() and p.name.lower() in wanted:
            rel = str(p.relative_to(repo_root)).replace("\\", "/")
            try:
                value = p.read_text(encoding="utf-8", errors="replace").strip()
            except Exception:
                value = None
            out[rel] = {"value": value, "sha256": sha256(p)}
    return dict(sorted(out.items()))


def verify_baseline(version: str, db: dict[str, Any]) -> dict[str, Any]:
    expected = BASELINE_EXPECTATIONS.get(version)
    summary = db.get("summary") or {}
    if expected is None:
        return {
            "status": "NO_FROZEN_EXPECTATION_FOR_VERSION",
            "version": version,
            "freeze_permission": False,
            "note": "Build output is available, but this version has no project-frozen metric baseline yet.",
        }
    actual_map = {
        "heroes": summary.get("heroes"),
        "forms": summary.get("forms"),
        "unique_owned_entities": summary.get("unique_owned_entities"),
        "chero_level_scaling_records": summary.get("chero_level_scaling_records"),
        "typed_level_scaling_records": summary.get("typed_level_scaling_records"),
        "typed_execution_unresolved": summary.get("typed_execution_unresolved"),
        "overall": summary.get("overall"),
    }
    mismatches = {k: {"expected": v, "actual": actual_map.get(k)} for k, v in expected.items() if actual_map.get(k) != v}
    return {
        "status": "PASS" if not mismatches else "FAIL",
        "version": version,
        "expected": expected,
        "actual": actual_map,
        "mismatches": mismatches,
        "freeze_permission": not mismatches,
    }


def run_pipeline(version_input: str, root: pathlib.Path, *, force_download: bool = False,
                 force_rebuild: bool = False) -> dict[str, Any]:
    cache_root = root / "cache"
    version = resolve_version(version_input, cache_root)
    tag = f"v{version}"
    print(f"[version] {version} ({tag})")

    archive = cache_root / "archives" / f"HeroesOfTheStorm_Gamedata-{tag}.zip"
    used_url = download_file(
        [ARCHIVE_URL_TEMPLATE.format(tag=tag), GITHUB_ARCHIVE_FALLBACK.format(tag=tag)],
        archive, force=force_download,
    )
    archive_hash = sha256(archive)
    extract_root = default_extract_root(cache_root)
    repo_root = safe_extract_zip(archive, extract_root / tag)
    print(f"[source] {repo_root}")

    build_root = root / "builds" / tag
    db_dir = build_root / "hero_unit_db"
    compact_path = db_dir / "hero_global_compact.json"
    static_db_path = db_dir / "hero_unit_db_static_v0_2.json"
    db_path = db_dir / "hero_unit_db.json"
    summary_path = db_dir / "summary.json"
    integrity_path = db_dir / "integrity_report.json"
    app_path = db_dir / "scaling_application_semantics.json"
    provenance_path = build_root / "source_provenance.json"

    prov = {
        "schema": {"name": "PYHOTS_VERSION_SOURCE_PROVENANCE", "version": TOOL_VERSION},
        "requested_version": version_input,
        "resolved_version": version,
        "tag": tag,
        "repository": REPOSITORY,
        "archive_url": used_url,
        "archive_path": str(archive.resolve()),
        "archive_sha256": archive_hash,
        "extracted_root": str(repo_root.resolve()),
        "build_metadata_files": provenance_files(repo_root),
    }
    build_root.mkdir(parents=True, exist_ok=True)
    provenance_path.write_text(json.dumps(prov, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if force_rebuild or not compact_path.exists():
        print("[1/5] building Hero-compatible compact catalog directly from exact-version raw XML...")
        compact = build_hero_compact_from_raw(repo_root, compact_path, version)
    else:
        print(f"[1/5] compact cache hit: {compact_path}")
        compact = json.loads(compact_path.read_text(encoding="utf-8"))

    print("[2/5] building static Hero Unit DB v0.2 evidence layer...")
    static_db = build_hero_db(compact)
    db_dir.mkdir(parents=True, exist_ok=True)
    save(static_db_path, static_db)

    heroes_lib = find_case_insensitive(repo_root, "HeroesLib.galaxy")
    game_lib = find_case_insensitive(repo_root, "GameLib.galaxy")
    if not heroes_lib or not game_lib:
        missing = [n for n, p in (("HeroesLib.galaxy", heroes_lib), ("GameLib.galaxy", game_lib)) if p is None]
        raise RuntimeError(f"build-matched Galaxy scripts missing: {missing}")

    print("[3/5] proving build-matched scaling application semantics from the same source tree...")
    app = build_scaling_application_evidence(
        static_db_path,
        compact_path,
        heroes_lib_path=heroes_lib,
        game_lib_path=game_lib,
        compact_build=version,
        script_build=version,
    )
    app.setdefault("inputs", {})["version_source_provenance"] = {
        "tag": tag,
        "archive_sha256": archive_hash,
        "same_extracted_tree_for_xml_and_galaxy": True,
    }
    app_path.write_text(json.dumps(app, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("[4/5] promoting build-matched runtime scaling semantics into canonical Hero Unit DB v0.2.4...")
    db = promote_runtime_scaling(static_db_path, compact_path, app_path)
    save(db_path, db)
    save(summary_path, db["summary"])
    save(integrity_path, db["integrity"])

    compat = verify_baseline(version, db)
    (build_root / "version_compatibility.json").write_text(
        json.dumps(compat, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"[5/5] Hero DB integrity={db['integrity']['overall']} version_compatibility={compat['status']}")

    manifest = {
        "schema": {"name": "PYHOTS_VERSION_HERO_DB_BUILD", "version": TOOL_VERSION},
        "version": version,
        "tag": tag,
        "outputs": {
            "hero_global_compact": {"path": str(compact_path.resolve()), "sha256": sha256(compact_path)},
            "hero_unit_db_static_v0_2": {"path": str(static_db_path.resolve()), "sha256": sha256(static_db_path)},
            "hero_unit_db": {"path": str(db_path.resolve()), "sha256": sha256(db_path)},
            "summary": {"path": str(summary_path.resolve()), "sha256": sha256(summary_path)},
            "integrity_report": {"path": str(integrity_path.resolve()), "sha256": sha256(integrity_path)},
            "scaling_application_semantics": {"path": str(app_path.resolve()), "sha256": sha256(app_path)},
            "source_provenance": {"path": str(provenance_path.resolve()), "sha256": sha256(provenance_path)},
        },
        "hero_db_integrity": db["integrity"]["overall"],
        "version_compatibility": compat,
        "scaling_application_integrity": app.get("integrity", {}).get("overall"),
        "freeze_permission": bool(
            db["integrity"]["overall"] == "PASS"
            and compat.get("freeze_permission")
            and app.get("build_matched_galaxy_evidence", {}).get("status") == "PROVEN_BUILD_MATCHED_GALAXY_APPLICATION"
        ),
    }
    manifest_path = build_root / "build_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("\n=== RESULT ===")
    print(f"version:                 {version}")
    print(f"Hero DB:                 {db['integrity']['overall']}")
    print(f"baseline compatibility:  {compat['status']}")
    print(f"Galaxy scaling proof:    {app.get('build_matched_galaxy_evidence', {}).get('status')}")
    print(f"freeze permission:       {manifest['freeze_permission']}")
    print(f"output:                  {build_root}")

    if db["integrity"]["overall"] != "PASS":
        raise SystemExit(2)
    if compat.get("status") == "FAIL":
        raise SystemExit(3)
    if app.get("integrity", {}).get("overall") != "PASS":
        raise SystemExit(4)
    return manifest


def main() -> None:
    ap = argparse.ArgumentParser(description="Download exact HOTS GameData version and build Pyhots Hero Unit DB")
    ap.add_argument("version", nargs="?", help="full version (2.55.17.97771) or build number (97771)")
    ap.add_argument("--root", default=None, help="archive/build root; default: <script>/versioned_data")
    ap.add_argument("--extract-root", default=None, help="optional short source extraction cache; on Windows defaults to the system temporary directory\\PyhotsGD")
    ap.add_argument("--force-download", action="store_true")
    ap.add_argument("--force-rebuild", action="store_true")
    ns = ap.parse_args()

    version = ns.version
    if not version:
        try:
            version = input("HOTS version/build (example 2.55.17.97771 or 97771): ").strip()
        except EOFError:
            version = ""
    if not version:
        raise SystemExit("version is required")
    root = pathlib.Path(ns.root).expanduser().resolve() if ns.root else (pathlib.Path(__file__).resolve().parent / "versioned_data")
    if ns.extract_root:
        os.environ["PYHOTS_EXTRACT_CACHE"] = str(pathlib.Path(ns.extract_root).expanduser().resolve())
    run_pipeline(version, root, force_download=ns.force_download, force_rebuild=ns.force_rebuild)


if __name__ == "__main__":
    main()
