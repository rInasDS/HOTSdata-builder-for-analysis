from static_geometry_probe import (
    absolute_shrub_geometry,
    doodad_footprint_enabled,
    footprint_place_area,
    parse_object_doodads,
    parse_terrain_frame,
)


def test_no_doodad_footprint_flag():
    raw = b'''<Objects><ObjectDoodad Id="1" Type="Curtain" Position="74.25,51.75,8"><Flag Index="NoDoodadFootprint" Value="1"/></ObjectDoodad></Objects>'''
    items = parse_object_doodads(raw)
    assert len(items) == 1
    assert not doodad_footprint_enabled(items[0])


def test_terrain_frame():
    raw = b'''<terrain><heightMap dim="257 217 " offset="0 0 0 " scale="1 1 1 "/></terrain>'''
    frame = parse_terrain_frame(raw)
    assert frame["status"] == "PROVEN_FROM_T3TERRAIN"
    assert frame["cell_dim"] == {"x": 256, "y": 216}


def test_stock_footprint_areas_and_cells():
    rec1 = {"children": [{"tag": "Layers", "attrs": {"index": "Place", "Area": "0,0,1,1"}}]}
    area1 = footprint_place_area(rec1, 1, 1)
    assert (area1["min_x"], area1["max_x"]) == (0, 1)

    area2 = footprint_place_area({"children": []}, 2, 2)
    assert area2 == {
        "min_x": -1,
        "min_y": -1,
        "max_x": 1,
        "max_y": 1,
        "source": "CENTERED_FROM_ROW_DIMENSIONS_STOCK_SHRUB",
    }

    terrain = {
        "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
        "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
    }
    geom1 = absolute_shrub_geometry(
        {"x": 72.5, "y": 154.5, "z": 8.0},
        {"place_area": area1},
        terrain,
    )
    assert [(c["x"], c["y"]) for c in geom1["cells"]] == [(72, 154)]

    geom2 = absolute_shrub_geometry(
        {"x": 100.0, "y": 80.0, "z": 8.0},
        {"place_area": area2},
        terrain,
    )
    assert {(c["x"], c["y"]) for c in geom2["cells"]} == {
        (99, 79), (100, 79), (99, 80), (100, 80)
    }


def test_quarter_anchor_rule():
    terrain = {
        "offset": {"x": 0.0, "y": 0.0, "z": 0.0},
        "scale": {"x": 1.0, "y": 1.0, "z": 1.0},
    }
    area = {"min_x": 0, "min_y": 0, "max_x": 1, "max_y": 1, "source": "test"}
    geom = absolute_shrub_geometry(
        {"x": 82.9804, "y": 87.9907, "z": 8.0},
        {"place_area": area},
        terrain,
    )
    assert geom["anchor_cell"] == {"x": 83, "y": 88}


if __name__ == "__main__":
    test_no_doodad_footprint_flag()
    test_terrain_frame()
    test_stock_footprint_areas_and_cells()
    test_quarter_anchor_rule()
    print("PASS")
