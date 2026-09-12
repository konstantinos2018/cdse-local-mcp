"""Registry of first-class collections and their vocabularies.

Every CDSE collection is searchable through the generic path. The specs here add tuned
knowledge for the four we support properly. Details and provenance: ``docs/collections.md``.

Two traps are encoded here rather than left to each caller:

* the archive asset key is ``Product`` for Sentinel-2 but ``product`` for Sentinel-3, so
  asset lookup is case-insensitive;
* asset keys are not friendly names (``chl-Nn``, ``Oa01_reflectanceData``), so they are
  mapped to plain ones.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

MB = 1024 * 1024


class GeometryKind(StrEnum):
    """How a product's pixels relate to the ground.

    ``GRIDDED`` products have a CRS and an affine transform. ``SWATH`` products carry a
    per-pixel lat/lon array instead; gridded-raster code applied to them fails silently.
    """

    GRIDDED = "gridded"
    SWATH = "swath"


@dataclass(frozen=True)
class AssetSpec:
    """A friendly name and description for one STAC asset key."""

    name: str
    description: str


@dataclass(frozen=True)
class CollectionSpec:
    """Tuned knowledge for one first-class collection."""

    id: str
    title: str
    geometry_kind: GeometryKind
    has_cloud_cover: bool
    file_format: str
    typical_archive_bytes: int | None
    data_assets: tuple[str, ...] = ()
    asset_names: dict[str, AssetSpec] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def is_swath(self) -> bool:
        return self.geometry_kind is GeometryKind.SWATH


_S2_BANDS = (
    "B01",
    "B02",
    "B03",
    "B04",
    "B05",
    "B06",
    "B07",
    "B08",
    "B8A",
    "B09",
    "B10",
    "B11",
    "B12",
    "TCI",
)

_S2_BAND_NAMES: dict[str, AssetSpec] = {
    "B01": AssetSpec("coastal_aerosol_60m", "Band 1, 443 nm, 60 m"),
    "B02": AssetSpec("blue_10m", "Band 2, 490 nm, 10 m"),
    "B03": AssetSpec("green_10m", "Band 3, 560 nm, 10 m"),
    "B04": AssetSpec("red_10m", "Band 4, 665 nm, 10 m"),
    "B05": AssetSpec("red_edge_1_20m", "Band 5, 705 nm, 20 m"),
    "B06": AssetSpec("red_edge_2_20m", "Band 6, 740 nm, 20 m"),
    "B07": AssetSpec("red_edge_3_20m", "Band 7, 783 nm, 20 m"),
    "B08": AssetSpec("nir_10m", "Band 8, 842 nm, 10 m"),
    "B8A": AssetSpec("narrow_nir_20m", "Band 8A, 865 nm, 20 m"),
    "B09": AssetSpec("water_vapour_60m", "Band 9, 945 nm, 60 m"),
    "B10": AssetSpec("cirrus_60m", "Band 10, 1375 nm, 60 m; L1C only"),
    "B11": AssetSpec("swir_1_20m", "Band 11, 1610 nm, 20 m"),
    "B12": AssetSpec("swir_2_20m", "Band 12, 2190 nm, 20 m"),
    "TCI": AssetSpec("true_colour_10m", "True-colour composite, 10 m"),
}

_OLCI_SHARED_NAMES: dict[str, AssetSpec] = {
    "geo-coordinates": AssetSpec(
        "geolocation",
        "Per-pixel latitude and longitude. Required for any spatial use of a swath product.",
    ),
    "time-coordinates": AssetSpec("acquisition_time", "Per-scanline acquisition time"),
    "tie-geo-coordinates": AssetSpec("tie_point_geolocation", "Coarse tie-point lat/lon grid"),
    "tie-geometries": AssetSpec("tie_point_geometries", "Sun and view angles on the tie grid"),
    "tie-meteo": AssetSpec("tie_point_meteo", "Meteorological fields on the tie grid"),
    "instrument-data": AssetSpec("instrument_data", "Detector and spectral calibration data"),
    "xfdumanifest": AssetSpec("manifest", "SAFE/SEN3 product manifest"),
}

_OLCI_L2_WATER_NAMES: dict[str, AssetSpec] = {
    **_OLCI_SHARED_NAMES,
    "chl-Nn": AssetSpec(
        "chlorophyll_nn",
        "Chlorophyll-a from the neural-net algorithm. Use this for coastal and turbid "
        "water. Check the units attribute: values are documented as log10-scaled.",
    ),
    "chl-Oc4me": AssetSpec(
        "chlorophyll_oc4me",
        "Chlorophyll-a from the OC4Me algorithm. Valid for clear open ocean only and "
        "unreliable in coastal water. Check the units attribute for log10 scaling.",
    ),
    "tsm-Nn": AssetSpec(
        "suspended_matter_nn",
        "Total suspended matter from the neural net. Check units for log10 scaling.",
    ),
    "trsp": AssetSpec(
        "transparency_kd490",
        "Diffuse attenuation coefficient at 490 nm (KD490), i.e. water transparency.",
    ),
    "iop-Nn": AssetSpec(
        "detrital_absorption_adg443",
        "Absorption by coloured detrital and dissolved material at 443 nm (ADG443).",
    ),
    "w-Aer": AssetSpec("aerosol", "Aerosol load over water (A865, T865)"),
    "par": AssetSpec("par", "Photosynthetically active radiation, 400-700 nm"),
    "iwv": AssetSpec("integrated_water_vapour", "Integrated water vapour column"),
    "wqsf": AssetSpec(
        "quality_flags",
        "Water Quality and Science Flags. Must be applied before interpreting any value: "
        "unmasked retrievals over land or cloud are meaningless.",
    ),
}

_OLCI_L1_NAMES: dict[str, AssetSpec] = {
    **_OLCI_SHARED_NAMES,
    "quality-flags": AssetSpec("quality_flags", "Per-pixel quality flags"),
    "removed-pixels": AssetSpec("removed_pixels", "Pixels removed during processing"),
}

_WATER_QUALITY_ASSETS = (
    "geo-coordinates",
    "chl-Nn",
    "chl-Oc4me",
    "tsm-Nn",
    "trsp",
    "iop-Nn",
    "par",
    "wqsf",
)

FIRST_CLASS: dict[str, CollectionSpec] = {
    "sentinel-2-l1c": CollectionSpec(
        id="sentinel-2-l1c",
        title="Sentinel-2 MSI Level-1C top-of-atmosphere reflectance",
        geometry_kind=GeometryKind.GRIDDED,
        has_cloud_cover=True,
        file_format="JPEG 2000 in a .SAFE archive",
        typical_archive_bytes=800 * MB,
        data_assets=_S2_BANDS,
        asset_names=_S2_BAND_NAMES,
        notes=(
            "Granules are in a UTM CRS, not EPSG:4326; a lon/lat bbox must be reprojected "
            "before any windowed read.",
            "Band resolutions differ: 10 m (B02, B03, B04, B08), 20 m (B05-B07, B8A, B11, "
            "B12), 60 m (B01, B09, B10).",
        ),
    ),
    "sentinel-2-l2a": CollectionSpec(
        id="sentinel-2-l2a",
        title="Sentinel-2 MSI Level-2A surface reflectance",
        geometry_kind=GeometryKind.GRIDDED,
        has_cloud_cover=True,
        file_format="JPEG 2000 in a .SAFE archive",
        typical_archive_bytes=1000 * MB,
        data_assets=tuple(b for b in _S2_BANDS if b != "B10"),
        asset_names=_S2_BAND_NAMES,
        notes=(
            "Granules are in a UTM CRS, not EPSG:4326.",
            "L2A nests imagery under IMG_DATA/R10m, R20m and R60m, unlike L1C. Always "
            "resolve paths from STAC asset hrefs rather than building them.",
            "B10 is absent from L2A; a scene classification layer (SCL) is added.",
        ),
    ),
    "sentinel-3-olci-2-wfr-ntc": CollectionSpec(
        id="sentinel-3-olci-2-wfr-ntc",
        title="Sentinel-3 OLCI Level-2 Water, full resolution, non-time-critical",
        geometry_kind=GeometryKind.SWATH,
        has_cloud_cover=False,
        file_format="NetCDF4 files in a .SEN3 directory",
        typical_archive_bytes=250 * MB,
        data_assets=_WATER_QUALITY_ASSETS,
        asset_names=_OLCI_L2_WATER_NAMES,
        notes=(
            "Swath product: there is no CRS or affine transform. Geolocation comes from the "
            "per-pixel latitude/longitude arrays in geo_coordinates.nc.",
            "300 m resolution. The wrr variants are ~1.2 km reduced resolution.",
            "Apply the wqsf quality flags before interpreting any retrieval.",
            "Read scale_factor, add_offset, units and _FillValue from each variable rather "
            "than assuming: chlorophyll and suspended matter are documented as log10-scaled.",
        ),
    ),
    "sentinel-3-olci-1-efr-ntc": CollectionSpec(
        id="sentinel-3-olci-1-efr-ntc",
        title="Sentinel-3 OLCI Level-1B, full resolution, non-time-critical",
        geometry_kind=GeometryKind.SWATH,
        has_cloud_cover=False,
        file_format="NetCDF4 files in a .SEN3 directory",
        typical_archive_bytes=650 * MB,
        data_assets=(
            *(f"Oa{i:02d}_radianceData" for i in range(1, 22)),
            "geo-coordinates",
            "quality-flags",
        ),
        asset_names=_OLCI_L1_NAMES,
        notes=(
            "Swath product: geolocation comes from geo_coordinates.nc, not a transform.",
            "Carries top-of-atmosphere radiances for all 21 OLCI bands plus uncertainties.",
        ),
    ),
}

# Timeliness variants share a spec with their non-time-critical sibling.
_TIMELINESS_ALIASES = {
    "sentinel-3-olci-2-wfr-nrt": "sentinel-3-olci-2-wfr-ntc",
    "sentinel-3-olci-1-efr-nrt": "sentinel-3-olci-1-efr-ntc",
}

# Plain-language shorthands a model or user is likely to produce.
_ALIASES = {
    "sentinel-2": "sentinel-2-l1c",
    "sentinel2": "sentinel-2-l1c",
    "s2": "sentinel-2-l1c",
    "s2-l1c": "sentinel-2-l1c",
    "s2-l2a": "sentinel-2-l2a",
    "sentinel-3-olci-l2-water": "sentinel-3-olci-2-wfr-ntc",
    "olci-water": "sentinel-3-olci-2-wfr-ntc",
    "olci-l2": "sentinel-3-olci-2-wfr-ntc",
    "olci-l1b": "sentinel-3-olci-1-efr-ntc",
    **_TIMELINESS_ALIASES,
}

ARCHIVE_ASSET_KEY = "product"


def canonical_collection_id(collection: str) -> str:
    """Normalise a collection identifier, resolving known shorthands.

    Unknown values pass through unchanged so any of the 419 CDSE collections still works.
    """
    key = collection.strip().lower()
    return _ALIASES.get(key, key)


def spec_for(collection: str) -> CollectionSpec | None:
    """Return the tuned spec for a collection, or None if it is not first-class."""
    key = canonical_collection_id(collection)
    if key in FIRST_CLASS:
        return FIRST_CLASS[key]
    return FIRST_CLASS.get(_TIMELINESS_ALIASES.get(key, ""))


def has_cloud_cover(collection: str) -> bool | None:
    """Whether a collection carries ``eo:cloud_cover``.

    Returns None for collections we have no tuned knowledge of, so callers can distinguish
    "known to have none" from "unknown".
    """
    spec = spec_for(collection)
    return None if spec is None else spec.has_cloud_cover


def describe_asset(collection: str, key: str) -> AssetSpec | None:
    """Friendly name and description for an asset key, if known."""
    spec = spec_for(collection)
    if spec is None:
        return None
    if key in spec.asset_names:
        return spec.asset_names[key]
    # Sentinel-2 STAC keys are the bare band ids; OLCI reflectance keys carry a suffix.
    stripped = key.removesuffix("Data")
    return spec.asset_names.get(stripped)


def find_archive_asset(assets: dict[str, object]) -> str | None:
    """Find the whole-product archive asset key, case-insensitively.

    Sentinel-2 spells it ``Product`` and Sentinel-3 spells it ``product``; a case-sensitive
    lookup silently misses one of them.
    """
    for key in assets:
        if key.lower() == ARCHIVE_ASSET_KEY:
            return key
    return None
