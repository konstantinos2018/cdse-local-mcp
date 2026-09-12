"""Download tools.

Downloads are jobs, not calls: a Sentinel-2 product takes minutes and MCP clients time out
in about a minute. Each tool here validates, checks the quota budget, enqueues, and returns
immediately with a `job_id` to poll.

Nothing here accepts a search query. Product ids come from `search_products`, so a human has
seen the candidates and their sizes before any bytes move.
"""

from __future__ import annotations

import logging

from cdse_local_mcp.auth import TokenProvider
from cdse_local_mcp.clients.odata import ODataClient
from cdse_local_mcp.clients.s3 import S3Client, split_s3_path
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain import collections as coll
from cdse_local_mcp.domain.models import DownloadReport, Job, ProductSummary, human_bytes
from cdse_local_mcp.errors import (
    AuthRequired,
    InvalidRequest,
    NotFound,
    OfflineProduct,
    tool_guard,
)
from cdse_local_mcp.tools import DOWNLOADS, READ_ONLY, ToolDef, discovery
from cdse_local_mcp.transfer.budget import TransferBudget, get_budget
from cdse_local_mcp.transfer.bulk import download_objects
from cdse_local_mcp.transfer.jobs import JobRegistry, get_registry

logger = logging.getLogger(__name__)

_odata: ODataClient | None = None
_tokens: TokenProvider | None = None
_s3: S3Client | None = None


def get_odata() -> ODataClient:
    """Return the shared OData client, creating it and its token provider on first use."""
    global _odata, _tokens
    if _odata is None:
        settings = get_settings()
        _tokens = TokenProvider(settings)
        _odata = ODataClient(settings, _tokens)
    return _odata


def set_odata(client: ODataClient | None) -> None:
    """Replace the shared OData client. For tests."""
    global _odata
    _odata = client


def get_s3() -> S3Client:
    """Return the shared S3 client, creating it on first use."""
    global _s3
    if _s3 is None:
        _s3 = S3Client(get_settings())
    return _s3


def set_s3(client: S3Client | None) -> None:
    """Replace the shared S3 client. For tests."""
    global _s3
    _s3 = client


def _registry() -> JobRegistry:
    return get_registry(get_settings().download_dir)


def _budget() -> TransferBudget:
    return get_budget(get_settings())


def _report(jobs: list[Job], notes: list[str] | None = None) -> DownloadReport:
    budget = _budget()
    return DownloadReport(
        jobs=jobs,
        session_bytes_used=human_bytes(budget.spent_bytes),
        session_bytes_remaining=human_bytes(budget.remaining_bytes),
        notes=notes or [],
    )


async def _resolve_uuid(collection: str, product_id: str, odata_uuid: str | None) -> str:
    """Find the OData UUID for a product, preferring one the caller already has."""
    if odata_uuid:
        return odata_uuid

    item = await discovery.get_client().get_item(collection=collection, item_id=product_id)
    summary = ProductSummary.from_stac_item(item, collection)
    if not summary.odata_uuid:
        raise NotFound(
            f"{product_id} does not expose a downloadable archive.",
            hint="Check the product id, or use list_product_assets to see what is offered.",
        )
    return summary.odata_uuid


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
    # Check credentials first. Product metadata is readable without any, so without this the
    # tool would queue a job that cannot possibly run and report success.
    if not settings.has_s3:
        raise AuthRequired(
            "Downloading requires CDSE S3 access keys, which are not configured.",
            hint=(
                "Create keys at https://eodata-s3keysmanager.dataspace.copernicus.eu/ and set "
                "CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY, then restart the server. Search "
                "and list_product_assets keep working without them."
            ),
        )

    canonical = coll.canonical_collection_id(collection)
    uuid = await _resolve_uuid(canonical, product_id, odata_uuid)
    info = await get_odata().product_info(uuid)

    if info.is_offline:
        raise OfflineProduct(
            f"{info.name} is on long-term archive and cannot be downloaded directly.",
            hint=(
                "It needs a Data Workspace order first. Free-tier limits are 25 products per "
                "month and one active order at a time. Choose an online product instead if "
                "one will do."
            ),
        )
    if not info.s3_path:
        raise NotFound(
            f"CDSE reports no S3 location for {info.name}.",
            hint="Pick another product; this one is not available on the object store.",
        )

    s3 = get_s3()
    bucket, prefix = split_s3_path(info.s3_path)
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
            directory_name=info.name,
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
            f"Downloading {info.name} ({len(objects)} files, "
            f"~{human_bytes(total) or 'unknown size'}) into {settings.download_dir}.",
            f"Poll download_status with job_id={job.job_id}. Do not poll in a tight loop.",
            "The result is an unpacked .SAFE directory, not a zip archive.",
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
    ToolDef(tool_guard(download_status), READ_ONLY),
    ToolDef(tool_guard(download_cancel), DOWNLOADS),
]
