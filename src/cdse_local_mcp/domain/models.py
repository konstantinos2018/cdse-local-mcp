"""Tool input and output models.

These are what the model sees, so they are projections: the fields that support a decision,
not the raw STAC item. A single Sentinel-2 item is several KB of mostly irrelevant metadata.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from cdse_local_mcp.domain import collections as coll

_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def human_bytes(size: int | None) -> str | None:
    """Render a byte count the way a person would read it."""
    if size is None:
        return None
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return None  # pragma: no cover - loop always returns


class ProductSummary(BaseModel):
    """One candidate product, with what is needed to choose between them."""

    product_id: str = Field(description="STAC item id; pass this to download tools")
    collection: str
    datetime: str | None = Field(description="Acquisition time, UTC ISO-8601")
    cloud_cover: float | None = Field(
        default=None, description="Percent cloud cover, or null where the collection has none"
    )
    tile: str | None = Field(default=None, description="Grid code, e.g. MGRS tile for Sentinel-2")
    platform: str | None = None
    online: bool | None = Field(
        default=None,
        description="False means the product is on long-term archive and needs an order",
    )
    odata_uuid: str | None = Field(
        default=None, description="OData UUID, taken from the archive asset href"
    )
    estimated_archive_size: str | None = Field(
        default=None,
        description=(
            "Rough typical size for this collection, NOT measured for this product. "
            "The exact size is read from CDSE when a download is requested."
        ),
    )

    @classmethod
    def from_stac_item(cls, item: dict[str, Any], collection: str) -> ProductSummary:
        props: dict[str, Any] = item.get("properties") or {}
        assets: dict[str, Any] = item.get("assets") or {}
        spec = coll.spec_for(collection)

        odata_uuid: str | None = None
        archive_key = coll.find_archive_asset(assets)
        if archive_key:
            href = assets[archive_key].get("href") or ""
            match = _UUID_RE.search(str(href))
            odata_uuid = match.group(0) if match else None

        online = props.get("online")
        if online is None:
            online = (props.get("_private") or {}).get("online") if props.get("_private") else None

        return cls(
            product_id=str(item.get("id", "")),
            collection=str(item.get("collection") or collection),
            datetime=props.get("datetime") or props.get("start_datetime"),
            cloud_cover=props.get("eo:cloud_cover"),
            tile=props.get("grid:code"),
            platform=props.get("platform"),
            online=online if isinstance(online, bool) else None,
            odata_uuid=odata_uuid,
            estimated_archive_size=human_bytes(spec.typical_archive_bytes) if spec else None,
        )


class SearchResult(BaseModel):
    """Candidates for the user to choose from, plus exactly what was searched."""

    collection: str
    area_searched: str = Field(description="The bounding box actually used, so it can be checked")
    time_searched: str
    cloud_cover_filter: str = Field(description="The filter applied, or 'none (any cloud cover)'")
    returned: int
    products: list[ProductSummary]
    next_cursor: str | None = Field(
        default=None, description="Pass back as `cursor` for the next page; null when exhausted"
    )
    notes: list[str] = Field(default_factory=list)


class AssetSummary(BaseModel):
    """One downloadable file within a product."""

    key: str = Field(description="STAC asset key; pass this to download tools")
    name: str | None = Field(default=None, description="Friendly name, where known")
    description: str | None = None
    media_type: str | None = None
    filename: str | None = None
    storage: str = Field(description="'s3' or 'https'")


class ProductAssets(BaseModel):
    """The files inside one product, which is what selective download operates on."""

    product_id: str
    collection: str
    summary: ProductSummary
    geometry_kind: str = Field(
        description="'gridded' (has a CRS and transform) or 'swath' (per-pixel lat/lon)"
    )
    file_format: str | None = None
    archive: AssetSummary | None = Field(
        default=None, description="The whole-product archive, if offered"
    )
    assets: list[AssetSummary]
    unavailable_assets: list[str] = Field(
        default_factory=list,
        description="Asset keys the catalogue declares with no href; they cannot be fetched",
    )
    notes: list[str] = Field(default_factory=list)


class CollectionSummary(BaseModel):
    """One CDSE collection."""

    collection_id: str
    title: str | None = None
    first_class: bool = Field(
        description="True when this server has tuned support: vocabularies, notes, fixtures"
    )


class CollectionList(BaseModel):
    """Collections matching a query."""

    returned: int
    collections: list[CollectionSummary]
    notes: list[str] = Field(default_factory=list)
