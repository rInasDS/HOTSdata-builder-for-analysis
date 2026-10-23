from static_geometry_probe import mapinfo_label, parse_position, parse_scale


def test_mapinfo_label():
    assert mapinfo_label(b"xxx\x00TowersOfDoom @\x00yyy") == "TowersOfDoom"


def test_vectors():
    assert parse_position("1.5,2.5,7") == {"x": 1.5, "y": 2.5, "z": 7.0}
    assert parse_scale("1,0.5,2") == {"x": 1.0, "y": 0.5, "z": 2.0}


if __name__ == "__main__":
    test_mapinfo_label()
    test_vectors()
    print("PASS")
