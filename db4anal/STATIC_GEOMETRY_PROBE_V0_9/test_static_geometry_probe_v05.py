import struct
from static_geometry_probe import parse_cell_attribute


def test_cell_attribute_format():
    # 2x1 terrain cells -> 16 bits -> 2 payload bytes.
    raw = struct.pack('<I', 16) + bytes([0x00, 0xFF])
    r = parse_cell_attribute(raw, 2, 1)
    assert r['status'] == 'PROVEN_CELLATTRIBUTE_BITFIELD'
    assert r['bits_per_terrain_cell'] == 8
    assert r['zero_cells'] == 1
    assert r['full_cells'] == 1


def test_dimension_mismatch_fails_closed():
    raw = struct.pack('<I', 8) + bytes([0x00])
    r = parse_cell_attribute(raw, 2, 1)
    assert r['status'] == 'UNRESOLVED_DIMENSION_MISMATCH'


if __name__ == '__main__':
    test_cell_attribute_format()
    test_dimension_mismatch_fails_closed()
    print('PASS')
