from __future__ import annotations

from fact_utils import load, save


def run_semantic_bundle(spatial_path, combat_path, output_path):
    """Package already-derived semantic layers for merge.

    v1.16 intentionally has no manual ability override table.  The envelope keeps
    the merge contract stable while making manual patching impossible in the
    active reproducible pipeline.
    """
    spatial = load(spatial_path)
    combat = load(combat_path)
    out = {
        "metadata": {
            "builder": "semantic_bundle_v1_16",
            "manual_overrides": 0,
        },
        "applied": [],
        "missing": [],
        "spatial": spatial,
        "combat": combat,
    }
    save(output_path, out)
    return out
