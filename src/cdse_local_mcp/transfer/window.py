"""Cropping a bounding box out of gridded rasters.

**Windows are cut locally, from bands fetched through the normal budgeted path.** Reading a
window remotely over GDAL's ``/vsis3`` looks like the obvious design and is not. Measured on
2026-09-26 against live CDSE, one Sentinel-2 10 m band, a 13 x 11 km window, identical pixels
from every method:

    method                          GETs   bytes     time
    remote window, GDAL defaults      89    6.5 MB   16-18 s  (110 s once)
    remote window, 1 MB chunks        20   93   MB    7.1 s
    remote window, 4 MB chunks        15  105   MB    9.0 s
    fetch whole band, crop locally     1  116   MB    4.9 s

JPEG 2000 forces GDAL through ~89 small reads of the codestream whatever the chunk size, so
remote windowing saves bytes but costs time, requests and latency variance - and its own
networking would bypass the budget semaphore every transfer must hold. Bytes are the cheapest
resource CDSE rations (12 TB a month against 10,000 requests), so fetching wins.

The fetched band stays in the product tree, which makes a second window over it free.

Remote windowing would win on Cloud-Optimised GeoTIFFs. None of the first-class collections
are COGs, so that is a later optimisation, not a gap.

Scope is **cropping, not computing**: no resampling, no band stacking, no reprojection of
pixels. Each band is written at its native resolution and CRS. Stacking bands of different
resolutions needs a resampling choice, which is analysis the caller should make knowingly.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cdse_local_mcp.clients.s3 import S3Client, S3Object
from cdse_local_mcp.domain.geometry import Bbox
from cdse_local_mcp.errors import InvalidRequest, UpstreamError
from cdse_local_mcp.transfer.budget import TransferBudget
from cdse_local_mcp.transfer.bulk import ProgressCallback, download_objects
from cdse_local_mcp.transfer.paths import partial_path, resolve_target

logger = logging.getLogger(__name__)

# Densify the bbox edges when reprojecting: a lon/lat rectangle is not a rectangle in UTM,
# and transforming only its four corners clips the curved edges.
_DENSIFY_POINTS = 21
_TILE = 256


@dataclass(frozen=True)
class WindowResult:
    """One cropped band, described well enough to be checked without opening it."""

    asset: str
    path: Path
    width: int
    height: int
    crs: str
    resolution_m: float
    covered_bounds: Bbox
    partial_coverage: bool
    size_bytes: int


def _rasterio() -> Any:
    """Import rasterio on first use: it pulls in GDAL, and most tool calls never need it."""
    try:
        import rasterio
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise UpstreamError(
            "rasterio is not installed, so windowed extraction is unavailable.",
            hint="rasterio is a required dependency; reinstall the package to restore it.",
        ) from exc
    return rasterio


def _pixel_window(src: Any, bbox_wgs84: Bbox) -> tuple[int, int, int, int]:
    """Integer pixel bounds ``(col_off, row_off, col_end, row_end)``, clamped to the raster.

    Floors the start and ceils the end, so a pixel the bbox only partly covers is included
    rather than silently dropped at the edge.
    """
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds

    native = transform_bounds("EPSG:4326", src.crs, *bbox_wgs84, densify_pts=_DENSIFY_POINTS)
    window = from_bounds(*native, transform=src.transform)

    col_off = max(math.floor(window.col_off), 0)
    row_off = max(math.floor(window.row_off), 0)
    col_end = min(math.ceil(window.col_off + window.width), src.width)
    row_end = min(math.ceil(window.row_off + window.height), src.height)

    if col_end <= col_off or row_end <= row_off:
        raise InvalidRequest(
            "The bounding box does not overlap this product.",
            hint=(
                "Search again with this bbox so only products covering it are returned. "
                "The bbox is [west, south, east, north] in EPSG:4326 lon/lat."
            ),
        )
    return col_off, row_off, col_end, row_end


def crop_to_geotiff(source: Path, bbox_wgs84: Bbox, target: Path, *, asset: str) -> WindowResult:
    """Cut ``bbox_wgs84`` out of a local raster and write it as a GeoTIFF.

    Synchronous and CPU-bound: call it through ``asyncio.to_thread``. The output keeps the
    source's CRS, resolution, data type and nodata, is losslessly compressed, and appears at
    ``target`` only once complete.
    """
    rasterio = _rasterio()
    from rasterio.warp import transform_bounds
    from rasterio.windows import Window
    from rasterio.windows import bounds as window_bounds

    with rasterio.open(source) as src:
        if src.crs is None or src.transform.is_identity:
            raise InvalidRequest(
                f"{asset} has no map projection, so a bounding box cannot be cut from it.",
                hint="Swath products need per-pixel lat/lon extraction instead.",
            )

        col_off, row_off, col_end, row_end = _pixel_window(src, bbox_wgs84)
        window = Window(col_off, row_off, col_end - col_off, row_end - row_off)
        data = src.read(window=window)
        transform = src.window_transform(window)

        profile = {
            "driver": "GTiff",
            "width": int(window.width),
            "height": int(window.height),
            "count": src.count,
            "dtype": src.dtypes[0],
            "crs": src.crs,
            "transform": transform,
            "nodata": src.nodata,
            # Lossless: reflectance is integer-coded, and a lossy crop would corrupt it.
            "compress": "deflate",
            "predictor": 2,
            "tiled": True,
            "blockxsize": _TILE,
            "blockysize": _TILE,
        }

        part = partial_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with rasterio.open(part, "w", **profile) as dst:
            dst.write(data)
        part.replace(target)

        left, bottom, right, top = window_bounds(window, src.transform)
        covered = transform_bounds(
            src.crs, "EPSG:4326", left, bottom, right, top, densify_pts=_DENSIFY_POINTS
        )
        partial = (
            col_off == 0 or row_off == 0 or col_end == src.width or row_end == src.height
        ) and not _contains(covered, bbox_wgs84)

        result = WindowResult(
            asset=asset,
            path=target,
            width=int(window.width),
            height=int(window.height),
            crs=str(src.crs),
            resolution_m=float(abs(src.res[0])),
            covered_bounds=(covered[0], covered[1], covered[2], covered[3]),
            partial_coverage=partial,
            size_bytes=target.stat().st_size,
        )

    logger.info(
        "cropped %s to %dx%d px (%d bytes)%s",
        asset,
        result.width,
        result.height,
        result.size_bytes,
        ", partial coverage" if partial else "",
    )
    return result


def _contains(outer: tuple[float, ...], inner: Bbox, tolerance: float = 1e-4) -> bool:
    """Whether ``outer`` covers ``inner`` in lon/lat, allowing for reprojection rounding."""
    return (
        outer[0] <= inner[0] + tolerance
        and outer[1] <= inner[1] + tolerance
        and outer[2] >= inner[2] - tolerance
        and outer[3] >= inner[3] - tolerance
    )


def window_filename(asset: str, bbox_wgs84: Bbox) -> str:
    """A self-describing output name: which band, and exactly which area.

    Two different windows over the same band must not overwrite each other, and a name that
    states its bbox can be checked by eye.
    """
    west, south, east, north = bbox_wgs84
    return f"{asset}_{west:.4f}_{south:.4f}_{east:.4f}_{north:.4f}.tif"


def product_stem(product_name: str) -> str:
    """``S2B_..._T34SEH_20240716T101056.SAFE`` -> ``S2B_..._T34SEH_20240716T101056``."""
    for suffix in (".SAFE", ".SEN3", ".zip"):
        if product_name.endswith(suffix):
            return product_name[: -len(suffix)]
    return product_name


async def extract_windows(
    *,
    client: S3Client,
    bucket: str,
    prefix: str,
    objects: list[tuple[str, S3Object]],
    destination_root: Path,
    product_name: str,
    bbox: Bbox,
    budget: TransferBudget,
    progress: ProgressCallback | None = None,
) -> list[WindowResult]:
    """Fetch each source band through the budgeted path, then crop it locally.

    Bands land in the product tree exactly as ``download_assets`` would place them, so a band
    already fetched - by either tool - is a cache hit and the crop costs no transfer at all.
    Crops go under ``windows/<product>/``, named by band and bbox.
    """
    await download_objects(
        client=client,
        bucket=bucket,
        prefix=prefix,
        objects=[obj for _, obj in objects],
        destination_root=destination_root,
        directory_name=product_name,
        budget=budget,
        progress=progress,
    )

    stem = product_stem(product_name)
    results: list[WindowResult] = []
    for asset, obj in objects:
        source = resolve_target(destination_root, product_name, *obj.relative_to(prefix).split("/"))
        target = resolve_target(destination_root, "windows", stem, window_filename(asset, bbox))
        # Decoding JPEG 2000 is CPU-bound; keep it off the event loop.
        results.append(await asyncio.to_thread(crop_to_geotiff, source, bbox, target, asset=asset))
    return results
