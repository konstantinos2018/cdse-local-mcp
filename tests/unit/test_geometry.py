"""Area parsing and validation."""

from __future__ import annotations

import pytest

from cdse_local_mcp.domain.geometry import POINT_BUFFER_DEGREES, resolve_area
from cdse_local_mcp.errors import InvalidRequest

GULF_OF_PATRAS = [21.3, 38.1, 21.9, 38.4]


def test_bbox_round_trips_and_reports_itself() -> None:
    area = resolve_area(bbox=GULF_OF_PATRAS)
    assert area.as_list == GULF_OF_PATRAS
    assert "EPSG:4326" in area.describe()


def test_swap_is_caught_when_a_longitude_lands_in_a_latitude_slot() -> None:
    # Off the coast of Japan, swapped: longitude 139.7 cannot be a latitude.
    with pytest.raises(InvalidRequest) as exc:
        resolve_area(bbox=[35.6, 139.7, 35.8, 139.9])
    assert "lon/lat" in (exc.value.hint or "")


def test_an_in_range_swap_is_undetectable_and_must_be_echoed_instead() -> None:
    """A swapped Gulf of Patras box is a *valid* box somewhere else entirely.

    No validation can catch this, which is exactly why every search echoes back the area it
    used. This test pins that limitation so nobody later claims swaps are detected.
    """
    area = resolve_area(bbox=[38.1, 21.3, 38.4, 21.9])
    assert area.as_list == [38.1, 21.3, 38.4, 21.9]
    assert "38.1000,21.3000" in area.describe()


def test_latitude_out_of_range_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="south"):
        resolve_area(bbox=[21.3, -91.0, 21.9, 38.4])


def test_antimeridian_crossing_is_rejected_rather_than_silently_empty() -> None:
    with pytest.raises(InvalidRequest) as exc:
        resolve_area(bbox=[179.0, -10.0, -179.0, 10.0])
    assert "antimeridian" in (exc.value.hint or "").lower()


def test_inverted_north_south_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="north of"):
        resolve_area(bbox=[21.3, 38.4, 21.9, 38.1])


def test_wrong_length_bbox_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="exactly 4"):
        resolve_area(bbox=[21.3, 38.1, 21.9])


def test_wkt_reduces_to_bounds() -> None:
    area = resolve_area(wkt="POLYGON((21.3 38.1, 21.9 38.1, 21.9 38.4, 21.3 38.4, 21.3 38.1))")
    assert area.as_list == pytest.approx(GULF_OF_PATRAS)
    assert area.source == "wkt"


def test_invalid_wkt_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="WKT"):
        resolve_area(wkt="NOT A GEOMETRY")


def test_geojson_feature_reduces_to_bounds() -> None:
    feature = {
        "type": "Feature",
        "properties": {},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[21.3, 38.1], [21.9, 38.1], [21.9, 38.4], [21.3, 38.4], [21.3, 38.1]]],
        },
    }
    area = resolve_area(geojson=feature)
    assert area.as_list == pytest.approx(GULF_OF_PATRAS)


def test_a_point_is_buffered_into_a_searchable_box_and_says_so() -> None:
    area = resolve_area(geojson='{"type": "Point", "coordinates": [21.6, 38.25]}')
    west, south, east, north = area.as_list
    assert west == pytest.approx(21.6 - POINT_BUFFER_DEGREES)
    assert east == pytest.approx(21.6 + POINT_BUFFER_DEGREES)
    assert south == pytest.approx(38.25 - POINT_BUFFER_DEGREES)
    assert north == pytest.approx(38.25 + POINT_BUFFER_DEGREES)
    assert "buffered" in area.describe()


def test_buffering_a_polar_point_stays_within_valid_latitudes() -> None:
    area = resolve_area(wkt="POINT(0 90)")
    assert area.as_list[3] == pytest.approx(90.0)


def test_no_area_is_rejected_with_guidance() -> None:
    with pytest.raises(InvalidRequest) as exc:
        resolve_area()
    assert "bbox" in (exc.value.hint or "")


def test_two_areas_at_once_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="only one"):
        resolve_area(bbox=GULF_OF_PATRAS, wkt="POINT(21.6 38.25)")
