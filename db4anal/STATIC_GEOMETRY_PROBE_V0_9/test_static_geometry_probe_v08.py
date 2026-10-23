from static_geometry_probe import angle_diff_deg


def test_angle_wrap():
    assert angle_diff_deg(179.0, -179.0) == 2.0
    assert angle_diff_deg(45.5, 45.0) == 0.5

if __name__ == '__main__':
    test_angle_wrap(); print('PASS')
