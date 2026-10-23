from static_geometry_probe import connected_components, point_in_rect, rect_from_center


def test_rectangles_and_join():
    r = rect_from_center(10.5, 20.5, 3.0, 3.0)
    assert r == {"min_x": 9.0, "min_y": 19.0, "max_x": 12.0, "max_y": 22.0}
    assert point_in_rect(10.5, 20.5, r)
    assert not point_in_rect(13.0, 20.5, r)


def test_components_touching_edges():
    items = [
        {"rectangle": rect_from_center(0.0, 0.0, 3.0, 3.0)},
        {"rectangle": rect_from_center(3.0, 0.0, 3.0, 3.0)},
        {"rectangle": rect_from_center(20.0, 0.0, 3.0, 3.0)},
    ]
    comps = connected_components(items)
    assert comps == [[0, 1], [2]]


if __name__ == "__main__":
    test_rectangles_and_join()
    test_components_touching_edges()
    print("PASS")
