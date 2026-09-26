"""The windowed extraction tool: a bounding box out of named bands, as GeoTIFFs."""

from __future__ import annotations

from typing import Any

from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain import collections as coll
from cdse_local_mcp.domain.geometry import resolve_area
from cdse_local_mcp.domain.models import DownloadReport, Job, human_bytes
from cdse_local_mcp.errors import InvalidRequest, tool_guard
from cdse_local_mcp.tools import DOWNLOADS, ToolDef
from cdse_local_mcp.tools._shared import (
    _budget,
    _product_context,
    _registry,
    _report,
    _require_s3,
    _select_assets,
    get_s3,
)
from cdse_local_mcp.transfer.bulk import pending_bytes
from cdse_local_mcp.transfer.paths import resolve_target
from cdse_local_mcp.transfer.window import WindowResult, extract_windows, product_stem

_RASTER_TYPES = ("jp2", "tiff", "geotiff")


def _is_raster(asset: dict[str, Any]) -> bool:
    media = str(asset.get("type") or "").lower()
    return any(kind in media for kind in _RASTER_TYPES)


def _overlaps(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    """Whether two lon/lat boxes intersect."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _describe(result: WindowResult) -> str:
    w, s, e, n = result.covered_bounds
    line = (
        f"{result.asset}: {result.width}x{result.height} px at {result.resolution_m:g} m, "
        f"{result.crs}, {human_bytes(result.size_bytes)}"
    )
    if result.partial_coverage:
        line += (
            f". PARTIAL: the bbox extends beyond this granule; the crop covers only "
            f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}"
        )
    return line


async def download_window(
    collection: str,
    product_id: str,
    assets: list[str],
    bbox: list[float] | None = None,
    wkt: str | None = None,
    geojson: str | None = None,
    odata_uuid: str | None = None,
    confirm: bool = False,
) -> DownloadReport:
    """Cut a bounding box out of named bands and save each as a GeoTIFF. Background job.

    Use this when the user wants an area, not a scene: "the Gulf of Patras from this
    image". Each band is written at its native resolution and projection - Sentinel-2
    granules are UTM, so a Gulf of Patras crop comes out in EPSG:32634, not lon/lat. Bands
    are never resampled or stacked; 10 m and 20 m bands stay separate files.

    Area: exactly one of `bbox` (`[west, south, east, north]`, EPSG:4326 lon/lat), `wkt` or
    `geojson`. If the area hangs off the edge of the granule the crop is clamped and the job
    reports PARTIAL coverage with the real extent - say so to the user.

    `assets` takes band keys or friendly names: `B04`, `red_10m`, `B08`, `nir_10m`, `TCI`.

    Cost: each band is fetched whole (~25-115 MiB for a 10 m band), then cropped locally.
    That is deliberate - it measured faster and far cheaper in requests than reading the
    window remotely. The band stays cached, so further windows over it transfer nothing.

    Sentinel-3 OLCI is not supported here: it is swath data with no map projection. Use
    `download_assets` with `geo-coordinates`, `wqsf` and the variables needed.

    Returns immediately with a `job_id`; poll `download_status`, whose `outputs` lists the
    GeoTIFFs and whose `notes` give each crop's size, resolution and coverage.
    """
    settings = get_settings()
    _require_s3()
    area = resolve_area(bbox=bbox, wkt=wkt, geojson=geojson)
    canonical = coll.canonical_collection_id(collection)

    spec = coll.spec_for(canonical)
    if spec is not None and spec.is_swath:
        raise InvalidRequest(
            f"{canonical} is swath data with no map projection, so it cannot be cropped by "
            "bounding box.",
            hint=(
                "Use download_assets with geo-coordinates (per-pixel lat/lon), wqsf (quality "
                "flags) and the variables you need, then mask by lat/lon."
            ),
        )

    ctx = await _product_context(canonical, product_id, odata_uuid)
    name, bucket, prefix = ctx.name, ctx.bucket, ctx.prefix
    # Check the footprint before fetching anything: finding out after a 110 MB band has
    # downloaded that the bbox misses the granule would waste the transfer entirely.
    if ctx.footprint is not None and not _overlaps(ctx.footprint, area.bbox):
        w, s, e, n = ctx.footprint
        raise InvalidRequest(
            f"The bounding box does not overlap {product_id}, which covers "
            f"{w:.4f},{s:.4f},{e:.4f},{n:.4f}.",
            hint="Search again with this bbox so only products covering it are returned.",
        )
    selected = await _select_assets(
        collection=canonical,
        product_id=product_id,
        requested=assets,
        item_assets=ctx.assets,
        bucket=bucket,
        prefix=prefix,
        accept=_is_raster,
        kind="raster",
    )
    source_objects = [obj for _, obj in selected.objects]
    to_fetch = pending_bytes(
        source_objects, destination_root=settings.download_dir, directory_name=name, prefix=prefix
    )
    _budget().check(to_fetch, confirmed=confirm)

    registry = _registry()
    # bytes_total is the selection, as in download_assets; progress counts a cached band as
    # present. What actually has to move is stated in the note and in `cached`.
    job = registry.create(
        product_id=product_id, collection=canonical, bytes_total=selected.total_bytes
    )
    out_dir = resolve_target(settings.download_dir, "windows", product_stem(name))

    async def runner(current: Job) -> None:
        async def on_progress(done: int, total: int | None) -> None:
            registry.progress(current, done, total)

        results = await extract_windows(
            client=get_s3(),
            bucket=bucket,
            prefix=prefix,
            objects=selected.objects,
            destination_root=settings.download_dir,
            product_name=name,
            bbox=area.bbox,
            budget=_budget(),
            progress=on_progress,
        )
        registry.finish(
            current,
            path=out_dir,
            cached=to_fetch == 0,
            verified=False,
            outputs=[r.path for r in results],
            notes=[_describe(r) for r in results],
        )

    registry.start(job, runner)

    cached = selected.total_bytes - to_fetch
    return _report(
        [job],
        [
            f"Cropping {', '.join(selected.keys)} from {name} to {area.describe()}.",
            f"Fetching {human_bytes(to_fetch)} of source bands"
            + (f"; {human_bytes(cached)} already cached." if cached else "."),
            f"GeoTIFFs will be written to {out_dir}, at native resolution and projection.",
            f"Poll download_status with job_id={job.job_id}. Do not poll in a tight loop.",
        ],
    )


TOOLS: list[ToolDef] = [ToolDef(tool_guard(download_window), DOWNLOADS)]
