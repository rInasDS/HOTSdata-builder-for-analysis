from static_geometry_probe import PNP_BIT_TRIANGLES


def triangle_area(vertices):
    (x1,y1),(x2,y2),(x3,y3)=vertices
    return abs(x1*(y2-y3)+x2*(y3-y1)+x3*(y1-y2))/2


def test_equal_subcell_area():
    areas=[triangle_area(v['vertices']) for v in PNP_BIT_TRIANGLES.values()]
    assert all(abs(a-0.125)<1e-12 for a in areas)
    assert abs(sum(areas)-1.0)<1e-12

if __name__ == '__main__':
    test_equal_subcell_area(); print('PASS')
