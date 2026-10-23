from __future__ import annotations

import json
from json_backend import save as _orjson_save
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class CatalogDefinition:
    object_id: str
    xml_type: str
    file: str
    mod: str | None
    attrs: dict[str, Any]
    children: list[dict[str, Any]]
    descendants: list[dict[str, Any]]
    order: int


def _attrs(attrs) -> dict[str, str]:
    return {str(k): str(v)[:2000] for k, v in attrs.items()}


def _flatten_descendants(elem: ET.Element) -> list[dict]:
    out = []

    def visit(node: ET.Element, path: list[str], depth: int) -> None:
        for child in list(node):
            child_path = path + [str(child.tag)]
            out.append({
                "tag": str(child.tag),
                "attrs": _attrs(child.attrib),
                "path": child_path,
                "depth": depth,
            })
            visit(child, child_path, depth + 1)

    visit(elem, [], 1)
    return out


class StormCatalogIndex:
    """
    Global XML catalog.

    v2.2:
    - all definitions for duplicate ids are preserved;
    - immediate children are preserved;
    - ALL nested descendants are also flattened with path/depth.
      This is required for structures such as:
        Cost -> Vital
        Cost -> Cooldown
        AreaArray -> Radius
        Charge -> CountMax
    """

    def __init__(self, mods_root: Path):
        self.mods_root = Path(mods_root).resolve()
        self.definitions_by_id: dict[str, list[CatalogDefinition]] = defaultdict(list)
        self.files_scanned = 0
        self.objects_indexed = 0
        self.parse_errors: list[dict] = []
        self.type_counts: Counter[str] = Counter()
        self.mod_counts: Counter[str] = Counter()
        self.root_tag_counts: Counter[str] = Counter()
        self.non_catalog_files_skipped = 0

    @staticmethod
    def _detect_mod(path: Path) -> str | None:
        for part in path.parts:
            if part.lower().endswith(".stormmod"):
                return part
        return None

    def build(self) -> None:
        if not self.mods_root.exists():
            raise FileNotFoundError(f"raw_xml mods root not found: {self.mods_root}")

        order = 0

        for path in sorted(self.mods_root.rglob("*.xml")):
            self.files_scanned += 1
            try:
                root = ET.parse(path).getroot()
                self.root_tag_counts[str(root.tag)] += 1
                mod = self._detect_mod(path)
                if mod:
                    self.mod_counts[mod] += 1

                # Only direct children of <Catalog> are catalog definitions.
                # Older builds used root.iter(), which incorrectly indexed nested
                # structures (e.g. RankArray/Talent nodes) and <Preload> references
                # as if they were standalone GameData records.
                if str(root.tag) != "Catalog":
                    self.non_catalog_files_skipped += 1
                    continue

                for elem in list(root):
                    oid = elem.attrib.get("id")
                    if not oid:
                        continue

                    order += 1
                    children = [{
                        "tag": str(c.tag),
                        "attrs": _attrs(c.attrib),
                    } for c in list(elem)]

                    definition = CatalogDefinition(
                        object_id=str(oid),
                        xml_type=str(elem.tag),
                        file=str(path),
                        mod=mod,
                        attrs=_attrs(elem.attrib),
                        children=children,
                        descendants=_flatten_descendants(elem),
                        order=order,
                    )
                    self.definitions_by_id[str(oid)].append(definition)
                    self.objects_indexed += 1
                    self.type_counts[str(elem.tag)] += 1

            except Exception as e:
                self.parse_errors.append({"file": str(path), "error": repr(e)})

    def to_index_json(self) -> dict:
        return {
            "metadata": {
                "builder": "storm_catalog_index_v2_3",
                "mods_root": str(self.mods_root),
                "files_scanned": self.files_scanned,
                "objects_indexed": self.objects_indexed,
                "unique_object_ids": len(self.definitions_by_id),
                "parse_errors": len(self.parse_errors),
                "nested_descendants_retained": True,
                "catalog_direct_children_only": True,
                "non_catalog_files_skipped": self.non_catalog_files_skipped,
            },
            "objects": {
                oid: [asdict(x) for x in defs]
                for oid, defs in sorted(self.definitions_by_id.items())
            },
        }

    def to_report_json(self) -> dict:
        duplicate = {k:v for k,v in self.definitions_by_id.items() if len(v)>1}
        cross = []
        for oid, defs in duplicate.items():
            mods = sorted({d.mod for d in defs if d.mod})
            if len(mods)>1:
                cross.append({
                    "object_id":oid,
                    "definition_count":len(defs),
                    "mods":mods,
                    "types":sorted({d.xml_type for d in defs}),
                    "files":[d.file for d in defs],
                })

        return {
            "metadata":{"builder":"storm_catalog_index_v2_3","mods_root":str(self.mods_root)},
            "summary":{
                "files_scanned":self.files_scanned,
                "objects_indexed":self.objects_indexed,
                "unique_object_ids":len(self.definitions_by_id),
                "duplicate_object_ids":len(duplicate),
                "cross_mod_duplicate_ids":len(cross),
                "parse_errors":len(self.parse_errors),
                "non_catalog_files_skipped":self.non_catalog_files_skipped,
            },
            "root_tag_counts":self.root_tag_counts.most_common(),
            "top_xml_types":self.type_counts.most_common(100),
            "top_stormmods_by_xml_file_count":self.mod_counts.most_common(100),
            "cross_mod_duplicate_examples":cross[:500],
            "parse_errors":self.parse_errors[:500],
        }


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    _orjson_save(path,data)


def build_catalog(mods_root: Path,index_output: Path,report_output: Path) -> dict:
    idx=StormCatalogIndex(mods_root);idx.build()
    save_json(index_output,idx.to_index_json())
    r=idx.to_report_json();save_json(report_output,r);return r
