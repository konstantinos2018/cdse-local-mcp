"""Download tools.

Downloads are jobs, not calls: a Sentinel-2 product takes minutes and MCP clients time out
in about a minute. Each tool here validates, checks the quota budget, enqueues, and returns
immediately with a `job_id` to poll.

Nothing here accepts a search query. Product ids come from `search_products`, so a human has
seen the candidates and their sizes before any bytes move.
"""

from __future__ import annotations

import logging

from cdse_local_mcp.clients.s3 import S3Object, split_s3_path
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain import collections as coll
from cdse_local_mcp.domain.models import DownloadReport, Job, human_bytes
from cdse_local_mcp.errors import InvalidRequest, NotFound, tool_guard
from cdse_local_mcp.tools import DOWNLOADS, READ_ONLY, ToolDef
from cdse_local_mcp.tools._shared import (
    _budget,
    _product_context,
    _registry,
    _report,
    _require_s3,
    _s3_href,
    get_odata,
    get_s3,
    set_odata,
    set_s3,
)
from cdse_local_mcp.transfer.bulk import download_objects

logger = logging.getLogger(__name__)

# Re-exported so callers and tests have one obvious place to swap clients.
__all__ = [
    "download_assets",
    "download_cancel",
    "download_product",
    "download_status",
    "get_odata",
    "get_s3",
    "set_odata",
    "set_s3",
]


async def download_product(
    collection: str,
    product_id: str,
    odata_uuid: str | None = None,
    confirm: bool = False,
) -> DownloadReport:
    """Download a complete product to local disk, as a background job.

    This is the expensive option: a Sentinel-2 product is around 800 MB spread over hundreds
    of files, while a single band is ~150 MB. Prefer `list_product_assets` and fetch only what
    is needed, unless the user genuinely wants the whole product.

    Call this only with a `product_id` the user has chosen from a `search_products` result,
    after telling them the size. If the product is larger than the per-call cap the tool
    refuses: report the size and call again with `confirm=True` only if they agree.

    The result is a `.SAFE` **directory**, not a zip. CDSE stores products unpacked on S3, so
    the tree is rebuilt as-is and needs no extraction.

    Returns immediately with a `job_id`. Poll `download_status` for progress; the directory
    path appears when the job completes. Files are never returned through this channel.

    Requires S3 access keys. The OAuth client cannot authorise downloads.
    """
    settings = get_settings()
    _require_s3()
    canonical = coll.canonical_collection_id(collection)
    name, bucket, prefix, _uuid, _assets = await _product_context(canonical, product_id, odata_uuid)

    s3 = get_s3()
    objects = await s3.list_objects(bucket, prefix)
    total = sum(obj.size for obj in objects)

    _budget().check(total, confirmed=confirm)

    registry = _registry()
    job = registry.create(product_id=product_id, collection=canonical, bytes_total=total)

    async def runner(current: Job) -> None:
        async def on_progress(done: int, job_total: int | None) -> None:
            registry.progress(current, done, job_total)

        result = await download_objects(
            client=s3,
            bucket=bucket,
            prefix=prefix,
            objects=objects,
            destination_root=settings.download_dir,
            directory_name=name,
            budget=_budget(),
            progress=on_progress,
        )
        registry.finish(
            current,
            path=result.directory,
            cached=result.files_cached == len(objects),
            verified=False,
        )

    registry.start(job, runner)

    return _report(
        [job],
        [
            f"Downloading {name} ({len(objects)} files, "
            f"~{human_bytes(total) or 'unknown size'}) into {settings.download_dir}.",
            f"Poll download_status with job_id={job.job_id}. Do not poll in a tight loop.",
            "The result is an unpacked .SAFE directory, not a zip archive.",
        ],
    )


