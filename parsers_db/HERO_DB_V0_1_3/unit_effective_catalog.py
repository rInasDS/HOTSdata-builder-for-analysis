from __future__ import annotations
from collections import defaultdict, Counter, deque
import re
from pathlib import Path
from json_backend import load, save
from effective_catalog import EffectiveCatalogCompiler
from typed_reference_resolver import resolve_ambiguous_candidate

UNIT_PREFIXES = (
    "CAbil", "CEffect", "CBehavior", "CWeapon", "CValidator", "CAccumulator",
    "CUnit", "CArmor", "CUpgrade", "CMover", "CFootprint", "CTurret"
)
_CONST_TOKEN_RE = re.compile(r"\$[A-Za-z0-9_]+")
NON_REFERENCE_ATTRS = {"id", "index", "removed"}


def _is_unit_semantic_type(xml_type: str) -> bool:
    return bool(xml_type) and (xml_type == "const" or xml_type.startswith(UNIT_PREFIXES))


def _looks_like_catalog_id(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    s = value.strip()
    if not s or s != value or s[0] == "$":
        return False
    try:
        float(s)
        return False
    except ValueError:
        return True


def _build_raw_reference_index(raw):
    all_keys_by_id = defaultdict(list)
    semantic_keys_by_id = defaultdict(list)
    defs_by_key = defaultdict(list)
    for oid, defs in raw.get("objects", {}).items():
        seen_all, seen_sem = set(), set()
        for d in defs:
            xt = d.get("xml_type", "")
            key = f"{xt}::{oid}"
            if key not in seen_all:
                all_keys_by_id[oid].append(key); seen_all.add(key)
            if _is_unit_semantic_type(xt):
                if key not in seen_sem:
                    semantic_keys_by_id[oid].append(key); seen_sem.add(key)
                defs_by_key[key].append(d)
    return all_keys_by_id, semantic_keys_by_id, defs_by_key, set(defs_by_key)


def _resolve_parent_target_key(child_type, parent_id, keys_by_id, key_set):
    exact = f"{child_type}::{parent_id}"
    if exact in key_set:
        return exact, "TYPE_SCOPED"
    candidates = []
    for key in keys_by_id.get(parent_id, []):
        ptype = key.split("::", 1)[0]
        if ptype and child_type and child_type.startswith(ptype):
            candidates.append(key)
    if not candidates:
        return None, None
    max_len = max(len(k.split("::", 1)[0]) for k in candidates)
    best = [k for k in candidates if len(k.split("::", 1)[0]) == max_len]
    return (best[0], "BASE_CLASS_PREFIX") if len(best) == 1 else (None, None)


def _edges_for_key(key, defs, all_keys_by_id, semantic_keys_by_id, key_set):
    resolved, ambiguous = [], []
    for d in defs:
        parent = d.get("attrs", {}).get("parent")
        if parent:
            pkey, pres = _resolve_parent_target_key(d.get("xml_type"), parent, semantic_keys_by_id, key_set)
            if pkey:
                resolved.append({
                    "source_key": key, "target_key": pkey, "target_id": parent,
                    "field": None, "path": [], "attr": "parent", "depth": 0,
                    "relation": "INHERITS", "resolution": pres,
                    "provenance": {"file": d.get("file"), "mod": d.get("mod"), "order": d.get("order")},
                })

        def consume(attr, value, path, depth):
            if attr in NON_REFERENCE_ATTRS or attr == "parent" or not isinstance(value, str):
                return
            prov = {"file": d.get("file"), "mod": d.get("mod"), "order": d.get("order")}
            for token in _CONST_TOKEN_RE.findall(value):
                cands = [k for k in semantic_keys_by_id.get(token, []) if k.startswith("const::")]
                if len(cands) == 1:
                    resolved.append({
                        "source_key": key, "target_key": cands[0], "target_id": token,
                        "field": path[-1] if path else None, "path": list(path), "attr": attr,
                        "depth": depth, "relation": "FORMULA_CONSTANT", "resolution": "CONST_TOKEN",
                        "provenance": prov,
                    })
            if not _looks_like_catalog_id(value):
                return
            sem_cands = semantic_keys_by_id.get(value, [])
            if not sem_cands:
                return
            all_cands = all_keys_by_id.get(value, [])
            base = {
                "source_key": key, "target_id": value, "field": path[-1] if path else None,
                "path": list(path), "attr": attr, "depth": depth,
                "relation": "CANDIDATE_REFERENCE", "provenance": prov,
            }
            if len(all_cands) == 1 and len(sem_cands) == 1:
                e = dict(base); e["target_key"] = sem_cands[0]; e["resolution"] = "UNIQUE_CATALOG_ID"; resolved.append(e)
            else:
                a = dict(base); a["candidate_keys"] = all_cands; a["replay_candidate_keys"] = sem_cands
                a["resolution"] = "AMBIGUOUS_TARGET_TYPE"
                typed = resolve_ambiguous_candidate(a)
                target_key = typed.get("target_key")
                if target_key in key_set:
                    e = dict(base); e["target_key"] = target_key; e["resolution"] = "TYPED_FIELD"
                    e["candidate_keys"] = all_cands; e["replay_candidate_keys"] = sem_cands
                    e["typed_relation_hint"] = typed["rule"].relation; resolved.append(e)
                else:
                    a["typed_status"] = typed.get("status")
                    a["typed_relation_hint"] = typed["rule"].relation
                    a["expected_prefixes"] = list(typed["rule"].expected_prefixes)
                    a["matching_candidate_keys"] = typed.get("matches", [])
                    ambiguous.append(a)

        for attr, value in (d.get("attrs") or {}).items():
            consume(attr, value, [], 0)
        for node in d.get("descendants", []) or []:
            path = node.get("path") or [node.get("tag")]
            depth = int(node.get("depth") or len(path) or 1)
            for attr, value in (node.get("attrs") or {}).items():
                consume(attr, value, path, depth)

    def dedup(items, amb=False):
        uniq = {}
        for e in items:
            tgt = tuple(e.get("candidate_keys", [])) if amb else e.get("target_key")
            sig = (tgt, tuple(e["path"]), e["attr"], e["relation"], e["provenance"].get("file"), e["provenance"].get("order"))
            uniq[sig] = e
        return list(uniq.values())
    return dedup(resolved), dedup(ambiguous, True)


def build_unit_effective_catalog_data(raw: dict, seed_unit_ids: list[str]):
    all_keys_by_id, sem_keys_by_id, defs_by_key, key_set = _build_raw_reference_index(raw)

    seed_audit, seed_keys = [], set()
    for uid in sorted(set(seed_unit_ids)):
        cands = [k for k in sem_keys_by_id.get(uid, []) if k.startswith("CUnit")]
        if len(cands) == 1:
            seed_keys.add(cands[0]); status = "RESOLVED_UNIT_ROOT"
        elif len(cands) > 1:
            exact = [k for k in cands if k.startswith("CUnit::")]
            if len(exact) == 1:
                seed_keys.add(exact[0]); cands = exact; status = "RESOLVED_EXACT_CUNIT_ROOT"
            else:
                status = "AMBIGUOUS_UNIT_ROOT"
        else:
            status = "ABSENT_UNIT_ROOT"
        seed_audit.append({"unit_id": uid, "status": status, "candidate_keys": cands, "all_catalog_keys": all_keys_by_id.get(uid, [])})

    q = deque(sorted(seed_keys))
    selected, graph, ambiguous_graph = set(), {}, {}
    while q:
        key = q.popleft()
        if key in selected:
            continue
        selected.add(key)
        edges, amb = _edges_for_key(key, defs_by_key.get(key, []), all_keys_by_id, sem_keys_by_id, key_set)
        graph[key], ambiguous_graph[key] = edges, amb
        for e in edges:
            t = e.get("target_key")
            if t and t not in selected:
                q.append(t)

    compiler = EffectiveCatalogCompiler(raw)
    records = {}
    for key in sorted(selected):
        xt, oid = key.split("::", 1)
        rec = compiler.resolve((xt, oid))
        if rec is not None:
            records[key] = rec

    selected_edges, selected_amb = [], []
    for key in sorted(selected):
        for e in graph.get(key, []):
            if e.get("target_key") in records:
                selected_edges.append(e)
        selected_amb.extend(ambiguous_graph.get(key, []))

    blocking = []
    record_keys = set(records)
    for u in compiler.unresolved:
        x = dict(u)
        x["unit_blocking"] = f"{x.get('type')}::{x.get('id')}" in record_keys
        if x["unit_blocking"]:
            blocking.append(x)

    result = {
        "metadata": {
            "builder": "pyhots_unit_effective_catalog_v0_1_5",
            "scope": "replay-observed non-roster unit dependency closure",
            "fail_closed": True,
            "prefixes": list(UNIT_PREFIXES),
            "map_context": (raw.get("metadata") or {}).get("map_context"),
        },
        "seed_audit": seed_audit,
        "records": records,
        "reference_edges": selected_edges,
        "ambiguous_reference_candidates": selected_amb,
        "unresolved_unit_blocking": blocking,
    }
    report = {
        "summary": {
            "seed_units": len(seed_audit),
            "resolved_unit_roots": sum(x["status"].startswith("RESOLVED") for x in seed_audit),
            "absent_unit_roots": sum(x["status"] == "ABSENT_UNIT_ROOT" for x in seed_audit),
            "ambiguous_unit_roots": sum(x["status"] == "AMBIGUOUS_UNIT_ROOT" for x in seed_audit),
            "dependency_records": len(records),
            "reference_edges": len(selected_edges),
            "ambiguous_reference_candidates": len(selected_amb),
            "blocking_unresolved": len(blocking),
        },
        "seed_unresolved": [x for x in seed_audit if not x["status"].startswith("RESOLVED")],
        "blocking_counts": dict(Counter(x.get("code", "UNKNOWN") for x in blocking)),
        "limitations": [
            "Dependency reachability does not imply runtime execution or ownership.",
            "Only typed execution/ownership edges may be used for reactive/combat semantics.",
            "Nested patch normalization remains fail-closed where effective inheritance cannot prove a value.",
        ],
    }
    return result, report


def build_unit_effective_catalog(raw_index_path: Path, seed_unit_ids: list[str], out_path: Path, report_path: Path):
    raw = load(raw_index_path)
    result, report = build_unit_effective_catalog_data(raw, seed_unit_ids)
    save(out_path, result)
    save(report_path, report)
    return report
