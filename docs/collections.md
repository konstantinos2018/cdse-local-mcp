# First-class collections

Per-collection specifics for the four collections that get tuned defaults, band/variable
vocabularies and recorded fixtures. Everything else in CDSE's 419 collections works through
the generic path without this treatment.

Asset keys and file names below were read from live STAC items on **2026-09-12**, not from
documentation. Re-verify with a real item before trusting any of it in new code.

| Collection | Format | Geometry | Priority |
|---|---|---|---|
| `sentinel-2-l1c` | JPEG 2000 in `.SAFE` | Gridded, UTM | **First** — full-product download |
| `sentinel-2-l2a` | JPEG 2000 in `.SAFE` | Gridded, UTM | **First** — full-product download |
| `sentinel-3-olci-1-efr-*` | NetCDF4 in `.SEN3` | **Swath**, per-pixel lat/lon | Second |
| `sentinel-3-olci-2-wfr-*` | NetCDF4 in `.SEN3` | **Swath**, per-pixel lat/lon | Second — water quality |

---

## Cross-collection asset traps

These bit us during exploration and will bite again:

1. **Some assets have a `null` href.** A `sentinel-3-olci-2-wfr-ntc` item declares `chlor-a`,
   `fluo` and `iop-Lsd` with `"href": null`. Filter out assets without a usable href before
   presenting or downloading anything, or the model will confidently offer a variable that
   cannot be fetched.
2. **The archive asset key's capitalisation is inconsistent between collections.**
   Sentinel-2 uses `Product`; Sentinel-3 OLCI uses `product`. **Look up asset keys
   case-insensitively**, always.
3. **Asset keys are not friendly names.** OLCI L2 uses `chl-Nn`, `tsm-Nn`, `iop-Nn`, `w-Aer`,
   `Oa01_reflectanceData`. Map them to plain names in `domain/collections.py`; never make a
   user or model guess the casing.
4. **Band/variable sets are not complete.** OLCI L2 WFR carries reflectance for Oa01–Oa12,
   Oa16, Oa17, Oa18 and Oa21 only — bands 13, 14, 15, 19 and 20 are absent because they are
   atmospheric absorption bands. Never generate asset keys by counting to 21; read the item.

---

## Sentinel-2 L1C and L2A

**Assets** (21 on an L1C item): `B01`–`B12`, `B8A`, `TCI` as `image/jp2` on
`s3://eodata/Sentinel-2/MSI/…`; `Product` as the `.zip` archive via OData; `thumbnail`;
and `safe_manifest`, `product_metadata`, `granule_metadata`, `inspire_metadata`,
`datastrip_metadata` as XML.

**Band resolutions** — windows must be built per band, never shared across resolutions:

| GSD | Bands |
|---|---|
| 10 m | B02 (blue), B03 (green), B04 (red), B08 (NIR) |
| 20 m | B05, B06, B07, B8A, B11, B12 |
| 60 m | B01, B09, B10 (L1C only; L2A drops B10) |

**L1C vs L2A internal layout differs.** L1C keeps JP2s in `GRANULE/*/IMG_DATA/`; L2A nests
them under `GRANULE/*/IMG_DATA/R10m/`, `R20m/`, `R60m/`. **Resolve paths from STAC asset
hrefs — never string-build them.** L2A also adds scene classification (`SCL`) and AOT/WVP
layers, and drops B10.

**Sizes**: ~800 MB per L1C `.SAFE`; ~100–180 MB per 10 m band; a 5 × 5 km windowed crop of
one band is under 1 MB.

**CRS**: granules are in a UTM zone — Gulf of Patras is tile `T34SEH`, EPSG:32634. Reproject
any EPSG:4326 bbox into the granule CRS before windowing.

**Cloud cover** is available as `eo:cloud_cover`. See the filtering rule in `CLAUDE.md`: it is
never applied silently.

---

## Sentinel-3 OLCI — the swath problem

OLCI is **not gridded**. There is no affine transform and no CRS; the product carries a
per-pixel latitude/longitude array in `geo_coordinates.nc`. Every Sentinel-2 assumption in
this codebase breaks here, and the failure is silent — code that treats the pixel array as a
grid produces a plausible-looking image located nowhere.

