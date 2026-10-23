from __future__ import annotations
from collections import defaultdict, Counter, deque
import re
from pathlib import Path
from json_backend import load, save
from effective_catalog import EffectiveCatalogCompiler
from typed_reference_resolver import resolve_ambiguous_candidate

REPLAY_PREFIXES = (
    "CAbil", "CEffect", "CBehavior", "CWeapon", "CValidator", "CAccumulator", "CUnit", "CTalent"
)
_CONST_TOKEN_RE = re.compile(r"\$[A-Za-z0-9_]+")
NON_REFERENCE_ATTRS = {"id", "index", "removed"}


def _is_replay_type(xml_type: str) -> bool:
    return bool(xml_type) and (xml_type == "const" or xml_type.startswith(REPLAY_PREFIXES))


def _looks_like_catalog_id(value: object) -> bool:
    """Lexical gate only. Semantics are deliberately assigned later.

    Numeric values are excluded because the catalog contains numeric object IDs
    (e.g. '4', '30'), which otherwise turn cooldown/radius/amount literals into
    false graph edges.
    """
    if not isinstance(value, str) or not value:
        return False
    s = value.strip()
    if not s or s != value:
        return False
    if s[0] == "$":
        return False
    try:
        float(s)
        return False
    except ValueError:
        pass
    return True


def _collect_seed_ids(herodata):
    """Explicit replay roots with expected catalog families."""
    seeds = []
    def add(object_id, family, reason):
        if object_id and object_id != ":PASSIVE:":
            seeds.append({"id": object_id, "family": family, "reason": reason})
    for hero_name, hero in herodata.get("heroes", {}).items():
        add(hero.get("unit_id"), "CUnit", f"hero_unit:{hero_name}")
        for uid in hero.get("summoned_unit_ids", []) or []:
            add(uid, "CUnit", f"summoned_unit:{hero_name}")
        for weapon in hero.get("weapons", []) or []:
            add(weapon.get("nameId") or weapon.get("id"), "CWeapon", f"hero_weapon:{hero_name}")
        for ability in hero.get("abilities", []) or []:
            add(ability.get("ability_id") or ability.get("id"), "CAbil", f"ability:{hero_name}")
        for talent in hero.get("talents", []) or []:
            # Talent IDs are predominantly CButton identities. Keep them as audit roots,
            # but do not force them into a replay family.
            add(talent.get("talent_id"), "CTalent", f"talent_id:{hero_name}")
            add(talent.get("ability_id"), "CAbil", f"talent_ability:{hero_name}")
            for link in talent.get("tooltip_ability_link_ids", []) or []:
                if isinstance(link, str) and link:
                    add(link.split("|", 1)[0], "CAbil", f"talent_tooltip_ability:{hero_name}")
    return seeds

def _definition_reference_edges(defn, source_key, known_by_id):
    """Return exact-catalog-id candidate references from the complete raw XML fact set.

    This is intentionally a *candidate reference graph*, not an ownership graph.
    Every edge retains field/path/provenance so the semantic layer can later label
    it EXECUTES/ACTIVATES/DEACTIVATES/CONDITIONS/etc.
    """
    out = []
    seen = set()

    def add(target_id, path, attr, depth, relation="CANDIDATE_REFERENCE"):
        if target_id not in known_by_id:
            return
        for target_key in known_by_id[target_id]:
            sig = (target_key, tuple(path), attr, relation)
            if sig in seen:
                continue
            seen.add(sig)
            out.append({
                "source_key": source_key,
                "target_key": target_key,
                "target_id": target_id,
                "field": path[-1] if path else None,
                "path": list(path),
                "attr": attr,
                "depth": depth,
                "relation": relation,
                "provenance": {
                    "file": defn.get("file"),
                    "mod": defn.get("mod"),
                    "order": defn.get("order"),
                },
            })

    parent = defn.get("attrs", {}).get("parent")
    if parent:
        # Catalog inheritance is type-scoped; do not fan out to unrelated same-ID types.
        same_type_key = f"{defn.get('xml_type')}::{parent}"
        if same_type_key in {k for keys in known_by_id.values() for k in keys}:
            sig = (same_type_key, (), "parent", "INHERITS")
            if sig not in seen:
                seen.add(sig)
                out.append({
                    "source_key": source_key,
                    "target_key": same_type_key,
                    "target_id": parent,
                    "field": None,
                    "path": [],
                    "attr": "parent",
                    "depth": 0,
                    "relation": "INHERITS",
                    "provenance": {"file": defn.get("file"), "mod": defn.get("mod"), "order": defn.get("order")},
                })

    # Root attributes can themselves contain catalog links.
    for attr, value in defn.get("attrs", {}).items():
        if attr in NON_REFERENCE_ATTRS or not _looks_like_catalog_id(value):
            continue
        if value in known_by_id:
            add(value, [], attr, 0)

    # descendants includes immediate children and all nested XML nodes.
    for node in defn.get("descendants", []) or []:
        path = node.get("path") or [node.get("tag")]
        depth = int(node.get("depth") or len(path) or 1)
        for attr, value in (node.get("attrs") or {}).items():
            if attr in NON_REFERENCE_ATTRS or not _looks_like_catalog_id(value):
                continue
            if value in known_by_id:
                add(value, path, attr, depth)
    return out


