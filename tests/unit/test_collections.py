"""The collection registry, and the live-API traps it exists to absorb."""

from __future__ import annotations

import pytest

from cdse_local_mcp.domain import collections as coll


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("sentinel-2-l1c", "sentinel-2-l1c"),
        ("SENTINEL-2-L1C", "sentinel-2-l1c"),
        ("s2", "sentinel-2-l1c"),
        ("olci-water", "sentinel-3-olci-2-wfr-ntc"),
        (" S2-L2A ", "sentinel-2-l2a"),
    ],
)
def test_shorthands_resolve_to_canonical_ids(given: str, expected: str) -> None:
    assert coll.canonical_collection_id(given) == expected


def test_unknown_collections_pass_through_untouched() -> None:
    assert coll.canonical_collection_id("clms_ndvi_global_300m_10daily_v3_cog") == (
        "clms_ndvi_global_300m_10daily_v3_cog"
    )
    assert coll.spec_for("clms_ndvi_global_300m_10daily_v3_cog") is None


def test_timeliness_variants_share_the_ntc_spec() -> None:
    nrt = coll.spec_for("sentinel-3-olci-2-wfr-nrt")
    assert nrt is not None
    assert nrt.is_swath


def test_cloud_cover_support_distinguishes_unknown_from_absent() -> None:
    assert coll.has_cloud_cover("sentinel-2-l1c") is True
    # Level-2 Water records scene cloud cover; Level-1B does not. Both verified live.
    assert coll.has_cloud_cover("sentinel-3-olci-2-wfr-ntc") is True
    assert coll.has_cloud_cover("sentinel-3-olci-1-efr-ntc") is False
    # Unknown is not the same as absent: the caller must be able to tell.
    assert coll.has_cloud_cover("some-collection-we-have-never-seen") is None


def test_archive_asset_lookup_is_case_insensitive() -> None:
    """Sentinel-2 spells it Product, Sentinel-3 spells it product."""
    assert coll.find_archive_asset({"Product": {}, "B04": {}}) == "Product"
    assert coll.find_archive_asset({"product": {}, "chl-Nn": {}}) == "product"
    assert coll.find_archive_asset({"B04": {}}) is None


def test_olci_water_assets_get_friendly_names_and_usage_guidance() -> None:
    chl_nn = coll.describe_asset("sentinel-3-olci-2-wfr-ntc", "chl-Nn")
    assert chl_nn is not None
    assert chl_nn.name == "chlorophyll_nn"
    assert "coastal" in chl_nn.description

    oc4me = coll.describe_asset("sentinel-3-olci-2-wfr-ntc", "chl-Oc4me")
    assert oc4me is not None
    assert "open ocean" in oc4me.description


def test_olci_reflectance_keys_resolve_despite_the_data_suffix() -> None:
    described = coll.describe_asset("sentinel-3-olci-1-efr-ntc", "geo-coordinates")
    assert described is not None
    assert described.name == "geolocation"


def test_sentinel2_band_names_carry_their_resolution() -> None:
    band = coll.describe_asset("sentinel-2-l1c", "B04")
    assert band is not None
    assert band.name == "red_10m"


def test_l2a_drops_the_cirrus_band() -> None:
    l1c = coll.spec_for("sentinel-2-l1c")
    l2a = coll.spec_for("sentinel-2-l2a")
    assert l1c is not None and l2a is not None
    assert "B10" in l1c.data_assets
    assert "B10" not in l2a.data_assets
