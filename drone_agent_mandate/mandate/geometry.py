"""Géométrie plane pour la géofence polygonale, écrite à la main (pas de shapely)."""

import math

from ..geo import EARTH_RADIUS_M

Point = tuple[float, float]  # (x, y) en mètres dans le repère local
EPS = 1e-9


def to_local(lat: float, lon: float, ref: tuple[float, float]) -> Point:
    """(lat, lon) en degrés -> (x est, y nord) en mètres par rapport à `ref`."""
    x = math.radians(lon - ref[1]) * EARTH_RADIUS_M * math.cos(math.radians(ref[0]))
    y = math.radians(lat - ref[0]) * EARTH_RADIUS_M
    return x, y


def _cross(o: Point, a: Point, b: Point) -> float:
    """Produit vectoriel (a - o) x (b - o) : > 0 si o -> a -> b tourne à gauche."""
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _on_segment(p: Point, a: Point, b: Point, tol: float = 1e-6) -> bool:
    """p est-il sur le segment [a, b] (à `tol` mètre près) ?"""
    if abs(_cross(a, b, p)) > tol * max(1.0, math.dist(a, b)):
        return False
    return (min(a[0], b[0]) - tol <= p[0] <= max(a[0], b[0]) + tol
            and min(a[1], b[1]) - tol <= p[1] <= max(a[1], b[1]) + tol)


def _edges(poly: list[Point]):
    return [(poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))]


def point_in_polygon(p: Point, poly: list[Point]) -> bool:
    """Lancer de rayon : on compte combien de bords coupe une demi-droite partant de p."""
    if any(_on_segment(p, a, b) for a, b in _edges(poly)):
        return True
    inside = False
    x, y = p
    for (x1, y1), (x2, y2) in _edges(poly):
        if (y1 > y) != (y2 > y):  # le bord enjambe l'horizontale passant par p
            x_cross = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x_cross > x:
                inside = not inside
    return inside


def segments_cross(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    """Les segments se coupent-ils strictement (en un point intérieur aux deux) ?"""
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return d1 * d2 < -EPS and d3 * d4 < -EPS


def segment_in_polygon(a: Point, b: Point, poly: list[Point], step_m: float = 1.0) -> bool:
    """Le trajet en ligne droite de a à b reste-t-il dans le polygone ?"""
    if not (point_in_polygon(a, poly) and point_in_polygon(b, poly)):
        return False
    if any(segments_cross(a, b, e1, e2) for e1, e2 in _edges(poly)):
        return False
    n = max(1, int(math.dist(a, b) / step_m))
    return all(point_in_polygon((a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n), poly)
               for i in range(1, n))


def polygon_is_simple(poly: list[Point]) -> bool:
    """Au moins 3 sommets, aire non nulle, et aucun bord ne coupe un bord non adjacent."""
    if len(poly) < 3:
        return False
    area2 = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in _edges(poly))
    if abs(area2) < EPS:
        return False
    edges = _edges(poly)
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            adjacent = j == i + 1 or (i == 0 and j == len(edges) - 1)
            if not adjacent and segments_cross(*edges[i], *edges[j]):
                return False
    return True


def polygon_in_polygon(inner: list[Point], outer: list[Point]) -> bool:
    """`inner` est-il entièrement contenu dans `outer` ? (sert au sous-mandat)"""
    return all(segment_in_polygon(a, b, outer) for a, b in _edges(inner))
