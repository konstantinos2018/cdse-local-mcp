# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Tool names and input schemas are part of the public interface: agent configurations depend on
them, so renaming a tool or changing its inputs is a breaking change.

## [Unreleased]

First release, targeting 0.1.0.

### Added

- `search_products` — catalogue search by collection, area and date over the CDSE STAC API,
  newest first, with cursor pagination. Echoes back the area and time range actually searched.
- `list_product_assets` — the individual files inside a product, with friendly names such as
  `red_10m` and `chlorophyll_nn`; assets the catalogue lists without a usable href are
  reported separately.
- `list_collections` — all 419 CDSE collections, following pagination past the CLMS entries.
- `download_product` — a whole product, rebuilt on disk as an unpacked `.SAFE` directory.
- `download_assets` — only the named bands or variables, written into the product's directory
  tree so later requests top it up without re-fetching.
- `download_window` — a bounding box cut out of named bands, one GeoTIFF per band at its native
  projection and resolution. Bands are cached, so further windows over them transfer nothing.
- `download_status` and `download_cancel` — downloads run as background jobs; cancelling keeps
  the partial file for resuming.
- Tuned support for Sentinel-2 L1C and L2A, and Sentinel-3 OLCI L1B and Level-2 Water
  discovery.

### Security

- Downloads authenticate with CDSE S3 access keys. Account passwords are never accepted.
- Every write is confined to the download directory; remote-supplied names cannot escape it.
- Per-call and per-session transfer caps, so an agent cannot spend a month's quota unasked.

### Known limitations

- **No cloud-cover default.** Optical searches refuse until a threshold, or `"any"`, is given.
  This is deliberate: the assistant is expected to ask.
- **Sentinel-3 OLCI windowing is not built.** `download_window` refuses swath products; use
  `download_assets` with `geo-coordinates`, `wqsf` and the variables needed.
- **S3 downloads are verified by size, not checksum.** Each object's length is checked; content
  hashes are not yet compared.
- **Windows are not read remotely.** Each band is fetched whole, then cropped locally. Measured
  on live CDSE, this was faster and used far fewer requests than remote JPEG 2000 reads.
