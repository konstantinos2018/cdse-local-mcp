"""Shared plumbing for the download tools.

Client singletons and the product-resolution steps both download tools need. Kept out of
``downloads.py`` so each tool there stays a thin adapter and the module stays under the size
ceiling.
"""

from __future__ import annotations

import logging
from typing import Any

from cdse_local_mcp.auth import TokenProvider
from cdse_local_mcp.clients.odata import ODataClient
from cdse_local_mcp.clients.s3 import S3Client, split_s3_path
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain.models import DownloadReport, Job, ProductSummary, human_bytes
from cdse_local_mcp.errors import AuthRequired, NotFound, OfflineProduct
from cdse_local_mcp.tools import discovery
from cdse_local_mcp.transfer.budget import TransferBudget, get_budget
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


async def _product_context(
    collection: str, product_id: str, odata_uuid: str | None
) -> tuple[str, str, str, str, dict[str, Any]]:
    """Resolve everything a download needs: name, bucket, prefix, and the item's assets.

    Returns ``(name, bucket, prefix, uuid, assets)``. Raises if the product is archived, so
    no caller starts a transfer that cannot succeed.
    """
    item = await discovery.get_client().get_item(collection=collection, item_id=product_id)
    summary = ProductSummary.from_stac_item(item, collection)
    uuid = odata_uuid or summary.odata_uuid
    if not uuid:
        raise NotFound(
            f"{product_id} does not expose a downloadable archive.",
            hint="Check the product id, or use list_product_assets to see what is offered.",
        )

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

    bucket, prefix = split_s3_path(info.s3_path)
    assets = item.get("assets") if isinstance(item.get("assets"), dict) else {}
    return info.name, bucket, prefix, uuid, assets or {}


def _require_s3() -> None:
    """Fail before any work when the download credentials are missing."""
    if not get_settings().has_s3:
        raise AuthRequired(
            "Downloading requires CDSE S3 access keys, which are not configured.",
            hint=(
                "Create keys at https://eodata-s3keysmanager.dataspace.copernicus.eu/ and set "
                "CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY, then restart the server. Search "
                "and list_product_assets keep working without them."
            ),
        )


def _s3_href(asset: Any) -> str | None:
    """The asset's S3 href, or None when it has none or is served over https."""
    if not isinstance(asset, dict):
        return None
    href = asset.get("href")
    return href if isinstance(href, str) and href.startswith("s3://") else None
