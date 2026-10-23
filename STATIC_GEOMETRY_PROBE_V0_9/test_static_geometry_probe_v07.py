import struct, tempfile
from pathlib import Path
from static_geometry_probe import parse_heightmap, parse_sync_cliff


def test_heightmap_and_cliff_formats():
    frame = {
        'vertex_dim': {'x': 2, 'y': 2}, 'cell_dim': {'x': 1, 'y': 1},
        'offset': {'x': 0.0, 'y': 0.0, 'z': 0.0},
        'scale': {'x': 1.0, 'y': 1.0, 'z': 1.0},
    }
    tx = {'vert_data': {'quantizeBias': '0', 'quantizeScale': '1', 'standardHeight': '8'}}
    header = struct.pack('<4I', int.from_bytes(b'HMAP','little'), 101, 2, 2) + bytes(16)
    cells = b''.join(struct.pack('<HHH', v, 0, 1) for v in [1,2,3,4])
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td)/'h.f32le'
        meta = parse_heightmap(header+cells, tx, frame, hp)
        assert meta['status'].startswith('PROVEN')
        assert hp.stat().st_size == 4*4
        cp = Path(td)/'c.u16le'
        chead = struct.pack('<8I', int.from_bytes(b'CLIF','little'),100,1,1,0,0,0,0)
        cmeta, vals = parse_sync_cliff(chead+struct.pack('<H',64), frame, cp)
        assert cmeta['status'].startswith('PROVEN') and vals == [64]
        assert cmeta['rounded_quantized_without_runtime_base_floor_histogram'] == {'4':1}

if __name__ == '__main__':
    test_heightmap_and_cliff_formats(); print('PASS')
