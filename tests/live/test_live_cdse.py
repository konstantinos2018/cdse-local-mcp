"""End-to-end tests against the real CDSE API.

Deselected by default (`-m 'not live'`). Run with `uv run pytest -m live`.

Catalogue search is unauthenticated, so these need no credentials, but they do spend
catalogue request quota. Keep the number of requests small.

Their purpose is to catch upstream drift: collection ids changing, the pagination shape
changing, assets disappearing. Mocked tests cannot see any of that.
"""

from __future__ import annotations

import pytest

from cdse_local_mcp.tools import discovery

pytestmark = pytest.mark.live

GULF_OF_PATRAS = [21.3, 38.1, 21.9, 38.4]


async def test_sentinel2_search_over_the_gulf_of_patras() -> None:
    result = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date="2024-07-01",
        end_date="2024-07-31",
        bbox=GULF_OF_PATRAS,
        max_cloud_cover=20,
        limit=5,
    )

    assert result.returned > 0
    for product in result.products:
        assert product.cloud_cover is not None
        assert product.cloud_cover <= 20
        assert product.odata_uuid is not None
        assert product.tile is not None


async def test_sentinel2_assets_still_expose_individual_bands() -> None:
    found = await discovery.search_products(
        collection="sentinel-2-l1c",
        start_date="2024-07-01",
        end_date="2024-07-31",
        bbox=GULF_OF_PATRAS,
        max_cloud_cover="any",
        limit=1,
    )
    product = found.products[0]

    assets = await discovery.list_product_assets(
        collection="sentinel-2-l1c", product_id=product.product_id
    )

    keys = {a.key for a in assets.assets}
    assert {"B02", "B04", "B08", "TCI"} <= keys
    assert assets.archive is not None
    assert all(a.storage == "s3" for a in assets.assets if a.key.startswith("B"))


async def test_olci_water_quality_variables_are_still_present() -> None:
    found = await discovery.search_products(
        collection="sentinel-3-olci-2-wfr-ntc",
        start_date="2024-07-01",
        end_date="2024-07-31",
        bbox=GULF_OF_PATRAS,
        limit=1,
    )
    assert found.returned > 0

    assets = await discovery.list_product_assets(
        collection="sentinel-3-olci-2-wfr-ntc", product_id=found.products[0].product_id
    )

    keys = {a.key for a in assets.assets}
    assert {"chl-Nn", "tsm-Nn", "geo-coordinates", "wqsf"} <= keys
    assert assets.geometry_kind == "swath"


async def test_sentinel_collections_are_reachable_past_the_first_page() -> None:
    """Guards the trap that every Sentinel collection sorts after 200 CLMS ones."""
    result = await discovery.list_collections(search="sentinel-2", limit=20)
    ids = {c.collection_id for c in result.collections}
    assert {"sentinel-2-l1c", "sentinel-2-l2a"} <= ids
