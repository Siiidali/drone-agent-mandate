from drone_agent_mandate.mandate.geometry import (point_in_polygon, polygon_in_polygon, polygon_is_simple,
                                                  segment_in_polygon, segments_cross)

SQUARE = [(0, 0), (10, 0), (10, 10), (0, 10)]
# Zone en L (non convexe) : le carré 10x10 privé de son quart haut-droit.
L_SHAPE = [(0, 0), (10, 0), (10, 5), (5, 5), (5, 10), (0, 10)]


def test_point_in_polygon():
    assert point_in_polygon((5, 5), SQUARE)
    assert not point_in_polygon((11, 5), SQUARE)
    assert point_in_polygon((0, 5), SQUARE)       # sur le bord : dedans
    assert point_in_polygon((10, 10), SQUARE)     # sur un sommet : dedans
    assert not point_in_polygon((8, 8), L_SHAPE)  # dans l'encoche du L


def test_segment_crossing_concave_notch_is_rejected():
    # Les deux extrémités sont dans le L, mais la ligne droite passe par l'encoche.
    a, b = (9, 2), (2, 9)
    assert point_in_polygon(a, L_SHAPE) and point_in_polygon(b, L_SHAPE)
    assert not segment_in_polygon(a, b, L_SHAPE)
    assert segment_in_polygon((1, 1), (9, 1), L_SHAPE)


def test_segment_through_reflex_vertex():
    assert not segment_in_polygon((4, 4), (8, 8), L_SHAPE)


def test_segments_cross():
    assert segments_cross((0, 0), (2, 2), (0, 2), (2, 0))
    assert not segments_cross((0, 0), (1, 1), (2, 2), (3, 3))
    assert not segments_cross((0, 0), (2, 0), (2, 0), (2, 2))  # contact à l'extrémité : pas strict


def test_polygon_is_simple():
    assert polygon_is_simple(SQUARE) and polygon_is_simple(L_SHAPE)
    assert not polygon_is_simple([(0, 0), (10, 10), (10, 0), (0, 10)])  # nœud papillon
    assert not polygon_is_simple([(0, 0), (1, 1)])
    assert not polygon_is_simple([(0, 0), (1, 1), (2, 2)])               # aire nulle


def test_polygon_in_polygon():
    assert polygon_in_polygon([(1, 1), (4, 1), (4, 4), (1, 4)], L_SHAPE)
    assert not polygon_in_polygon([(1, 1), (9, 1), (9, 9), (1, 9)], L_SHAPE)  # couvre l'encoche
    assert not polygon_in_polygon([(1, 1), (12, 1), (12, 4)], SQUARE)
