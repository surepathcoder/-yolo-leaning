"""Zones and geometry."""

from vision.zones.engine import (
    ZoneDefinition,
    ZoneEngine,
    ZoneEventKind,
    ZoneKind,
    ZoneObservation,
    ZoneOccupancy,
)
from vision.zones.geometry import (
    GeometryError,
    bbox_polygon_overlap_ratio,
    distance_point_to_segment,
    normalise_line,
    normalise_polygon,
    point_in_polygon,
    points_in_polygon,
    polygon_area,
    polygon_bounds,
    polygon_centroid,
    segments_intersect,
    side_of_line,
)

__all__ = [
    "GeometryError",
    "ZoneDefinition",
    "ZoneEngine",
    "ZoneEventKind",
    "ZoneKind",
    "ZoneObservation",
    "ZoneOccupancy",
    "bbox_polygon_overlap_ratio",
    "distance_point_to_segment",
    "normalise_line",
    "normalise_polygon",
    "point_in_polygon",
    "points_in_polygon",
    "polygon_area",
    "polygon_bounds",
    "polygon_centroid",
    "segments_intersect",
    "side_of_line",
]
