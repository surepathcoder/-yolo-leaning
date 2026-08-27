"""Polygon and line geometry.

Pure functions on normalised 0..1 coordinates. No I/O, no state, no
dependencies beyond NumPy — so every behaviour here is exhaustively testable,
which matters because a subtle error in point-in-polygon becomes a security
guard being sent to the wrong aisle.
"""

from __future__ import annotations

import math

import numpy as np

Point = tuple[float, float]
Polygon = list[Point] | list[list[float]]


class GeometryError(ValueError):
    """A polygon or line is not usable."""


def normalise_polygon(polygon: Polygon) -> list[Point]:
    """Validate and canonicalise polygon vertices.

    Raises :class:`GeometryError` rather than silently accepting a degenerate
    shape: a zone that is actually a line would match nothing, and the
    installer would see "no events" with no idea why.
    """
    if polygon is None:
        raise GeometryError("Polygon is missing.")
    points: list[Point] = []
    for vertex in polygon:
        if len(vertex) != 2:
            raise GeometryError("Each polygon vertex must have exactly two coordinates.")
        x, y = float(vertex[0]), float(vertex[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise GeometryError("Polygon coordinates must be finite numbers.")
        points.append((x, y))

    if len(points) < 3:
        raise GeometryError("A polygon needs at least three points.")
    # A closing point that repeats the first is common in drawing tools.
    if len(points) > 3 and points[0] == points[-1]:
        points = points[:-1]
    if len(points) < 3:
        raise GeometryError("A polygon needs at least three distinct points.")
    if polygon_area(points) < 1e-9:
        raise GeometryError("Polygon has effectively zero area.")
    return points


def polygon_area(polygon: list[Point]) -> float:
    """Absolute area via the shoelace formula."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    for i in range(len(polygon)):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % len(polygon)]
        total += x1 * y2 - x2 * y1
    return abs(total) / 2.0


def polygon_centroid(polygon: list[Point]) -> Point:
    """Area centroid. Falls back to the vertex mean for degenerate input."""
    area = 0.0
    cx = cy = 0.0
    for i in range(len(polygon)):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % len(polygon)]
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    area /= 2.0
    if abs(area) < 1e-12:
        xs = [p[0] for p in polygon]
        ys = [p[1] for p in polygon]
        return (sum(xs) / len(xs), sum(ys) / len(ys))
    return (cx / (6 * area), cy / (6 * area))


def point_in_polygon(point: Point, polygon: list[Point]) -> bool:
    """Ray-casting point-in-polygon test.

    Handles concave polygons correctly, which matters: an installer drawing
    around a shelf unit or a counter will produce concave shapes routinely, and
    a convex-only test would report people inside zones they are not in.

    Points exactly on an edge are treated as inside, so a zone boundary is
    inclusive and a person standing precisely on the line is not flickering
    between states.
    """
    x, y = point
    if point_on_polygon_boundary(point, polygon):
        return True

    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the horizontal ray from (x, y) cross edge j->i?
        if (yi > y) != (yj > y):
            x_intersect = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_intersect:
                inside = not inside
        j = i
    return inside


def point_on_polygon_boundary(point: Point, polygon: list[Point], tolerance: float = 1e-9) -> bool:
    """Whether a point lies on any edge of the polygon."""
    x, y = point
    n = len(polygon)
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if _point_on_segment(x, y, x1, y1, x2, y2, tolerance):
            return True
    return False


def _point_on_segment(
    px: float, py: float, x1: float, y1: float, x2: float, y2: float, tolerance: float
) -> bool:
    cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
    if abs(cross) > tolerance:
        return False
    # Collinear: check it lies between the endpoints.
    return (
        min(x1, x2) - tolerance <= px <= max(x1, x2) + tolerance
        and min(y1, y2) - tolerance <= py <= max(y1, y2) + tolerance
    )


def points_in_polygon(points: np.ndarray, polygon: list[Point]) -> np.ndarray:
    """Vectorised point-in-polygon for an ``(N, 2)`` array.

    Used for shelf occupancy, where thousands of sample points are tested
    against one polygon on every sample and a Python loop would dominate the
    cost.
    """
    points = np.asarray(points, dtype=np.float64)
    if points.size == 0:
        return np.zeros(0, dtype=bool)
    polygon_array = np.asarray(polygon, dtype=np.float64)

    x = points[:, 0]
    y = points[:, 1]
    inside = np.zeros(len(points), dtype=bool)

    n = len(polygon_array)
    j = n - 1
    for i in range(n):
        xi, yi = polygon_array[i]
        xj, yj = polygon_array[j]
        straddles = (yi > y) != (yj > y)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_intersect = np.where(yj != yi, (xj - xi) * (y - yi) / (yj - yi) + xi, xi)
        inside ^= straddles & (x < x_intersect)
        j = i
    return inside


def polygon_bounds(polygon: list[Point]) -> tuple[float, float, float, float]:
    """Axis-aligned bounding box ``(x1, y1, x2, y2)``."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return (min(xs), min(ys), max(xs), max(ys))


# ---------------------------------------------------------------------------
# Lines
# ---------------------------------------------------------------------------


def normalise_line(
    point_a: Point | list[float], point_b: Point | list[float]
) -> tuple[Point, Point]:
    """Validate a two-point line."""
    if point_a is None or point_b is None or len(point_a) != 2 or len(point_b) != 2:
        raise GeometryError("A line needs two points of two coordinates each.")
    ax, ay = float(point_a[0]), float(point_a[1])
    bx, by = float(point_b[0]), float(point_b[1])
    if not all(math.isfinite(v) for v in (ax, ay, bx, by)):
        raise GeometryError("Line coordinates must be finite numbers.")
    if math.hypot(bx - ax, by - ay) < 1e-9:
        raise GeometryError("Line endpoints must be distinct.")
    return (ax, ay), (bx, by)


def side_of_line(point: Point, line_a: Point, line_b: Point) -> float:
    """Signed cross product: which side of the line the point is on.

    Positive and negative are the two sides; near-zero means on the line. The
    *sign* is what direction-aware counting uses -- a person crossing from
    negative to positive is going one way, and the reverse the other.
    """
    return (line_b[0] - line_a[0]) * (point[1] - line_a[1]) - (line_b[1] - line_a[1]) * (
        point[0] - line_a[0]
    )


def segments_intersect(p1: Point, p2: Point, p3: Point, p4: Point) -> bool:
    """Whether segment ``p1-p2`` intersects segment ``p3-p4``.

    Both segments are bounded, which is the point: a person's path segment
    only counts as a crossing if it passes through the drawn line, not through
    the infinite line it lies on. Without this, someone walking well past the
    end of a doorway line would be counted as entering.
    """
    d1 = side_of_line(p3, p1, p2)
    d2 = side_of_line(p4, p1, p2)
    d3 = side_of_line(p1, p3, p4)
    d4 = side_of_line(p2, p3, p4)

    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return True

    # Collinear touching cases.
    tolerance = 1e-12
    if abs(d1) < tolerance and _point_on_segment(p3[0], p3[1], p1[0], p1[1], p2[0], p2[1], 1e-9):
        return True
    if abs(d2) < tolerance and _point_on_segment(p4[0], p4[1], p1[0], p1[1], p2[0], p2[1], 1e-9):
        return True
    if abs(d3) < tolerance and _point_on_segment(p1[0], p1[1], p3[0], p3[1], p4[0], p4[1], 1e-9):
        return True
    return bool(
        abs(d4) < tolerance and _point_on_segment(p2[0], p2[1], p3[0], p3[1], p4[0], p4[1], 1e-9)
    )


def line_length(point_a: Point, point_b: Point) -> float:
    return math.hypot(point_b[0] - point_a[0], point_b[1] - point_a[1])


def distance_point_to_segment(point: Point, seg_a: Point, seg_b: Point) -> float:
    """Shortest distance from a point to a bounded segment."""
    px, py = point
    ax, ay = seg_a
    bx, by = seg_b
    dx, dy = bx - ax, by - ay
    length_squared = dx * dx + dy * dy
    if length_squared < 1e-18:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length_squared))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def bbox_polygon_overlap_ratio(
    bbox: tuple[float, float, float, float], polygon: list[Point], samples: int = 24
) -> float:
    """Approximate the fraction of a box covered by a polygon.

    Monte-Carlo-free: a regular ``samples x samples`` grid is used so the
    result is deterministic. Determinism matters more than exactness here --
    shelf occupancy is compared against thresholds over time, and a value that
    jitters by a few percent between identical frames would produce phantom
    stock events.
    """
    x1, y1, x2, y2 = bbox
    if x2 <= x1 or y2 <= y1:
        return 0.0
    xs = np.linspace(x1, x2, samples)
    ys = np.linspace(y1, y2, samples)
    grid_x, grid_y = np.meshgrid(xs, ys)
    points = np.column_stack([grid_x.ravel(), grid_y.ravel()])
    return float(points_in_polygon(points, polygon).mean())
