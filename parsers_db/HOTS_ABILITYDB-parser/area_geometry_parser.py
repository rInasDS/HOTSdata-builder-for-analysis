from __future__ import annotations

import re
from collections import Counter, defaultdict

from fact_utils import load, save, ability_records, source_fact
from xml_semantics import ability_starts, traverse, values_for_tag, descendants, const_value


TOKEN_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")

# Reverse references through these raw values are too generic/noisy.
REJECT_RAW_VALUES = {
    "", "0", "1", "true", "false", "None", "none",
}

# Only these actor/shape types are potentially useful as geometry carriers.
ACTOR_GEOMETRY_WHITELIST = {
    "CActorQuad",
    "CActorRegionQuad",
    "CActorRegionCircle",
    "CActorRegionArc",
    "CActorSplat",
    "CActorRange",
    "CActorReflectionQuad",
    "CActorForceBox",
    "CActorForceCylinder",
    "CActorForceConeRoundedEnd",
    "CActorArc",
}

# Explicitly reject obviously non-geometry actor types.
ACTOR_GEOMETRY_BLACKLIST_PREFIXES = (
    "CActorSound",
    "CActorEventMacro",
    "CActorMinimap",
    "CActorSelection",
    "CActorShadow",
    "CActorUnit",
    "CActorModel",
    "CActorSimple",
)

# Reverse-ref context has to look like a semantic object link, not a random scalar.
ALLOWED_REVERSE_ATTRS = {
    "Effect",
    "LaunchEffect",
    "ImpactEffect",
    "InitialEffect",
    "FinalEffect",
    "SearchEffect",
    "Link",
    "AbilCmd",
    "Terms",
    "Model",
    "Unit",
    "Missile",
    "Site",
    "Actor",
}

ALLOWED_REVERSE_TAG_HINTS = (
    "On",
    "Event",
    "Effect",
    "Launch",
    "Impact",
    "Missile",
    "Cursor",
    "Guide",
    "Target",
    "Unit",
    "Actor",
)


def build_reverse_index(objects):
    reverse = defaultdict(list)
    object_ids = set(objects.keys())

    for source_id, definitions in objects.items():
        for d in definitions:
            for node in descendants(d):
                attrs = node.get("attrs", {})
                tag = str(node.get("tag") or "")

                for attr_name, raw in attrs.items():
                    if raw is None:
                        continue

                    text = str(raw).strip()

                    if text in REJECT_RAW_VALUES:
                        continue

                    candidates = set(TOKEN_RE.findall(text))

                    if text in object_ids:
                        candidates.add(text)

                    for target in candidates:
                        if target in REJECT_RAW_VALUES:
                            continue

                        if target not in object_ids:
                            continue

                        reverse[target].append({
                            "source_id": source_id,
                            "xml_type": d.get("xml_type"),
                            "file": d.get("file"),
                            "tag": tag,
                            "attr": attr_name,
                            "raw": text,
                        })

    return reverse


def is_semantic_reverse_ref(ref):
    raw = str(ref.get("raw") or "").strip()
    if raw in REJECT_RAW_VALUES:
        return False

    attr = str(ref.get("attr") or "")
    tag = str(ref.get("tag") or "")

    if attr in ALLOWED_REVERSE_ATTRS:
        return True

    if any(hint in tag for hint in ALLOWED_REVERSE_TAG_HINTS):
        return True

    return False


def actor_type_allowed(xml_type):
    if xml_type in ACTOR_GEOMETRY_WHITELIST:
        return True

    if any(xml_type.startswith(prefix) for prefix in ACTOR_GEOMETRY_BLACKLIST_PREFIXES):
        return False

    return False