def _build_raw_reference_index(raw):
    all_keys_by_id = defaultdict(list)
    replay_keys_by_id = defaultdict(list)
    defs_by_key = defaultdict(list)
    for oid, defs in raw.get("objects", {}).items():
        seen_all=set(); seen_replay=set()
        for d in defs:
            xt = d.get("xml_type", "")
            key = f"{xt}::{oid}"
            if key not in seen_all:
                all_keys_by_id[oid].append(key); seen_all.add(key)
            if _is_replay_type(xt):
                if key not in seen_replay:
                    replay_keys_by_id[oid].append(key); seen_replay.add(key)
                defs_by_key[key].append(d)
    return all_keys_by_id, replay_keys_by_id, defs_by_key, set(defs_by_key)


def _resolve_parent_target_key(child_type, parent_id, replay_keys_by_id, replay_key_set):
    exact=f"{child_type}::{parent_id}"
    if exact in replay_key_set:
        return exact, "TYPE_SCOPED"
    candidates=[]
    for key in replay_keys_by_id.get(parent_id,[]):
        ptype=key.split("::",1)[0]
        if ptype and child_type and child_type.startswith(ptype):
            candidates.append(key)
    if not candidates:
        return None, None
    max_len=max(len(k.split("::",1)[0]) for k in candidates)
    best=[k for k in candidates if len(k.split("::",1)[0])==max_len]
    if len(best)==1:
        return best[0], "BASE_CLASS_PREFIX"
    return None, None


def _edges_for_key(key, defs, all_keys_by_id, replay_keys_by_id, replay_key_set):
    resolved=[]; ambiguous=[]
    for d in defs:
        parent=d.get("attrs",{}).get("parent")
        if parent:
            parent_key,parent_resolution=_resolve_parent_target_key(d.get("xml_type"),parent,replay_keys_by_id,replay_key_set)
            if parent_key:
                resolved.append({
                    "source_key":key,"target_key":parent_key,"target_id":parent,
                    "field":None,"path":[],"attr":"parent","depth":0,
                    "relation":"INHERITS","resolution":parent_resolution,
                    "provenance":{"file":d.get("file"),"mod":d.get("mod"),"order":d.get("order")},
                })

        def consume(attr,value,path,depth):
            if attr in NON_REFERENCE_ATTRS or attr=="parent" or not isinstance(value,str): return
            prov={"file":d.get("file"),"mod":d.get("mod"),"order":d.get("order")}
            # Formula constants are real replay dependencies. Preserve the expression edge
            # separately from ordinary catalog links; semantic evaluation happens later.
            for token in _CONST_TOKEN_RE.findall(value):
                cands=[k for k in replay_keys_by_id.get(token,[]) if k.startswith("const::")]
                if len(cands)==1:
                    resolved.append({"source_key":key,"target_key":cands[0],"target_id":token,
                        "field":path[-1] if path else None,"path":list(path),"attr":attr,"depth":depth,
                        "relation":"FORMULA_CONSTANT","resolution":"CONST_TOKEN","provenance":prov})
            if not _looks_like_catalog_id(value): return
            replay_candidates=replay_keys_by_id.get(value,[])
            if not replay_candidates: return
            candidates=all_keys_by_id.get(value,[])
            base={"source_key":key,"target_id":value,"field":path[-1] if path else None,
                  "path":list(path),"attr":attr,"depth":depth,"relation":"CANDIDATE_REFERENCE",
                  "provenance":prov}
            if len(candidates)==1 and len(replay_candidates)==1:
                e=dict(base);e["target_key"]=replay_candidates[0];e["resolution"]="UNIQUE_CATALOG_ID";resolved.append(e)
            else:
                a=dict(base);a["candidate_keys"]=candidates;a["replay_candidate_keys"]=replay_candidates
                a["resolution"]="AMBIGUOUS_TARGET_TYPE"
                typed=resolve_ambiguous_candidate(a)
                target_key=typed.get("target_key")
                if target_key in replay_key_set:
                    e=dict(base)
                    e["target_key"]=target_key
                    e["resolution"]="TYPED_FIELD"
                    e["candidate_keys"]=candidates
                    e["replay_candidate_keys"]=replay_candidates
                    e["typed_relation_hint"]=typed["rule"].relation
                    resolved.append(e)
                else:
                    a["typed_status"]=typed.get("status")
                    a["typed_relation_hint"]=typed["rule"].relation
                    a["expected_prefixes"]=list(typed["rule"].expected_prefixes)
                    a["matching_candidate_keys"]=typed.get("matches",[])
                    ambiguous.append(a)

        for attr,value in (d.get("attrs") or {}).items(): consume(attr,value,[],0)
        for node in d.get("descendants",[]) or []:
            path=node.get("path") or [node.get("tag")]
            depth=int(node.get("depth") or len(path) or 1)
            for attr,value in (node.get("attrs") or {}).items(): consume(attr,value,path,depth)

    def dedup(items,amb=False):
        uniq={}
        for e in items:
            tgt=tuple(e.get("candidate_keys",[])) if amb else e.get("target_key")
            sig=(tgt,tuple(e["path"]),e["attr"],e["relation"],e["provenance"].get("file"),e["provenance"].get("order"))
            uniq[sig]=e
        return list(uniq.values())
    return dedup(resolved),dedup(ambiguous,True)

