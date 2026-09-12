"""Area-of-interest parsing and validation.

All input geometry is EPSG:4326 lon/lat. Granule CRSs (UTM for Sentinel-2) are handled at
read time, not here — see ``docs/collections.md``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from shapely.geometry import shape
from shapely.wkt import loads as wkt_loads

from cdse_local_mcp.errors import InvalidRequest

Bbox = tuple[float, float, float, float]

# A point or a zero-width line is a reasonable thing to ask for ("imagery over this spot"),
# but a zero-area bbox is not a reliable search input. Expand it, and say so in the echoed
# description rather than doing it silently.
POINT_BUFFER_DEGREES = 0.005


@dataclass(frozen=True)
class Area:
    """A validated bounding box in EPSG:4326, plus a note on where it came from."""

    bbox: Bbox
    source: str

    @property
    def as_list(self) -> list[float]:
        return list(self.bbox)

    def describe(self) -> str:
        w, s, e, n = self.bbox
        return f"{w:.4f},{s:.4f},{e:.4f},{n:.4f} (EPSG:4326 lon/lat, from {self.source})"


def _validate(bbox: Bbox, source: str) -> Area:
    west, south, east, north = (float(v) for v in bbox)

    for name, value, limit in (
        ("west", west, 180.0),
        ("east", east, 180.0),
        ("south", south, 90.0),
        ("north", north, 90.0),
    ):
        if not -limit <= value <= limit:
            raise InvalidRequest(
                f"{name}={value} is outside the valid range [{-limit}, {limit}].",
                hint=(
                    "Bounding boxes are [west, south, east, north] in EPSG:4326 "
                    "lon/lat order - longitude first, not latitude."
                ),
            )

    if south > north:
        raise InvalidRequest(
            f"south={south} is north of north={north}.",
            hint="Order is [west, south, east, north]; check you did not swap lat and lon.",
        )

    if west > east:
        raise InvalidRequest(
            f"west={west} is east of east={east}.",
            hint=(
                "Either the values are inverted, or the box crosses the antimeridian, which "
                "is not supported. To span the antimeridian, split it into one box west of "
                "180 and one east of -180."
            ),
        )

    # Degenerate geometry: a point or a line has no area to search.
    buffered = False
    if west == east:
        west, east = west - POINT_BUFFER_DEGREES, east + POINT_BUFFER_DEGREES
        buffered = True
    if south == north:
        south, north = south - POINT_BUFFER_DEGREES, north + POINT_BUFFER_DEGREES
        buffered = True

    if buffered:
        source = f"{source}, buffered by {POINT_BUFFER_DEGREES} degrees to give it area"
        west, east = max(west, -180.0), min(east, 180.0)
        south, north = max(south, -90.0), min(north, 90.0)

    return Area(bbox=(west, south, east, north), source=source)


def area_from_bbox(bbox: list[float]) -> Area:
    """Validate a ``[west, south, east, north]`` list."""
    if len(bbox) != 4:
        raise InvalidRequest(
            f"bbox needs exactly 4 numbers, got {len(bbox)}.",
            hint="Use [west, south, east, north] in EPSG:4326 lon/lat order.",
        )
    return _validate((bbox[0], bbox[1], bbox[2], bbox[3]), "bbox")


def area_from_wkt(wkt: str) -> Area:
    """Validate WKT and reduce it to its bounding box."""
    try:
        geom = wkt_loads(wkt)
    except Exception as exc:
        raise InvalidRequest(
            f"Could not parse WKT: {exc}",
            hint="Provide valid WKT such as POLYGON((21.3 38.1, 21.9 38.1, ...)).",
        ) from exc
    if geom.is_empty:
        raise InvalidRequest("The WKT geometry is empty.", hint="Provide a non-empty geometry.")
    return _validate(geom.bounds, "wkt")


def area_from_geojson(geojson: str | dict[str, Any]) -> Area:
    """Validate a GeoJSON geometry, Feature or FeatureCollection and take its bounds."""
    raw = geojson
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InvalidRequest(
                f"Could not parse GeoJSON: {exc}", hint="Pass valid JSON."
            ) from exc
    if not isinstance(raw, dict):
        raise InvalidRequest("GeoJSON must be an object.")

    geom_obj: Any = raw
    if raw.get("type") == "Feature":
        geom_obj = raw.get("geometry")
    elif raw.get("type") == "FeatureCollection":
        features = raw.get("features") or []
        if not features:
            raise InvalidRequest("The FeatureCollection has no features.")
        geom_obj = {
            "type": "GeometryCollection",
            "geometries": [f.get("geometry") for f in features if f.get("geometry")],
        }

    try:
        geom = shape(geom_obj)
    except Exception as exc:
        raise InvalidRequest(
            f"Could not interpret the GeoJSON geometry: {exc}",
            hint="Supply a geometry, Feature or FeatureCollection with valid coordinates.",
        ) from exc
    if geom.is_empty:
        raise InvalidRequest("The GeoJSON geometry is empty.")
    return _validate(geom.bounds, "geojson")


def resolve_area(
    bbox: list[float] | None = None,
    wkt: str | None = None,
    geojson: str | dict[str, Any] | None = None,
) -> Area:
    """Accept exactly one geometry form and return a validated :class:`Area`."""
    provided = [name for name, val in (("bbox", bbox), ("wkt", wkt), ("geojson", geojson)) if val]
    if not provided:
        raise InvalidRequest(
            "No area of interest given.",
            hint=(
                "Pass one of bbox=[west, south, east, north], wkt=..., or geojson=.... "
                "For a place name, supply its approximate bounding box in EPSG:4326."
            ),
        )
    if len(provided) > 1:
        raise InvalidRequest(
            f"Give only one area of interest, got {' and '.join(provided)}.",
            hint="Pick whichever is most precise and drop the others.",
        )

    if bbox is not None:
        return area_from_bbox(bbox)
    if wkt is not None:
        return area_from_wkt(wkt)
    assert geojson is not None
    return area_from_geojson(geojson)
