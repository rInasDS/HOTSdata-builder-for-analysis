from static_geometry_probe import PNP_BIT_TRIANGLES, CANONICAL_PNP_MASKS


def test_pnp_triangle_layout():
    assert set(PNP_BIT_TRIANGLES) == set(range(8))
    assert CANONICAL_PNP_MASKS == (0x0F, 0x2B, 0x33, 0x71, 0x8E, 0xCC, 0xD4, 0xF0)
    # Each triangle is represented by exactly 3 local vertices and stays inside one terrain cell.
    for rec in PNP_BIT_TRIANGLES.values():
        assert len(rec['vertices']) == 3
        assert all(0.0 <= x <= 1.0 and 0.0 <= y <= 1.0 for x, y in rec['vertices'])

if __name__ == '__main__':
    test_pnp_triangle_layout(); print('PASS')