def geometry_values(objects, definition):
    tags = {
        "Radius", "Width", "Height", "Length", "Arc", "Range",
        "Scale", "Size", "Quad", "HalfWidth", "HalfHeight",
    }

    out = []

    for node in descendants(definition):
        tag = str(node.get("tag") or "")
        attrs = node.get("attrs", {})

        if tag not in tags:
            continue

        for attr_name, raw in attrs.items():
            value = const_value(objects, raw)

            if value is not None:
                out.append({
                    "tag": tag,
                    "attr": attr_name,
                    "value": value,
                    "path": node.get("path", []),
                    "xml_depth": node.get("depth"),
                    "raw": raw,
                })

    return out


def run_area_geometry(catalog_path, herodata_path, output_path, report_path):
    cat = load(catalog_path)
    hd = load(herodata_path)
    objects = cat.get("objects", {})

    reverse = build_reverse_index(objects)

    heroes = {}

    total = 0
    with_radius = 0
    with_width = 0
    with_actor = 0
    with_unit = 0
    with_reverse_actor = 0

    reverse_actor_types = Counter()
    rejected_reverse = Counter()
    accepted_reverse = Counter()

    review = []

    for hero, a in ability_records(hd):
        total += 1
        facts = []

        starts = ability_starts(objects, a)
        reached = traverse(objects, starts, max_depth=8)

        forward_ids = {n["id"] for n in reached}
        for start in starts:
            forward_ids.add(str(start.get("object_id")))

        saw_radius = False
        saw_width = False
        saw_actor = False
        saw_unit = False
        saw_reverse_actor = False

        # 1) Strong forward geometry.
        for n in reached:
            d = n["defn"]
            typ = str(d.get("xml_type") or "")
            oid = n["id"]

            if typ in {"CEffectEnumArea", "CEffectSearch"}:
                facts.append(source_fact(
                    "AREA_NODE",
                    typ,
                    {
                        "object_id": oid,
                        "xml_type": typ,
                        "graph_depth": n["depth"],
                        "chain": n["chain"],
                        "file": d.get("file"),
                    },
                    "HIGH",
                ))

            if typ == "CUnit":
                saw_unit = True

            for x in values_for_tag(
                objects,
                d,
                {"Radius", "Width", "Height", "Length", "Arc", "Range"},
            ):
                kind = x["tag"].upper()

                if typ in {"CEffectEnumArea", "CEffectSearch"} and kind == "RADIUS":
                    conf = "HIGH"
                elif typ.startswith("CAbil") and n["depth"] == 0:
                    conf = "HIGH"
                elif typ == "CUnit":
                    conf = "LOW"
                else:
                    conf = "MEDIUM"

                facts.append(source_fact(
                    kind,
                    x["value"],
                    {
                        "object_id": oid,
                        "xml_type": typ,
                        "graph_depth": n["depth"],
                        "raw": x["raw"],
                        "path": x["path"],
                        "file": d.get("file"),
                    },
                    conf,
                ))

                if kind == "RADIUS":
                    saw_radius = True
                if kind == "WIDTH":
                    saw_width = True

        # 2) Reverse geometry: filtered aggressively.
        reverse_candidates = {}

        for target_id in forward_ids:
            if target_id in REJECT_RAW_VALUES:
                continue

            for ref in reverse.get(target_id, []):
                typ = str(ref.get("xml_type") or "")

                if not is_semantic_reverse_ref(ref):
                    rejected_reverse["NON_SEMANTIC_REF"] += 1
                    continue

                allowed = (
                    actor_type_allowed(typ)
                    or typ.startswith("CMover")
                    or typ.startswith("CShape")
                )

                if not allowed:
                    rejected_reverse[f"TYPE:{typ}"] += 1
                    continue

                key = (
                    ref["source_id"],
                    typ,
                    ref.get("file"),
                    ref.get("tag"),
                    ref.get("attr"),
                    target_id,
                )

                reverse_candidates[key] = {
                    **ref,
                    "target_id": target_id,
                }

        for key, ref in reverse_candidates.items():
            source_id, typ, file, _, _, target_id = key

            for d in objects.get(source_id, []):
                if str(d.get("xml_type") or "") != typ:
                    continue

                accepted_reverse[typ] += 1

                if actor_type_allowed(typ):
                    saw_actor = True
                    saw_reverse_actor = True
                    reverse_actor_types[typ] += 1

                    facts.append(source_fact(
                        "ACTOR_GEOMETRY",
                        typ,
                        {
                            "object_id": source_id,
                            "xml_type": typ,
                            "reverse_target_id": target_id,
                            "reverse_ref_tag": ref.get("tag"),
                            "reverse_ref_attr": ref.get("attr"),
                            "reverse_ref_raw": ref.get("raw"),
                            "file": file,
                        },
                        "MEDIUM",
                    ))

                if typ.startswith("CMover"):
                    facts.append(source_fact(
                        "REVERSE_MOVER",
                        typ,
                        {
                            "object_id": source_id,
                            "xml_type": typ,
                            "reverse_target_id": target_id,
                            "reverse_ref_tag": ref.get("tag"),
                            "reverse_ref_attr": ref.get("attr"),
                            "reverse_ref_raw": ref.get("raw"),
                            "file": file,
                        },
                        "LOW",
                    ))

                for x in geometry_values(objects, d):
                    tag = x["tag"].upper()

                    if tag in {"WIDTH", "HALFWIDTH"}:
                        kind = "WIDTH"
                        saw_width = True
                    elif tag in {"HEIGHT", "HALFHEIGHT"}:
                        kind = "HEIGHT"
                    elif tag == "RADIUS":
                        kind = "RADIUS"
                        saw_radius = True
                    elif tag == "LENGTH":
                        kind = "LENGTH"
                    elif tag == "ARC":
                        kind = "ARC"
                    elif tag == "RANGE":
                        kind = "RANGE"
                    elif tag == "SCALE":
                        kind = "ACTOR_SCALE"
                    elif tag == "SIZE":
                        kind = "ACTOR_SIZE"
                    elif tag == "QUAD":
                        kind = "ACTOR_QUAD_VALUE"
                    else:
                        continue

                    # Reverse geometry is never HIGH by itself.
                    conf = "MEDIUM" if actor_type_allowed(typ) else "LOW"

                    facts.append(source_fact(
                        kind,
                        x["value"],
                        {
                            "object_id": source_id,
                            "xml_type": typ,
                            "reverse_target_id": target_id,
                            "reverse_ref_tag": ref.get("tag"),
                            "reverse_ref_attr": ref.get("attr"),
                            "reverse_ref_raw": ref.get("raw"),
                            "raw": x["raw"],
                            "path": x["path"],
                            "file": file,
                        },
                        conf,
                    ))

        if saw_radius:
            with_radius += 1
        if saw_width:
            with_width += 1
        if saw_actor:
            with_actor += 1
        if saw_unit:
            with_unit += 1
        if saw_reverse_actor:
            with_reverse_actor += 1

        if not facts:
            review.append({
                "hero": hero,
                "record_key": a.get("record_key"),
                "ability_id": a.get("id"),
                "reason": "NO_AREA_GEOMETRY_FACT",
            })

        heroes.setdefault(hero, []).append({
            "record_key": a.get("record_key"),
            "ability_id": a.get("id"),
            "name": a.get("name"),
            "facts": facts,
        })

    data = {
        "metadata": {
            "builder": "area_geometry_parser_v3_2_semantic_reverse",
            "reverse_reference_index": True,
            "reverse_policy": "semantic_whitelist",
        },
        "heroes": heroes,
    }

    report = {
        "summary": {
            "abilities_total": total,
            "with_radius": with_radius,
            "with_width": with_width,
            "with_actor_geometry": with_actor,
            "with_unit_geometry": with_unit,
            "with_reverse_actor_geometry": with_reverse_actor,
        },
        "reverse_actor_types": reverse_actor_types.most_common(50),
        "accepted_reverse_types": accepted_reverse.most_common(50),
        "rejected_reverse_reasons": rejected_reverse.most_common(100),
        "review": review,
    }

    save(output_path, data)
    save(report_path, report)
    return report
