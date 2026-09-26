# cdse-local-mcp

An MCP server that gives AI assistants access to the [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/):
search the Sentinel catalogue, inspect products, and retrieve them to local disk.

It runs locally over stdio, so the data lands on your machine and stays there.

> **Status: alpha.** Discovery, whole-product, selective and windowed downloads work for
> Sentinel-2, verified against the live API. OLCI windowing is next — see [Roadmap](#roadmap).

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

## Design decisions worth knowing

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

## Install

Requires Python 3.11+.

```bash
git clone https://github.com/konstantinos2018/cdse-local-mcp
cd cdse-local-mcp
uv sync --extra download          # or: pip install -e ".[download]"
```

The `download` extra (`boto3`, `rasterio`) is what makes downloads work. **Always include
it**: `uv sync` makes the environment match its arguments exactly, so a later plain `uv sync`
uninstalls both. The server would still start and search would still work, but every download
would fail.

## Credentials

**Catalogue search needs no credentials.** You can search, inspect products and browse
collections immediately.

Downloads need **S3 access keys** from a free
[Copernicus Data Space account](https://dataspace.copernicus.eu/):

| Variable | For | Where |
|---|---|---|
| `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY` | All downloads | [S3 keys manager](https://eodata-s3keysmanager.dataspace.copernicus.eu/) |
| `CDSE_DOWNLOAD_DIR` | Where files land | Defaults to `~/.cache/cdse-local-mcp` |

Account passwords are never used or accepted.

**Why S3 rather than OAuth.** The OData download endpoint rejects tokens from a Sentinel Hub
OAuth client (`DAT-ZIP-609`, "Token audience not allowed") and accepts only a password-grant
token. S3 keys avoid passwords entirely, are independently revocable, and are CDSE's
documented high-performance path.

## Connect an MCP client

```json
{
  "mcpServers": {
    "cdse": {
      "command": "uv",
      "args": ["--directory", "/path/to/cdse-local-mcp", "run", "cdse-local-mcp"],
      "env": {
        "CDSE_S3_ACCESS_KEY": "...",
        "CDSE_S3_SECRET_KEY": "..."
      }
    }
  }
}
```

Try it by hand with the MCP Inspector:

```bash
npx @modelcontextprotocol/inspector uv run cdse-local-mcp
```

## Tools

| Tool | Does |
|---|---|
| `search_products` | Find products by collection, area and date, newest first |
| `list_product_assets` | List the individual files inside a product, with friendly names |
| `list_collections` | Browse the 419 CDSE collections |
| `download_assets` | Fetch only the bands or variables you name — usually what you want |
| `download_window` | Cut a bounding box out of named bands, as GeoTIFFs |
| `download_product` | Fetch a whole product to disk, as a background job |
| `download_status` | Check progress; returns the file path when complete |
| `download_cancel` | Stop a download, keeping the partial file for resuming |

## Supported collections

Any CDSE collection can be searched. Four have tuned support — band and variable
vocabularies, usage guidance, recorded test fixtures:

| Collection | |
|---|---|
| `sentinel-2-l1c` | Top-of-atmosphere reflectance |
| `sentinel-2-l2a` | Surface reflectance |
| `sentinel-3-olci-2-wfr-ntc` | Water quality: chlorophyll, suspended matter, KD490 transparency |
| `sentinel-3-olci-1-efr-ntc` | OLCI Level-1B radiances |

For water quality, note that `chl_nn` (neural net) is the appropriate chlorophyll product in
coastal and turbid water, while `chl_oc4me` is calibrated for clear open ocean. The server
says so when it returns both.

## Roadmap

- [x] Catalogue discovery over STAC
- [x] Whole-product download as background jobs, resumable and size-verified
- [x] Selective download: individual bands and OLCI variables
- [x] Windowed extraction for gridded products (Sentinel-2): bbox crops as GeoTIFFs
- [ ] Windowed extraction for swath products (Sentinel-3 OLCI): per-pixel lat/lon masking

## Quotas

CDSE's free tier allows 10 000 catalogue requests and roughly 12 TB of transfer per month,
with 4 concurrent connections. The server caps transfers per call and per session so an
assistant cannot spend your month in a loop. Full numbers: [docs/cdse-apis.md](docs/cdse-apis.md).

## Development

```bash
uv sync --all-extras
uv run pytest              # unit tests, no network
uv run pytest -m live      # against the real CDSE API; no credentials needed
uv run ruff check . && uv run mypy
```

Design documents:
[CLAUDE.md](CLAUDE.md) for working conventions,
[docs/cdse-apis.md](docs/cdse-apis.md) for verified endpoints and quotas,
[docs/collections.md](docs/collections.md) for per-collection detail,
[docs/downloads.md](docs/downloads.md) for the download architecture.

## License

Apache-2.0. Copyright 2026 Kostas Vlachos.

Contains modified Copernicus data. Copernicus Sentinel data are provided free of charge under
the [Copernicus data policy](https://dataspace.copernicus.eu/terms-and-conditions).
