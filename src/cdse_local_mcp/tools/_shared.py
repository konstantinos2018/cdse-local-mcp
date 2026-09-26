"""Shared plumbing for the download tools.

Client singletons and the product-resolution steps both download tools need. Kept out of
``downloads.py`` so each tool there stays a thin adapter and the module stays under the size
ceiling.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from cdse_local_mcp.auth import TokenProvider
from cdse_local_mcp.clients.odata import ODataClient
from cdse_local_mcp.clients.s3 import S3Client, S3Object, split_s3_path
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain import collections as coll
from cdse_local_mcp.domain.models import DownloadReport, Job, ProductSummary, human_bytes
from cdse_local_mcp.errors import AuthRequired, InvalidRequest, NotFound, OfflineProduct
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


@dataclass(frozen=True)
class ProductContext:
    """Everything a download needs to know about one product."""

    name: str
    bucket: str
    prefix: str
    uuid: str
    assets: dict[str, Any]
    footprint: tuple[float, float, float, float] | None


async def _product_context(
    collection: str, product_id: str, odata_uuid: str | None
) -> ProductContext:
    """Resolve a product to its S3 location, its assets and its footprint.

    Raises if the product is archived, so no caller starts a transfer that cannot succeed.
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
    raw_assets = item.get("assets")
    raw_bbox = item.get("bbox")
    footprint = (
        (float(raw_bbox[0]), float(raw_bbox[1]), float(raw_bbox[2]), float(raw_bbox[3]))
        if isinstance(raw_bbox, list) and len(raw_bbox) == 4
        else None
    )
    return ProductContext(
        name=info.name,
        bucket=bucket,
        prefix=prefix,
        uuid=uuid,
        assets=raw_assets if isinstance(raw_assets, dict) else {},
        footprint=footprint,
    )


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


@dataclass(frozen=True)
class SelectedAssets:
    """Requested assets resolved to concrete objects in the bucket."""

    objects: list[tuple[str, S3Object]]
    whole_product_bytes: int

    @property
    def total_bytes(self) -> int:
        return sum(obj.size for _, obj in self.objects)

    @property
    def keys(self) -> list[str]:
        return [key for key, _ in self.objects]


async def _select_assets(
    *,
    collection: str,
    product_id: str,
    requested: list[str],
    item_assets: dict[str, Any],
    bucket: str,
    prefix: str,
    accept: Callable[[dict[str, Any]], bool] | None = None,
    kind: str = "downloadable",
) -> SelectedAssets:
    """Resolve asset names, friendly or raw, to S3 objects with exact sizes.

    ``accept`` narrows which assets qualify - the window tool admits rasters only - and the
    error for an unknown name lists exactly the qualifying ones. One bucket listing supplies
    every size, so this costs a single request however many assets are named.
    """
    if not requested:
        raise InvalidRequest(
            "No assets were requested.",
            hint="Call list_product_assets to see what this product offers, then name some.",
        )

    available = [
        key
        for key, value in item_assets.items()
        if _s3_href(value) and (accept is None or accept(value))
    ]
    wanted: dict[str, str] = {}
    for request in requested:
        key = coll.match_asset_key(collection, request, available)
        if key is None:
            raise InvalidRequest(
                f"{product_id} has no {kind} asset matching {request!r}.",
                hint=(
                    "Available: " + ", ".join(sorted(available)[:40]) + ". Friendly names from "
                    "list_product_assets also work."
                ),
            )
        wanted[key] = str(_s3_href(item_assets[key]))

    sizes = {obj.key: obj.size for obj in await get_s3().list_objects(bucket, prefix)}
    objects: list[tuple[str, S3Object]] = []
    for key, href in wanted.items():
        _, object_key = split_s3_path(href)
        if object_key not in sizes:
            raise NotFound(
                f"The catalogue lists {key} but the object store does not hold it.",
                hint="Try another product, or download the whole product instead.",
            )
        objects.append((key, S3Object(key=object_key, size=sizes[object_key])))

    return SelectedAssets(objects=objects, whole_product_bytes=sum(sizes.values()))
