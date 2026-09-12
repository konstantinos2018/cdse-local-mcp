"""Catalogue discovery tools: find products, inspect what is inside them, list collections.

These are read-only. Nothing here downloads data; that is a separate, explicitly confirmed
step, per the interaction rules in ``CLAUDE.md``.
"""

from __future__ import annotations

from typing import Any, Literal

from cdse_local_mcp.clients.stac import StacClient, next_search_cursor
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain import collections as coll
from cdse_local_mcp.domain.geometry import resolve_area
from cdse_local_mcp.domain.models import (
    AssetSummary,
    CollectionList,
    CollectionSummary,
    ProductAssets,
    ProductSummary,
    SearchResult,
)
from cdse_local_mcp.domain.timerange import resolve_time_range
from cdse_local_mcp.errors import CloudCoverUnspecified, InvalidRequest, NotFound, tool_guard
from cdse_local_mcp.tools import READ_ONLY, ToolDef

_CLOUD_COVER_MAX = 100.0
_client: StacClient | None = None


def get_client() -> StacClient:
    """Return the shared STAC client, creating it on first use."""
    global _client
    if _client is None:
        _client = StacClient(get_settings())
    return _client


def set_client(client: StacClient | None) -> None:
    """Replace the shared client. For tests."""
    global _client
    _client = client


def _resolve_cloud_cover(collection: str, value: float | str | None) -> float | None:
    """Apply the no-silent-cloud-filtering rule.

    Returns the threshold to send upstream, or None for no filter. Raises when the caller
    left the decision unmade on a collection that has cloud cover.
    """
    supports = coll.has_cloud_cover(collection)

    if isinstance(value, str):
        if value.lower() != "any":
            raise InvalidRequest(
                f"max_cloud_cover={value!r} is not understood.",
                hint="Pass a number from 0 to 100, or the string 'any' for no filter.",
            )
        return None

    if value is None:
        if supports:
            raise CloudCoverUnspecified(
                f"{collection} carries cloud cover, and no threshold was given.",
                hint=(
                    "Ask the user what maximum cloud cover they want (a common choice is 20 "
                    "percent), then call again with max_cloud_cover=<number>. If they do not "
                    "care, pass max_cloud_cover='any'. Do not guess a threshold: filtering "
                    "silently, or not filtering silently, both produce answers that look "
                    "correct and are not."
                ),
            )
        return None

    if not 0 <= value <= _CLOUD_COVER_MAX:
        raise InvalidRequest(
            f"max_cloud_cover={value} is outside 0-100.",
            hint="Cloud cover is a percentage.",
        )
    if supports is False:
        raise InvalidRequest(
            f"{collection} does not carry cloud cover, so it cannot be filtered on.",
            hint=(
                "Drop max_cloud_cover. Sentinel-1 is radar; Sentinel-3 OLCI uses per-pixel "
                "quality flags (wqsf) instead, applied when reading the data."
            ),
        )
    return value


async def search_products(
    collection: str,
    start_date: str,
    end_date: str | None = None,
    bbox: list[float] | None = None,
    wkt: str | None = None,
    geojson: str | None = None,
    max_cloud_cover: float | Literal["any"] | None = None,
    limit: int = 10,
    cursor: str | None = None,
) -> SearchResult:
    """Find Copernicus products over an area and time range.

    This is the first step of every workflow. It does not download anything: present the
    candidates to the user, let them choose, then call a download tool with the chosen
    `product_id`. Never pick for them without asking.

    Cloud cover: for optical collections you MUST decide explicitly. If the user stated a
    threshold, pass it. If they did not, ask them before searching, then pass a number or
    `"any"`. The tool refuses rather than guessing.

    Area: pass exactly one of `bbox` (as `[west, south, east, north]` in EPSG:4326 lon/lat
    order), `wkt`, or `geojson`. For a place name, supply its approximate bounding box; the
    box is echoed back in `area_searched` so the user can check it.

    Dates are `YYYY-MM-DD` or ISO-8601 instants, UTC. A `start_date` with no `end_date`
    searches that whole day.

    Collections: `sentinel-2-l1c`, `sentinel-2-l2a`, `sentinel-3-olci-2-wfr-ntc` (water
    quality) and `sentinel-3-olci-1-efr-ntc` are fully supported; any CDSE collection id
    works. Use `list_collections` to find others.

    Results are newest first. `estimated_archive_size` is a rough per-collection figure, not
    a measurement of this product.
    """
    settings = get_settings()
    canonical = coll.canonical_collection_id(collection)
    spec = coll.spec_for(canonical)

    if limit < 1 or limit > settings.max_search_limit:
        raise InvalidRequest(
            f"limit={limit} is outside 1-{settings.max_search_limit}.",
            hint="Ask for a small page and use the returned cursor for more.",
        )

    area = resolve_area(bbox=bbox, wkt=wkt, geojson=geojson)
    time_range = resolve_time_range(start_date, end_date)
    cloud_threshold = _resolve_cloud_cover(canonical, max_cloud_cover)

    payload = await get_client().search(
        collection=canonical,
        bbox=area.as_list,
        datetime_range=time_range.as_stac(),
        limit=limit,
        max_cloud_cover=cloud_threshold,
        cursor=cursor,
    )

    features: list[dict[str, Any]] = [
        f for f in (payload.get("features") or []) if isinstance(f, dict)
    ]
    products = [ProductSummary.from_stac_item(f, canonical) for f in features]

    notes: list[str] = []
    if not products:
        notes.append(
            "No products matched. Check the bounding box is [west, south, east, north] in "
            "lon/lat order, widen the date range, or relax the cloud-cover threshold."
        )
    if spec and spec.is_swath:
        notes.append(
            f"{canonical} is swath data ({spec.file_format}): geolocation comes from a "
            "per-pixel lat/lon array, not a map projection."
        )
    if any(p.online is False for p in products):
        notes.append(
            "Some products are offline (long-term archive) and need a Data Workspace order "
            "before they can be downloaded."
        )
    if products:
        notes.append(
            "Sizes are rough per-collection estimates. Confirm the choice with the user "
            "before downloading."
        )

    return SearchResult(
        collection=canonical,
        area_searched=area.describe(),
        time_searched=time_range.describe(),
        cloud_cover_filter=(
            f"cloud cover <= {cloud_threshold}%"
            if cloud_threshold is not None
            else "none (any cloud cover)"
        ),
        returned=len(products),
        products=products,
        next_cursor=next_search_cursor(payload),
        notes=notes,
    )


