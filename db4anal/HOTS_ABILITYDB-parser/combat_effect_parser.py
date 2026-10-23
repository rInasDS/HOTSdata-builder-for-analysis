from __future__ import annotations
from combat_typed_engine import run_combat as _run

def run_combat_effects(replay_catalog_path, typed_graph_path, herodata_path, output_path, report_path):
    return _run(replay_catalog_path, typed_graph_path, herodata_path, output_path, report_path)
