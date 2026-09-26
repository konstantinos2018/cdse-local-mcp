"""download_window end to end, over a fake object store serving a real raster."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.io import MemoryFile
from rasterio.transform import from_origin
from rasterio.warp import transform, transform_bounds

from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain.models import JobState
from cdse_local_mcp.errors import AuthRequired, InvalidRequest
from cdse_local_mcp.tools import discovery, downloads, windows
from tests.unit.test_downloads import (  # noqa: F401 - the autouse fixture must be imported
    PRODUCT_ID,
    S3_PREFIX,
    UUID,
    FakeODataClient,
    FakeS3Client,
    FakeStac,
    _info,
    _run_to_completion,
    isolated_download_dir,
)

UTM_34N = "EPSG:32634"
SIZE = 2000
BAND_KEY = "GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B04.jp2"
META_KEY = "MTD_MSIL1C.xml"
INSIDE = [21.50, 38.25, 21.55, 38.30]


def _band_bytes() -> tuple[bytes, list[float]]:
    """A real UTM GeoTIFF, returned as bytes plus its lon/lat footprint."""
    xs, ys = transform("EPSG:4326", UTM_34N, [21.45], [38.35])
    affine = from_origin(xs[0], ys[0], 10.0, 10.0)
    data = np.arange(SIZE * SIZE, dtype=np.uint32).reshape(SIZE, SIZE) % 60000 + 1
    with MemoryFile() as mem:
        with mem.open(
            driver="GTiff",
            width=SIZE,
            height=SIZE,
            count=1,
            dtype="uint16",
            crs=UTM_34N,
            transform=affine,
            nodata=0,
        ) as dst:
            dst.write(data.astype("uint16"), 1)
            footprint = list(transform_bounds(UTM_34N, "EPSG:4326", *dst.bounds))
        return mem.read(), footprint


BAND, FOOTPRINT = _band_bytes()


def _wire(*, collection: str = "sentinel-2-l1c") -> FakeS3Client:
    item = {
        "id": PRODUCT_ID,
        "collection": collection,
        "bbox": FOOTPRINT,
        "properties": {"datetime": "2024-07-16T09:15:59Z"},
        "assets": {
            "B04": {"href": f"s3://eodata/{S3_PREFIX}/{BAND_KEY}", "type": "image/jp2"},
            "product_metadata": {
                "href": f"s3://eodata/{S3_PREFIX}/{META_KEY}",
                "type": "application/xml",
            },
            "Product": {
                "href": f"https://download.dataspace.copernicus.eu/odata/v1/Products({UUID})/$value"
            },
        },
    }
    discovery.set_client(FakeStac(item))  # type: ignore[arg-type]
    downloads.set_odata(FakeODataClient(_info()))  # type: ignore[arg-type]
    s3 = FakeS3Client({BAND_KEY: BAND, META_KEY: b"<xml/>"})
    downloads.set_s3(s3)  # type: ignore[arg-type]
    return s3


async def _crop(bbox: list[float], assets: list[str] | None = None):  # type: ignore[no-untyped-def]
    started = await windows.download_window(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=assets or ["B04"], bbox=bbox
    )
    await _run_to_completion(started.jobs[0].job_id)
    done = (await downloads.download_status(job_id=started.jobs[0].job_id)).jobs[0]
    return started, done


async def test_a_window_becomes_a_georeferenced_geotiff() -> None:
    _wire()

    _, done = await _crop(INSIDE)

    assert done.state is JobState.COMPLETED, done.error
    assert len(done.outputs) == 1
    with rasterio.open(done.outputs[0]) as out:
        assert out.crs.to_epsg() == 32634, "native UTM, not reprojected to lon/lat"
        assert out.res == (10.0, 10.0)
        assert out.width < SIZE and out.height < SIZE, "it must actually be a crop"
    assert "10 m" in done.notes[0]


async def test_a_second_window_over_the_same_band_transfers_nothing() -> None:
    s3 = _wire()

    await _crop(INSIDE)
    started, done = await _crop([21.52, 38.26, 21.54, 38.28])

    assert len(s3.objects_streamed) == 1, "the cached band was fetched again"
    assert any("Fetching 0 B" in n and "already cached" in n for n in started.notes)
    assert done.state is JobState.COMPLETED
    assert done.cached is True


async def test_different_windows_do_not_overwrite_each_other() -> None:
    _wire()

    _, first = await _crop(INSIDE)
    _, second = await _crop([21.52, 38.26, 21.54, 38.28])

    assert first.outputs[0] != second.outputs[0]
    assert Path(first.outputs[0]).exists() and Path(second.outputs[0]).exists()


async def test_partial_coverage_is_reported_rather_than_padded() -> None:
    _wire()

    _, done = await _crop([21.30, 38.25, 21.55, 38.30])  # hangs off the west edge

    assert done.state is JobState.COMPLETED
    assert "PARTIAL" in done.notes[0]


async def test_a_bbox_missing_the_granule_is_refused_before_any_transfer() -> None:
    """Finding out after fetching a 110 MB band would waste the whole transfer."""
    s3 = _wire()

    with pytest.raises(InvalidRequest, match="does not overlap"):
        await windows.download_window(
            collection="sentinel-2-l1c",
            product_id=PRODUCT_ID,
            assets=["B04"],
            bbox=[23.0, 39.0, 23.1, 39.1],
        )

    assert s3.objects_streamed == []
    assert (await downloads.download_status()).jobs == []


async def test_swath_collections_are_redirected_to_download_assets() -> None:
    _wire(collection="sentinel-3-olci-2-wfr-ntc")

    with pytest.raises(InvalidRequest) as exc:
        await windows.download_window(
            collection="sentinel-3-olci-2-wfr-ntc",
            product_id=PRODUCT_ID,
            assets=["chl-Nn"],
            bbox=INSIDE,
        )

    assert "swath" in exc.value.message
    assert "download_assets" in (exc.value.hint or "")
    assert "geo-coordinates" in (exc.value.hint or "")


async def test_only_rasters_can_be_windowed() -> None:
    _wire()

    with pytest.raises(InvalidRequest) as exc:
        await windows.download_window(
            collection="sentinel-2-l1c",
            product_id=PRODUCT_ID,
            assets=["product_metadata"],
            bbox=INSIDE,
        )

    hint = exc.value.hint or ""
    assert "B04" in hint
    assert "product_metadata" not in hint, "the hint must list only what can be cropped"


async def test_windowing_needs_s3_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDSE_S3_ACCESS_KEY", raising=False)
    get_settings.cache_clear()
    _wire()

    with pytest.raises(AuthRequired):
        await windows.download_window(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["B04"], bbox=INSIDE
        )
