from pathlib import Path
import json
import xml.etree.ElementTree as ET

from pyhots_version_builder import normalize_full_version, parse_definition, mod_priority, verify_baseline


def test_version_normalization():
    assert normalize_full_version("v2.55.17.97771") == "2.55.17.97771"
    assert normalize_full_version("2.55.17.97771") == "2.55.17.97771"
    assert normalize_full_version("97771") is None


def test_mod_priority():
    assert mod_priority("core.stormmod") < mod_priority("heroesdata.stormmod")
    assert mod_priority("heroesdata.stormmod") < mod_priority("zarya.stormmod")


def test_veterancy_ancestor_context_retained(tmp_path: Path):
    root = ET.fromstring('''<CBehaviorVeterancy id="X"><VeterancyLevelArray MinVeterancyXP="1"><Modification><VitalMaxArray index="Energy" value="10"/></Modification></VeterancyLevelArray></CBehaviorVeterancy>''')
    d = parse_definition(root, file=tmp_path / "x.xml", mod="heroesdata.stormmod", order=7)
    assert d is not None
    nodes = d["descendants"]
    v = next(x for x in nodes if x["tag"] == "VitalMaxArray")
    assert v["path"] == ["VeterancyLevelArray", "Modification", "VitalMaxArray"]
    assert [x["tag"] for x in v["ancestor_context"]] == ["VeterancyLevelArray", "Modification", "VitalMaxArray"]
    assert v["ancestor_context"][-1]["index"] == "Energy"


def test_97771_baseline_gate():
    db = {"summary": {
        "heroes": 90, "forms": 104, "unique_owned_entities": 129,
        "chero_level_scaling_records": 1641, "typed_level_scaling_records": 1641,
        "typed_execution_unresolved": 0, "overall": "PASS",
    }}
    assert verify_baseline("2.55.17.97771", db)["status"] == "PASS"
    db["summary"]["heroes"] = 91
    assert verify_baseline("2.55.17.97771", db)["status"] == "FAIL"


def test_safe_extract_flattens_github_root(tmp_path: Path):
    import zipfile
    from pyhots_version_builder import safe_extract_zip

    archive = tmp_path / "src.zip"
    deep = "HeroesOfTheStorm_Gamedata-2.55.17.97771/" + "/".join(["verylongcomponent"] * 12) + "/x.xml"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("HeroesOfTheStorm_Gamedata-2.55.17.97771/.github/workflows/test.yml", "name: test\n")
        z.writestr(deep, "<Catalog/>\n")

    dest = tmp_path / "out"
    root = safe_extract_zip(archive, dest)
    assert root == dest
    assert (dest / ".github" / "workflows" / "test.yml").is_file()
    assert (dest.joinpath(*(["verylongcomponent"] * 12), "x.xml")).is_file()
    assert not (dest / "HeroesOfTheStorm_Gamedata-2.55.17.97771").exists()
    # Cache marker path should make a second call a no-op/cache hit.
    assert safe_extract_zip(archive, dest) == dest
