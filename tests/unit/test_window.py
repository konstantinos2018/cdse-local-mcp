"""Cropping: correct pixels, correct place, honest about coverage.

A crop that is off by a row, or lands in the wrong UTM position, still produces a
plausible-looking image. So these tests check pixel values against the source, not just
dimensions.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin
from rasterio.warp import transform

from cdse_local_mcp.errors import InvalidRequest
from cdse_local_mcp.transfer.paths import partial_path
from cdse_local_mcp.transfer.window import crop_to_geotiff, window_filename

UTM_34N = "EPSG:32634"  # Gulf of Patras, tile 34SEH
RES = 10.0
SIZE = 2000  # 20 x 20 km
WEST_LON, NORTH_LAT = 21.45, 38.35


def _make_band(path: Path) -> tuple[np.ndarray, rasterio.Affine]:
    """A uint16 band in UTM with a unique value per pixel, so any offset error shows."""
    xs, ys = transform("EPSG:4326", UTM_34N, [WEST_LON], [NORTH_LAT])
    affine = from_origin(xs[0], ys[0], RES, RES)
    data = (np.arange(SIZE * SIZE, dtype=np.uint32).reshape(SIZE, SIZE) % 60000 + 1).astype(
        np.uint16
    )
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=SIZE,
        height=SIZE,
        count=1,
        dtype="uint16",
        crs=UTM_34N,
        transform=affine,
        nodata=0,
    ) as dst:
        dst.write(data, 1)
    return data, affine


@pytest.fixture
def band(tmp_path: Path) -> tuple[Path, np.ndarray, rasterio.Affine]:
    path = tmp_path / "B04.jp2"  # the extension lies; GDAL identifies by content
    data, affine = _make_band(path)
    return path, data, affine


def _offsets(out_transform: rasterio.Affine, src_transform: rasterio.Affine) -> tuple[int, int]:
    col = round((out_transform.c - src_transform.c) / RES)
    row = round((src_transform.f - out_transform.f) / RES)
    return col, row


def test_the_crop_holds_exactly_the_source_pixels(tmp_path: Path, band: tuple) -> None:
    source, data, affine = band
    bbox = (21.50, 38.25, 21.55, 38.30)

    result = crop_to_geotiff(source, bbox, tmp_path / "out.tif", asset="B04")

    with rasterio.open(result.path) as out:
        cropped = out.read(1)
        col, row = _offsets(out.transform, affine)
        assert out.crs.to_epsg() == 32634, "the crop must keep the native UTM projection"
        assert out.res == (RES, RES), "no resampling"

    expected = data[row : row + result.height, col : col + result.width]
    np.testing.assert_array_equal(cropped, expected)
    assert result.partial_coverage is False


def test_the_crop_covers_the_requested_area(tmp_path: Path, band: tuple) -> None:
    """Densified reprojection: UTM edges curve in lon/lat, and a 4-corner transform clips."""
    source, _, _ = band
    bbox = (21.50, 38.25, 21.55, 38.30)

    result = crop_to_geotiff(source, bbox, tmp_path / "out.tif", asset="B04")

    w, s, e, n = result.covered_bounds
    assert w <= bbox[0] and s <= bbox[1] and e >= bbox[2] and n >= bbox[3]


def test_a_bbox_hanging_off_the_granule_is_clamped_and_flagged(tmp_path: Path, band: tuple) -> None:
    source, _, _ = band
    bbox = (21.30, 38.25, 21.55, 38.30)  # extends well west of the raster

    result = crop_to_geotiff(source, bbox, tmp_path / "out.tif", asset="B04")

    assert result.partial_coverage is True
    assert result.covered_bounds[0] > bbox[0], "reported coverage must be the real extent"


def test_a_bbox_entirely_outside_is_refused_with_guidance(tmp_path: Path, band: tuple) -> None:
    source, _, _ = band

    with pytest.raises(InvalidRequest) as exc:
        crop_to_geotiff(source, (23.0, 39.0, 23.1, 39.1), tmp_path / "out.tif", asset="B04")

    assert "does not overlap" in exc.value.message
    assert not (tmp_path / "out.tif").exists()


def test_a_sub_pixel_bbox_still_yields_a_pixel(tmp_path: Path, band: tuple) -> None:
    """Flooring the start and ceiling the end keeps partly covered edge pixels."""
    source, _, _ = band

    result = crop_to_geotiff(
        source, (21.5000, 38.3000, 21.50001, 38.30001), tmp_path / "out.tif", asset="B04"
    )

    assert result.width >= 1 and result.height >= 1


def test_output_is_losslessly_compressed_tiled_and_complete(tmp_path: Path, band: tuple) -> None:
    source, _, _ = band
    target = tmp_path / "out.tif"

    crop_to_geotiff(source, (21.50, 38.25, 21.55, 38.30), target, asset="B04")

    with rasterio.open(target) as out:
        assert out.compression.name.lower() == "deflate"
        assert out.block_shapes[0] == (256, 256), "output must be tiled"
        assert out.nodata == 0, "nodata must survive the crop"
    assert not partial_path(target).exists()


def test_an_unprojected_raster_is_refused(tmp_path: Path) -> None:
    """Swath data has no transform; cropping it by bbox would produce pixels located nowhere."""
    path = tmp_path / "swath.tif"
    with rasterio.open(
        path, "w", driver="GTiff", width=10, height=10, count=1, dtype="uint16"
    ) as dst:
        dst.write(np.ones((1, 10, 10), dtype="uint16"))

    with pytest.raises(InvalidRequest, match="no map projection"):
        crop_to_geotiff(path, (21.5, 38.2, 21.6, 38.3), tmp_path / "out.tif", asset="x")


def test_window_filenames_state_the_band_and_the_area() -> None:
    assert window_filename("B04", (21.55, 38.2, 21.7, 38.3)) == (
        "B04_21.5500_38.2000_21.7000_38.3000.tif"
    )