async def list_product_assets(collection: str, product_id: str) -> ProductAssets:
    """List the individual files inside one product, and its full metadata.

    Use this before downloading. Products are not monolithic: a Sentinel-2 scene exposes
    each band separately (~100-180 MB) alongside the whole archive (~800 MB), and a
    Sentinel-3 OLCI Level-2 scene exposes each water-quality variable as its own NetCDF
    file. Fetching three variables instead of the whole product is the difference between
    tens of megabytes and hundreds.

    For OLCI water quality, a useful minimum is `geo-coordinates` (per-pixel latitude and
    longitude), the variables of interest, and `wqsf` (quality flags, which must be applied
    before any value is interpreted).

    Assets the catalogue declares without a usable href are reported separately in
    `unavailable_assets` and cannot be fetched.
    """
    canonical = coll.canonical_collection_id(collection)
    spec = coll.spec_for(canonical)

    item = await get_client().get_item(collection=canonical, item_id=product_id)
    raw_assets = item.get("assets")
    if not isinstance(raw_assets, dict):
        raise NotFound(
            f"{product_id} returned no assets.",
            hint="Check the product id came from a search_products result.",
        )

    archive_key = coll.find_archive_asset(raw_assets)
    assets: list[AssetSummary] = []
    unavailable: list[str] = []

    for key, value in sorted(raw_assets.items()):
        if key == archive_key or not isinstance(value, dict):
            continue
        href = value.get("href")
        if not href or not isinstance(href, str):
            unavailable.append(key)
            continue
        described = coll.describe_asset(canonical, key)
        assets.append(
            AssetSummary(
                key=key,
                name=described.name if described else None,
                description=described.description if described else None,
                media_type=value.get("type"),
                filename=href.rsplit("/", 1)[-1] if "/" in href else None,
                storage="s3" if href.startswith("s3://") else "https",
            )
        )

    archive: AssetSummary | None = None
    if archive_key:
        raw_archive = raw_assets[archive_key]
        archive = AssetSummary(
            key=archive_key,
            name="whole_product_archive",
            description="The complete product. Large: prefer selected assets where possible.",
            media_type=raw_archive.get("type"),
            filename=None,
            storage="https",
        )

    notes = list(spec.notes) if spec else []
    if unavailable:
        notes.append(
            f"{len(unavailable)} asset(s) are declared by the catalogue with no href and "
            "cannot be downloaded: " + ", ".join(unavailable)
        )

    return ProductAssets(
        product_id=product_id,
        collection=canonical,
        summary=ProductSummary.from_stac_item(item, canonical),
        geometry_kind=spec.geometry_kind.value if spec else "unknown",
        file_format=spec.file_format if spec else None,
        archive=archive,
        assets=assets,
        unavailable_assets=unavailable,
        notes=notes,
    )


async def list_collections(search: str | None = None, limit: int = 40) -> CollectionList:
    """List CDSE collections, optionally filtered by a substring of the id or title.

    CDSE publishes over 400 collections. Four have tuned support in this server and are
    flagged `first_class`; the rest are searchable but without band vocabularies or notes.

    Useful searches: `"sentinel-2"`, `"olci"`, `"dem"`, `"land cover"`.
    """
    if limit < 1:
        raise InvalidRequest(f"limit={limit} must be at least 1.")

    raw = await get_client().list_collections()
    needle = (search or "").strip().lower()

    matches: list[CollectionSummary] = []
    for entry in raw:
        cid = str(entry.get("id", ""))
        title = entry.get("title")
        if needle and needle not in cid.lower() and needle not in str(title or "").lower():
            continue
        matches.append(
            CollectionSummary(
                collection_id=cid,
                title=title if isinstance(title, str) else None,
                first_class=coll.spec_for(cid) is not None,
            )
        )

    matches.sort(key=lambda c: (not c.first_class, c.collection_id))
    notes = [f"{len(raw)} collections published by CDSE; {len(matches)} matched."]
    if len(matches) > limit:
        notes.append(f"Showing the first {limit}; refine `search` to narrow the list.")

    return CollectionList(
        returned=min(len(matches), limit),
        collections=matches[:limit],
        notes=notes,
    )


TOOLS: list[ToolDef] = [
    ToolDef(tool_guard(search_products), READ_ONLY),
    ToolDef(tool_guard(list_product_assets), READ_ONLY),
    ToolDef(tool_guard(list_collections), READ_ONLY),
]
