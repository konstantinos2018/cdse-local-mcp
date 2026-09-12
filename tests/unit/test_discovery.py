"""Discovery tools, against mocked CDSE responses."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx
from fastmcp.exceptions import ToolError

from cdse_local_mcp.errors import (
    CloudCoverUnspecified,
    InvalidRequest,
    QuotaExceeded,
    UpstreamError,
)
from cdse_local_mcp.tools import discovery
from tests.conftest import STAC, load_fixture

GULF_OF_PATRAS = [21.3, 38.1, 21.9, 38.4]
JULY = ("2024-07-01", "2024-07-31")


def _search_route(payload: dict[str, Any] | None = None) -> respx.Route:
    return respx.post(f"{STAC}/search").mock(
        return_value=httpx.Response(200, json=payload or load_fixture("s2_search.json"))
    )


# --- the cloud-cover rule --------------------------------------------------------------


async def test_optical_search_without_a_cloud_decision_refuses_rather_than_guessing() -> None:
    with pytest.raises(CloudCoverUnspecified) as exc:
        await discovery.search_products(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            end_date=JULY[1],
            bbox=GULF_OF_PATRAS,
        )
    hint = exc.value.hint or ""
    assert "Ask the user" in hint
    assert "'any'" in hint


@respx.mock
async def test_explicit_any_searches_without_a_cloud_filter() -> None:
    route = _search_route()
    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date=JULY[0],
        end_date=JULY[1],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
    )
    body = route.calls.last.request.content.decode()
    assert "eo:cloud_cover" not in body
    assert result.cloud_cover_filter == "none (any cloud cover)"


@respx.mock
async def test_a_threshold_is_passed_upstream_and_reported_back() -> None:
    route = _search_route()
    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date=JULY[0],
        end_date=JULY[1],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover=20,
    )
    import json

    body = json.loads(route.calls.last.request.content)
    assert body["query"] == {"eo:cloud_cover": {"lte": 20}}
    assert "20" in result.cloud_cover_filter


async def test_cloud_cover_on_a_collection_without_it_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="does not carry cloud cover"):
        await discovery.search_products(
            collection="sentinel-3-olci-2-wfr-ntc",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover=20,
        )


async def test_a_nonsense_cloud_value_is_rejected() -> None:
    with pytest.raises(InvalidRequest):
        await discovery.search_products(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover="low",  # type: ignore[arg-type]
        )


async def test_out_of_range_cloud_cover_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="0-100"):
        await discovery.search_products(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover=140,
        )


# --- search results --------------------------------------------------------------------


@respx.mock
async def test_search_projects_items_and_extracts_the_odata_uuid() -> None:
    _search_route()
    result = await discovery.search_products(
        collection="s2",
        start_date=JULY[0],
        end_date=JULY[1],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
    )

    assert result.collection == "sentinel-2-l1c"  # shorthand resolved
    assert result.returned == 2
    first = result.products[0]
    assert first.product_id.startswith("S2")
    assert first.cloud_cover is not None
    assert first.tile is not None
    # The Product asset href carries the OData UUID, so no second lookup is needed.
    assert first.odata_uuid is not None
    assert len(first.odata_uuid) == 36
    assert first.estimated_archive_size is not None


@respx.mock
async def test_search_returns_the_pagination_token_from_the_post_body() -> None:
    _search_route()
    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date=JULY[0],
        end_date=JULY[1],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
    )
    assert result.next_cursor is not None
    assert result.next_cursor.startswith("next:sentinel-2-l1c:")


@respx.mock
async def test_a_cursor_is_sent_back_as_the_token() -> None:
    route = _search_route()
    await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date=JULY[0],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
        cursor="next:sentinel-2-l1c:SOMEITEM",
    )
    import json

    assert json.loads(route.calls.last.request.content)["token"] == "next:sentinel-2-l1c:SOMEITEM"


@respx.mock
async def test_search_echoes_the_area_and_time_actually_used() -> None:
    _search_route()
    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date="2024-07-31",
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
    )
    assert "21.3000,38.1000,21.9000,38.4000" in result.area_searched
    assert "2024-07-31T00:00:00" in result.time_searched
    assert "23:59:59" in result.time_searched


@respx.mock
async def test_no_results_is_not_an_error_but_says_what_to_try() -> None:
    _search_route({"type": "FeatureCollection", "features": [], "links": []})
    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date=JULY[0],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover=0,
    )
    assert result.returned == 0
    assert any("lon/lat order" in n for n in result.notes)


async def test_an_absurd_limit_is_rejected() -> None:
    with pytest.raises(InvalidRequest, match="limit"):
        await discovery.search_products(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover="any",
            limit=5000,
        )


# --- assets ------------------------------------------------------------------------------


@respx.mock
async def test_assets_drop_null_hrefs_and_report_them_separately() -> None:
    item = load_fixture("olci_l2_item.json")
    respx.get(url__regex=rf"{STAC}/collections/.+/items/.+").mock(
        return_value=httpx.Response(200, json=item)
    )

    result = await discovery.list_product_assets(
        collection="sentinel-3-olci-2-wfr-ntc", product_id=item["id"]
    )

    assert set(result.unavailable_assets) == {"chlor-a", "fluo", "iop-Lsd"}
    assert all(a.key not in result.unavailable_assets for a in result.assets)
    assert any("cannot be downloaded" in n for n in result.notes)


@respx.mock
async def test_lowercase_archive_key_is_still_found() -> None:
    item = load_fixture("olci_l2_item.json")
    respx.get(url__regex=rf"{STAC}/collections/.+/items/.+").mock(
        return_value=httpx.Response(200, json=item)
    )

    result = await discovery.list_product_assets(
        collection="sentinel-3-olci-2-wfr-ntc", product_id=item["id"]
    )

    assert result.archive is not None
    assert result.archive.key == "product"
    # and it is not duplicated among the data assets
    assert all(a.key != "product" for a in result.assets)


@respx.mock
async def test_swath_products_are_flagged_and_variables_named() -> None:
    item = load_fixture("olci_l2_item.json")
    respx.get(url__regex=rf"{STAC}/collections/.+/items/.+").mock(
        return_value=httpx.Response(200, json=item)
    )

    result = await discovery.list_product_assets(collection="olci-water", product_id=item["id"])

    assert result.geometry_kind == "swath"
    assert any("per-pixel" in n for n in result.notes)
    names = {a.key: a.name for a in result.assets}
    assert names["chl-Nn"] == "chlorophyll_nn"
    assert names["geo-coordinates"] == "geolocation"
    storage = {a.key: a.storage for a in result.assets}
    assert storage["chl-Nn"] == "s3"


# --- collections -------------------------------------------------------------------------


@respx.mock
async def test_collections_pagination_reaches_sentinel_on_the_second_page() -> None:
    """The regression guard for the trap that Sentinel sorts after 200 CLMS collections."""
    page1 = load_fixture("collections_page1.json")
    page2 = load_fixture("collections_page2.json")

    def handler(request: httpx.Request) -> httpx.Response:
        payload = page2 if "offset=200" in str(request.url) else page1
        return httpx.Response(200, json=payload)

    respx.route(method="GET", url__startswith=f"{STAC}/collections").mock(side_effect=handler)

    result = await discovery.list_collections(search="sentinel-2")
    ids = [c.collection_id for c in result.collections]
    assert "sentinel-2-l1c" in ids
    assert all(c.first_class for c in result.collections)


@respx.mock
async def test_collections_marks_first_class_and_sorts_them_first() -> None:
    page1 = load_fixture("collections_page1.json")
    page2 = load_fixture("collections_page2.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=page2 if "offset=200" in str(request.url) else page1)

    respx.route(method="GET", url__startswith=f"{STAC}/collections").mock(side_effect=handler)

    result = await discovery.list_collections()
    assert result.collections[0].first_class is True
    assert any(not c.first_class for c in result.collections)


# --- error surfacing ----------------------------------------------------------------------


@respx.mock
async def test_upstream_failure_becomes_an_actionable_tool_error() -> None:
    respx.post(f"{STAC}/search").mock(return_value=httpx.Response(500, text="boom"))
    guarded = next(t.fn for t in discovery.TOOLS if t.fn.__name__ == "search_products")

    with pytest.raises(ToolError) as exc:
        await guarded(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover="any",
        )
    message = str(exc.value)
    assert "upstream_error" in message
    assert "What to do" in message


@respx.mock
async def test_a_missing_product_is_reported_as_not_found() -> None:
    respx.get(url__regex=rf"{STAC}/collections/.+/items/.+").mock(
        return_value=httpx.Response(404, text="")
    )
    guarded = next(t.fn for t in discovery.TOOLS if t.fn.__name__ == "list_product_assets")

    with pytest.raises(ToolError, match="not_found"):
        await guarded(collection="sentinel-2-l1c", product_id="NOPE")


# --- retry -------------------------------------------------------------------------------


@respx.mock
async def test_a_rate_limit_is_retried_and_then_succeeds() -> None:
    responses = [
        httpx.Response(429, headers={"Retry-After": "0"}),
        httpx.Response(200, json=load_fixture("s2_search.json")),
    ]
    respx.post(f"{STAC}/search").mock(side_effect=responses)

    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date=JULY[0],
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
    )
    assert result.returned == 2


@respx.mock
async def test_persistent_rate_limiting_gives_up_with_advice() -> None:
    respx.post(f"{STAC}/search").mock(
        return_value=httpx.Response(429, headers={"Retry-After": "0"})
    )

    with pytest.raises(QuotaExceeded) as exc:
        await discovery.search_products(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover="any",
        )
    assert "Wait a minute" in (exc.value.hint or "")


@respx.mock
async def test_a_bad_request_is_not_retried() -> None:
    """Retrying a 400 just spends quota; it will fail identically every time."""
    route = respx.post(f"{STAC}/search").mock(return_value=httpx.Response(400, text="bad"))

    with pytest.raises(UpstreamError):
        await discovery.search_products(
            collection="sentinel-2-l1c",
            start_date=JULY[0],
            bbox=GULF_OF_PATRAS,
            max_cloud_cover="any",
        )
    assert route.call_count == 1