async def download_assets(
    collection: str,
    product_id: str,
    assets: list[str],
    odata_uuid: str | None = None,
    confirm: bool = False,
) -> DownloadReport:
    """Download only the named files from a product, as a background job.

    Almost always the right choice. A Sentinel-2 product is ~785 MiB across 66 files, but one
    10 m band is ~110 MiB and a 60 m band is ~3 MiB. For Sentinel-3 OLCI water quality, three
    variables plus geolocation are tens of megabytes against a ~250 MiB product.

    `assets` takes STAC asset keys or the friendly names from `list_product_assets`:
      - Sentinel-2 bands: `B02` `B03` `B04` `B08` ... `B8A`, `TCI`, or `red_10m`, `nir_10m`
      - OLCI water quality: `chl-Nn`, `tsm-Nn`, `trsp`, `wqsf`, or `chlorophyll_nn`

    For OLCI, always include `geo-coordinates` — it holds the per-pixel latitude and longitude
    without which the values cannot be placed on a map — and `wqsf`, the quality flags that
    must be applied before any retrieval is interpreted.

    Files are written into the product's `.SAFE` or `.SEN3` directory structure, so fetching
    more assets later fills in the same tree and already-present files are not re-downloaded.

    Returns immediately with a `job_id`; poll `download_status`. Requires S3 access keys.
    """
    settings = get_settings()
    _require_s3()

    if not assets:
        raise InvalidRequest(
            "No assets were requested.",
            hint="Call list_product_assets to see what this product offers, then name some.",
        )

    canonical = coll.canonical_collection_id(collection)
    name, bucket, prefix, _uuid, item_assets = await _product_context(
        canonical, product_id, odata_uuid
    )

    available = [key for key, value in item_assets.items() if _s3_href(value)]
    wanted: dict[str, str] = {}
    for request in assets:
        key = coll.match_asset_key(canonical, request, available)
        if key is None:
            raise InvalidRequest(
                f"{product_id} has no downloadable asset matching {request!r}.",
                hint=(
                    "Available: " + ", ".join(sorted(available)[:40]) + ". Friendly names from "
                    "list_product_assets also work."
                ),
            )
        wanted[key] = str(_s3_href(item_assets[key]))

    s3 = get_s3()
    sizes = {obj.key: obj.size for obj in await s3.list_objects(bucket, prefix)}
    objects: list[S3Object] = []
    for key, href in wanted.items():
        _, object_key = split_s3_path(href)
        if object_key not in sizes:
            raise NotFound(
                f"The catalogue lists {key} but the object store does not hold it.",
                hint="Try another product, or download the whole product instead.",
            )
        objects.append(S3Object(key=object_key, size=sizes[object_key]))

    total = sum(obj.size for obj in objects)
    _budget().check(total, confirmed=confirm)

    registry = _registry()
    job = registry.create(product_id=product_id, collection=canonical, bytes_total=total)

    async def runner(current: Job) -> None:
        async def on_progress(done: int, job_total: int | None) -> None:
            registry.progress(current, done, job_total)

        result = await download_objects(
            client=s3,
            bucket=bucket,
            prefix=prefix,
            objects=objects,
            destination_root=settings.download_dir,
            directory_name=name,
            budget=_budget(),
            progress=on_progress,
        )
        registry.finish(
            current,
            path=result.directory,
            cached=result.files_cached == len(objects),
            verified=False,
        )

    registry.start(job, runner)

    whole_product = sum(sizes.values())
    return _report(
        [job],
        [
            f"Downloading {len(objects)} asset(s) from {name}: {', '.join(sorted(wanted))} "
            f"(~{human_bytes(total)}), into {settings.download_dir}.",
            f"That is {human_bytes(total)} instead of {human_bytes(whole_product)} for the "
            "whole product.",
            f"Poll download_status with job_id={job.job_id}. Do not poll in a tight loop.",
        ],
    )


async def download_status(job_id: str | None = None, limit: int = 10) -> DownloadReport:
    """Check download progress.

    With a `job_id`, reports that job. Without one, lists recent jobs, most recent first.

    Poll at a human pace — every 10 to 30 seconds for a large product. Polling in a tight
    loop spends the catalogue request quota and makes the transfer no faster.

    A completed job carries the local file `path`. `cached: true` means the file was already
    present and verified, so nothing was transferred.
    """
    if limit < 1:
        raise InvalidRequest(f"limit={limit} must be at least 1.")

    registry = _registry()
    if job_id:
        return _report([registry.get(job_id)])

    jobs = registry.recent(limit)
    notes: list[str] = []
    if not jobs:
        notes.append("No downloads have been started in this session.")
    return _report(jobs, notes)


async def download_cancel(job_id: str) -> DownloadReport:
    """Cancel a running download.

    The partial file is kept, so starting the same download again resumes from where it
    stopped rather than beginning afresh.
    """
    registry = _registry()
    job = registry.cancel(job_id)
    return _report(
        [job],
        [
            "Cancellation requested. The partial file is kept; downloading the same product "
            "again will resume from it."
        ],
    )


TOOLS: list[ToolDef] = [
    ToolDef(tool_guard(download_product), DOWNLOADS),
    ToolDef(tool_guard(download_assets), DOWNLOADS),
    ToolDef(tool_guard(download_status), READ_ONLY),
    ToolDef(tool_guard(download_cancel), DOWNLOADS),
]
