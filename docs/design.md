# How cdse-local-mcp behaves, and why

The README covers installing and using the server. This page explains the choices behind its
behaviour, for anyone wondering why it asks before downloading, refuses some requests, or
returns files in a particular shape.

## Why

Asking for Earth observation data is easy to say and tedious to do: find the right collection
id, build a bounding box in the right axis order, filter the cloud cover, work out which of a
product's 21 assets you actually need, and avoid pulling a gigabyte when 40 MB would do. This
server puts that behind a conversation:

> *"Find me a Sentinel-2 L1C scene over the Gulf of Patras in July 2024, under 20% cloud."*

```
3 products, area searched 21.3000,38.1000,21.9000,38.4000 (EPSG:4326 lon/lat)

  S2A_MSIL1C_20240731T092031_..._T34SEH   cloud  6.8%   tile MGRS-34SEH   ~800 MB
  S2B_MSIL1C_20240726T091559_..._T34SEH   cloud 16.4%   tile MGRS-34SEH   ~800 MB
  S2A_MSIL1C_20240721T092031_..._T34SEH   cloud  2.5%   tile MGRS-34SEH   ~800 MB
```

## Design decisions

**Nothing downloads by accident.** Search returns candidates with size estimates; you choose;
download tools accept only explicit product ids. There is no path from a place name straight
to bytes on disk.

**Cloud cover is never filtered silently.** If you did not state a threshold, the assistant is
told to ask you rather than pick one. Quietly returning the five least-cloudy scenes when you
asked about a specific date is a wrong answer that looks right.

**Ask for bands, not products.** `download_assets` takes the files you actually need, by key
or friendly name, and writes them into the product's directory structure so later requests
top up the same tree:

```
download_assets(..., assets=["red_10m", "nir_10m"])
  -> B04, B08: 51.8 MiB instead of 204.7 MiB for the whole product
```

**Crop an area, not a scene.** `download_window` cuts your bounding box out of each band and
writes a GeoTIFF at the band's native resolution and projection — no resampling. Bands are
fetched once and cached, so further windows over them are free:

```
download_window(..., assets=["red_10m", "nir_10m"], bbox=[21.3, 38.1, 21.9, 38.4])
  -> B04, B08 over the Gulf of Patras: 5273 x 3364 px each, EPSG:32634, 12.6 s
  -> a second window over the same bands: 0 B fetched, 1.9 s
```

**Products arrive unpacked.** CDSE stores a `.SAFE` as a tree of objects rather than a zip,
so a download rebuilds that tree on disk — ready to open in QGIS or rasterio, no extraction
step. A real Sentinel-2 L1C scene is 66 files and ~785 MiB.

**Data moves through the filesystem, not the conversation.** Tool results carry paths, sizes
and checksums. A Sentinel-2 band is ~150 MB and an assistant's context window is not a pipe
for raster data.

**Sentinel-3 is not Sentinel-2.** OLCI is swath data with per-pixel latitude/longitude and no
map projection. The server tracks that distinction, because code that treats a swath as a grid
produces plausible output located nowhere.

## Water quality

For water quality, note that `chl_nn` (neural net) is the appropriate chlorophyll product in
coastal and turbid water, while `chl_oc4me` is calibrated for clear open ocean. The server
says so when it returns both.

## Authentication

**Why S3 rather than OAuth.** The OData download endpoint rejects tokens from a Sentinel Hub
OAuth client (`DAT-ZIP-609`, "Token audience not allowed") and accepts only a password-grant
token. S3 keys avoid passwords entirely, are independently revocable, and are CDSE's
documented high-performance path.

## Quotas

CDSE's free tier allows 10 000 catalogue requests and roughly 12 TB of transfer per month,
with 4 concurrent connections. The server caps transfers per call and per session so an
assistant cannot spend your month in a loop. Full numbers: [cdse-apis.md](cdse-apis.md).

For the measurements behind these choices, see [downloads.md](downloads.md),
[collections.md](collections.md) and [cdse-apis.md](cdse-apis.md).