def _duplicate_report(hd):
    dup = []
    for hero, h in hd.get("heroes", {}).items():
        groups = defaultdict(list)
        for a in h.get("abilities", []) or []:
            aid = a.get("ability_id") or a.get("id")
            if aid:
                groups[aid].append({
                    "record_key": a.get("record_key"), "name": a.get("name"),
                    "slot": a.get("slot"), "source": a.get("source")
                })
        for aid, items in groups.items():
            if len(items) > 1:
                dup.append({"hero": hero, "ability_id": aid, "count": len(items), "records": items})
    return {"summary": {"duplicate_ability_ids_within_hero": len(dup)}, "duplicates": dup}


def build_replay_effective_catalog(raw_index_path: Path, herodata_path: Path,
                                   out_path: Path, report_path: Path, duplicate_report_path: Path):
    raw = load(raw_index_path)
    hd = load(herodata_path)
    seed_specs = _collect_seed_ids(hd)
    all_keys_by_id, replay_keys_by_id, defs_by_key, replay_key_set = _build_raw_reference_index(raw)

    seed_keys = set()
    seed_audit = []
    seen_seed_specs=set()
    for spec in seed_specs:
        sig=(spec["id"],spec["family"],spec["reason"])
        if sig in seen_seed_specs: continue
        seen_seed_specs.add(sig)
        sid=spec["id"]; family=spec["family"]
        all_keys=all_keys_by_id.get(sid,[])
        replay_keys=replay_keys_by_id.get(sid,[])
        if family:
            matched=[k for k in replay_keys if k.split("::",1)[0].startswith(family)]
        else:
            matched=list(replay_keys) if len(all_keys)==len(replay_keys)==1 else []
        if matched:
            seed_keys.update(matched); status="RESOLVED_REPLAY_SEED"
        elif family=="CAbil" and len(replay_keys)==1:
            # HeroData occasionally exposes a gameplay ability through a Behavior identity
            # (e.g. ImprovedIceBlock/AnaShrike) rather than CAbil. The HeroData role is
            # authoritative enough to accept a single replay-type fallback, with provenance.
            matched=list(replay_keys); seed_keys.update(matched); status="RESOLVED_REPLAY_SEED_FALLBACK"
        elif all_keys:
            status="NON_REPLAY_OR_AMBIGUOUS_SEED"
        else:
            status="ABSENT_FROM_CATALOG"
        seed_audit.append({**spec,"status":status,"all_catalog_keys":all_keys,"matched_replay_keys":matched})

    q = deque(sorted(seed_keys))
    selected_keys = set()
    graph = {}
    ambiguous_graph = {}
    while q:
        key = q.popleft()
        if key in selected_keys:
            continue
        selected_keys.add(key)
        edges,ambiguous=_edges_for_key(key,defs_by_key.get(key,[]),all_keys_by_id,replay_keys_by_id,replay_key_set)
        graph[key]=edges; ambiguous_graph[key]=ambiguous
        for edge in edges:
            target_key = edge["target_key"]
            if target_key not in selected_keys:
                q.append(target_key)

    # Resolve only replay-selected records. This avoids loading the ~1.3 GB full
    # effective_catalog.json back into memory during Step 2.
    compiler = EffectiveCatalogCompiler(raw)
    selected_records = {}
    for key in sorted(selected_keys):
        xt, oid = key.split("::", 1)
        rec = compiler.resolve((xt, oid))
        if rec is not None:
            selected_records[key] = rec

    blocking = []
    selected_key_set = set(selected_records)
    for u in compiler.unresolved:
        x = dict(u)
        parent = str(x.get("parent") or "")
        if parent.startswith("Base"):
            x["parent_class"] = "EXTERNAL_OR_DEFAULT_TEMPLATE"
        child_key = f"{x.get('type')}::{x.get('id')}"
        x["replay_blocking"] = child_key in selected_key_set
        if x["replay_blocking"]:
            blocking.append(x)

    selected_edges = []
    selected_ambiguous = []
    nested_edges = 0
    for key in sorted(selected_keys):
        for e in graph.get(key, []):
            if e["target_key"] in selected_keys:
                selected_edges.append(e)
                if e.get("depth", 0) > 1:
                    nested_edges += 1
        selected_ambiguous.extend(ambiguous_graph.get(key, []))

    compact = {
        "metadata": {
            "builder": "replay_effective_catalog_v1_5_typed_closure",
            "fail_closed": True,
            "scope": "hero ability/talent/unit/weapon dependency closure (CTalent included)",
            "nested_reference_scan": True,
            "numeric_literal_reference_guard": True,
            "formula_constant_dependencies": True,
            "effective_nested_merge": False,
            "note": "reference_edges are scope links; typed-field ambiguity resolution is used only to discover replay dependencies, not ownership",
            "lazy_reachable_graph": True,
        },
        "seed_audit": seed_audit,
        "records": selected_records,
        "reference_edges": selected_edges,
        "ambiguous_reference_candidates": selected_ambiguous,
        "unresolved_replay_blocking": blocking,
    }
    save(out_path, compact)

    dup_report = _duplicate_report(hd)
    save(duplicate_report_path, dup_report)

    report = {
        "summary": {
            "explicit_seed_specs": len(seed_audit),
            "unique_seed_ids": len({x["id"] for x in seed_audit}),
            "seed_keys_found": len(seed_keys),
            "seed_specs_resolved": sum(x["status"] in {"RESOLVED_REPLAY_SEED","RESOLVED_REPLAY_SEED_FALLBACK"} for x in seed_audit),
            "seed_specs_resolved_fallback": sum(x["status"]=="RESOLVED_REPLAY_SEED_FALLBACK" for x in seed_audit),
            "seed_specs_non_replay_or_ambiguous": sum(x["status"]=="NON_REPLAY_OR_AMBIGUOUS_SEED" for x in seed_audit),
            "seed_specs_absent": sum(x["status"]=="ABSENT_FROM_CATALOG" for x in seed_audit),
            "dependency_record_keys": len(selected_keys),
            "replay_records": len(selected_records),
            "reference_edges": len(selected_edges),
            "ambiguous_reference_candidates": len(selected_ambiguous),
            "nested_reference_edges": nested_edges,
            "replay_blocking_unresolved": len(blocking),
            "duplicate_ability_ids_within_hero": dup_report["summary"]["duplicate_ability_ids_within_hero"],
        },
        "blocking_counts": dict(Counter(x.get("code", "UNKNOWN") for x in blocking)),
        "blocking_parent_classes": dict(Counter(x.get("parent_class", "NORMAL") for x in blocking)),
        "seed_audit_nonresolved": [x for x in seed_audit if x["status"] not in {"RESOLVED_REPLAY_SEED","RESOLVED_REPLAY_SEED_FALLBACK"}],
        "replay_blocking_unresolved": blocking,
        "limitations": [
            "Candidate reference reachability is scope discovery only; it must not be interpreted as ownership.",
            "Ambiguous references are traversed only when structural field semantics prove a single target family; unresolved ambiguity remains fail-closed.",
            "Nested XML references are preserved as edges, but nested field inheritance/patch merge is not yet normalized.",
            "StormMod load-order precedence is not asserted by this stage; conflicting layered patches require provenance-aware resolution.",
        ],
    }
    save(report_path, report)
    return report
