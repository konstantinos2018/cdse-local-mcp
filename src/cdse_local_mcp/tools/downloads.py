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
from cdse_local_mcp.transfer.download import download_stream
from cdse_local_mcp.transfer.jobs import JobRegistry, get_registry
from cdse_local_mcp.transfer.paths import resolve_target

logger = logging.getLogger(__name__)

_odata: ODataClient | None = None
_tokens: TokenProvider | None = None


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


async def download_product_archive(
    collection: str,
    product_id: str,
    odata_uuid: str | None = None,
    confirm: bool = False,
) -> DownloadReport:
    """Download a complete product archive to local disk, as a background job.

    This is the expensive option: a whole Sentinel-2 `.SAFE` archive is around 800 MB, while
    a single band is ~150 MB and a quicklook is ~100 KB. Prefer `list_product_assets` and
    fetch only what is needed, unless the user genuinely wants the full product.

    Call this only with a `product_id` the user has chosen from a `search_products` result,
    after telling them the estimated size. If the product is larger than the per-call cap the
    tool refuses: report the size to the user and call again with `confirm=True` only if they
    agree.

    Returns immediately with a `job_id`. Poll `download_status` for progress; the file path
    appears when the job completes. The file is never returned through this channel.

    Passing `odata_uuid` from the search result saves a catalogue lookup.
    """
    settings = get_settings()
    # Check credentials before doing anything else. Product metadata is readable without
    # them, so without this the tool would happily queue a job that cannot possibly run.
    if not settings.has_oauth:
        raise AuthRequired(
            "Downloading requires CDSE OAuth credentials, which are not configured.",
            hint=(
                "Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET from an OAuth client in the "
                "Copernicus Data Space dashboard, then restart the server. Catalogue search "
                "and list_product_assets keep working without them."
            ),
        )

    canonical = coll.canonical_collection_id(collection)
    uuid = await _resolve_uuid(canonical, product_id, odata_uuid)

    odata = get_odata()
    info = await odata.product_info(uuid)

    if info.is_offline:
        raise OfflineProduct(
            f"{info.name} is on long-term archive and cannot be downloaded directly.",
            hint=(
                "It needs a Data Workspace order first. Free-tier limits are 25 products per "
                "month and one active order at a time. Choose an online product instead if "
                "one will do."
            ),
        )

    estimated = info.content_length or 0
    _budget().check(estimated, confirmed=confirm)

    filename = info.name if info.name.lower().endswith(".zip") else f"{info.name}.zip"
    target = resolve_target(settings.download_dir, filename)

    registry = _registry()
    job = registry.create(
        product_id=product_id, collection=canonical, bytes_total=info.content_length
    )

    async def runner(current: Job) -> None:
        async def on_progress(done: int, total: int | None) -> None:
            registry.progress(current, done, total)

        result = await download_stream(
            open_stream=lambda start: odata.stream_archive(uuid, resume_from=start),
            target=target,
            budget=_budget(),
            expected_size=info.content_length,
            checksum=info.checksum_value,
            checksum_algorithm=info.checksum_algorithm,
            progress=on_progress,
        )
        registry.finish(
            current,
            path=result.path,
            cached=result.cached,
            verified=result.checksum_verified,
        )

    registry.start(job, runner)

    notes = [
        f"Downloading {info.name} (~{human_bytes(info.content_length) or 'unknown size'}) "
        f"to {target}.",
        f"Poll download_status with job_id={job.job_id}. Do not poll in a tight loop.",
    ]
    if not info.checksum_value:
        notes.append("CDSE published no checksum for this product; only its length is verified.")

    return _report([job], notes)


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
    ToolDef(tool_guard(download_product_archive), DOWNLOADS),
    ToolDef(tool_guard(download_status), READ_ONLY),
    ToolDef(tool_guard(download_cancel), DOWNLOADS),
]