Consequences:

- **`rasterio.windows.from_bounds` is meaningless for OLCI.** A bbox subset means: read the
  `latitude`/`longitude` arrays, build a boolean mask for the bbox, take the bounding
  row/column slice of that mask, then slice each variable array identically.
- **Output is a table or a NetCDF subset, not a GeoTIFF.** For the "lat/lon plus water
  quality parameters" use case the natural product is a tidy table — one row per pixel with
  `latitude`, `longitude`, and the requested variables — as CSV or Parquet. Producing a
  GeoTIFF would require resampling onto a regular grid, which is an analysis decision the
  server should not make silently.
- **300 m full resolution** (`efr`/`wfr`); the `err`/`wrr` variants are ~1.2 km reduced
  resolution. A whole OLCI scene is a 4865 × 4091 swath.
- **Timeliness variants** `-nrti` (near real time), `-ntc` (non-time-critical, best quality),
  `-stc`. Prefer `-ntc` for analysis; say which was used.

### OLCI L2 WFR — water quality variables

Confirmed file-to-quantity mapping:

| Asset key | File | Quantity |
|---|---|---|
| `chl-Nn` | `chl_nn.nc` | Chlorophyll-a, neural-net algorithm — **use for coastal/turbid water** |
| `chl-Oc4me` | `chl_oc4me.nc` | Chlorophyll-a, OC4Me algorithm — clear open ocean only |
| `tsm-Nn` | `tsm_nn.nc` | Total suspended matter, neural net |
| `trsp` | `trsp.nc` | Diffuse attenuation coefficient at 490 nm (KD490) — transparency |
| `iop-Nn` | `iop_nn.nc` | Absorption by coloured detrital and dissolved material at 443 nm (ADG443) |
| `w-Aer` | `w_aer.nc` | Aerosol load (A865, T865) |
| `par` | `par.nc` | Photosynthetically active radiation, 400–700 nm |
| `iwv` | `iwv.nc` | Integrated water vapour |
| `wqsf` | `wqsf.nc` | **Water Quality and Science Flags** |
| `geo-coordinates` | `geo_coordinates.nc` | Per-pixel `latitude`, `longitude` |
| `time-coordinates` | `time_coordinates.nc` | Per-scanline time |
| `Oa{01..12,16,17,18,21}_reflectanceData` | `Oa*_reflectance.nc` | Water-leaving reflectance |

For the Gulf of Patras (coastal, potentially turbid), `chl_nn` is the appropriate chlorophyll
product and `chl_oc4me` will be unreliable. Say so when returning both.

**A minimal water-quality request** is `geo_coordinates.nc` + the requested variable files +
`wqsf.nc` — tens of MB rather than the ~700 MB full `.SEN3`. This is the single best argument
for selective download.

### Two rules that prevent wrong numbers

**1. Never hardcode scaling — read the CF attributes.** Every variable read must apply
`scale_factor`, `add_offset` and `_FillValue` from the variable's own attributes, and respect
`valid_min`/`valid_max`.

**2. Read the `units` attribute and surface it.** OLCI L2 water variables are widely
documented as **log10-transformed** — chlorophyll and TSM carry units of the form
`lg(re mg.m-3)`, meaning the stored value is `log10(concentration)`. An unconverted read
returns numbers around 0–2 that look like plausible chlorophyll values but are wrong by
orders of magnitude. **This is flagged as unverified**: the fetches attempted during planning
did not reach an authoritative specification. On first implementation, open a real
`chl_nn.nc`, print the variable attributes, and record them in a test fixture. Then have the
code branch on the `units` string rather than on an assumption, and always report units
alongside values.

**3. Apply WQSF before interpreting anything.** The flag variable marks land, cloud, cloud
margin, invalid, sun glint, and suspect retrievals. Unmasked chlorophyll over cloud or land
is meaningless. Expose masking as an explicit, documented parameter with a sensible default,
and state in the result which flags were applied.
