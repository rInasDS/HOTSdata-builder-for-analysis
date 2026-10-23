from pathlib import Path


def test_manual_override_layer_removed_v116():
    root = Path(__file__).parent
    assert not (root / "ability_overrides.json").exists()
    build = (root / "build_abilities_database.py").read_text()
    assert "run_overrides" not in build
    assert "ability_overrides.json" not in build
