# cdse-local-mcp

An MCP server that gives AI assistants access to the [Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/):
search the Sentinel catalogue, inspect products, and retrieve them to local disk.

It runs locally over stdio, so the data lands on your machine and stays there.

> **Status: alpha.** Discovery, whole-product, selective and windowed downloads work for
> Sentinel-2, verified against the live API. OLCI windowing is next — see [Roadmap](#roadmap).
> Install straight from GitHub with `uvx`; see [Get started](#get-started).

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

## Get started

You need two things:

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**, which fetches and runs
  the server straight from GitHub. There is nothing to clone.
- **CDSE S3 access keys**, but only for downloads. Search works without them. See
  [Configuration](#configuration).

Two details matter, and both fail quietly when wrong:

- **Use absolute paths.** Desktop apps don't load your shell profile, so `uvx` is usually not
  on their `PATH`.
- **Put settings in the client's configuration**, as shown below, not in a `.env` file. The
  server would start and search, but every download would fail for lack of keys.

Find the absolute path of `uvx`:

```bash
command -v uvx        # macOS and Linux
where uvx             # Windows
```

### Claude Code

```bash
 claude mcp add cdse --scope user \
  -e CDSE_S3_ACCESS_KEY=your-access-key \
  -e CDSE_S3_SECRET_KEY=your-secret-key \
  -- "$(command -v uvx)" --from git+https://github.com/konstantinos2018/cdse-local-mcp cdse-local-mcp
```

`$(command -v uvx)` is fine here: your shell expands it before the command runs, so the
stored configuration gets the absolute path. `--scope user` makes the server available in every
project, and the leading space keeps your keys out of shell history. Confirm with
`claude mcp list`.

### Claude Desktop and Cowork

Add the server to `claude_desktop_config.json`:

| OS | File |
|---|---|
| macOS | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| Windows | `%APPDATA%\Claude\claude_desktop_config.json` |
| Linux (beta) | `~/.config/Claude/claude_desktop_config.json` |

```json
{
  "mcpServers": {
    "cdse": {
      "command": "/absolute/path/to/uvx",
      "args": ["--from", "git+https://github.com/konstantinos2018/cdse-local-mcp", "cdse-local-mcp"],
      "env": {
        "CDSE_S3_ACCESS_KEY": "your-access-key",
        "CDSE_S3_SECRET_KEY": "your-secret-key"
      }
    }
  }
}
```

Replace `/absolute/path/to/uvx` with the path printed above. Unlike in a terminal, nothing in
this file is expanded, so `$(...)` and `~` don't work in `command`. If the file already has an
`mcpServers` object, add the `"cdse"` entry inside it. Then **fully quit and reopen the app**;
closing the window is not enough.

Cowork uses the same configuration. It runs in a virtual machine that sees only the folder you
share with it, while this server runs on your computer. For Cowork to open what it downloads,
set `CDSE_DOWNLOAD_DIR` to a folder inside the shared one.

### The first launch

The first launch builds the server and downloads its dependencies, including the GDAL library
bundled with `rasterio`. That can take long enough for a client to give up waiting. Build it
once in a terminal first; the command exits when it's done:

```bash
uvx --from git+https://github.com/konstantinos2018/cdse-local-mcp cdse-local-mcp < /dev/null
```

In the Windows Command Prompt, use `< NUL` instead of `< /dev/null`.

### Updating

`uvx` caches the build, so a new version on GitHub isn't picked up automatically. To update,
rebuild with `--refresh`, then restart your client:

```bash
uvx --refresh --from git+https://github.com/konstantinos2018/cdse-local-mcp cdse-local-mcp < /dev/null
```

Once releases are tagged, you can pin one instead by appending it to the URL, for example
`git+https://github.com/konstantinos2018/cdse-local-mcp@v0.1.0`.

### While the repository is private

`uvx` fetches with `git`, so it needs the same access as `git clone`. With the
[GitHub CLI](https://cli.github.com/), run `gh auth setup-git` once so that `git` uses your
GitHub login over HTTPS. If you use SSH keys with GitHub instead, replace the URL with
`git+ssh://git@github.com/konstantinos2018/cdse-local-mcp` everywhere above.

### Check it works

Ask *"list the Sentinel-2 collections"*: it needs no credentials, so it tests the connection
by itself. Then try a search and a small download.

At startup the server reports what it received, on stderr:

```
cdse-local-mcp 0.1.0.dev0 ready: 8 tools, oauth=False, s3=True
```

`s3=False` means the keys are not reaching it. Claude Desktop keeps this log in
`~/.config/Claude/logs/mcp-server-<name>.log` on Linux, and in `~/Library/Logs/Claude/` on
macOS.

| Symptom | Cause | Fix |
|---|---|---|
| Server doesn't start, or "not found" / `ENOENT` | `uvx` not found: relative path, or `$(...)` in a JSON file | Use the absolute path |
| Server fails or times out on first use | First build still running | Build it in a terminal first, as above |
| "Repository not found" or an authentication error | No access to the private repository | See [While the repository is private](#while-the-repository-is-private) |
| Search works; downloads say *"S3 access keys, which are not configured"* | Keys not in the client configuration | Add them to `env` (or `-e` for Claude Code) |
| Tools don't appear after editing the config | App not fully restarted | Quit it completely and reopen |
| Can't find the downloaded files | Looking in the wrong place | Check `Downloads/cdse-local-mcp`, or wherever `CDSE_DOWNLOAD_DIR` points |
| Cowork can't open the downloaded files | Its VM sees only the shared folder | Download into that folder |

## Configuration

All settings are environment variables, set in your client's configuration as above.

**Catalogue search needs no credentials.** Downloads need **S3 access keys** from a free
[Copernicus Data Space account](https://dataspace.copernicus.eu/).

| Variable | For | Notes |
|---|---|---|
| `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY` | All downloads | Create them in the [S3 keys manager](https://eodata-s3keysmanager.dataspace.copernicus.eu/); the secret is shown once |
| `CDSE_DOWNLOAD_DIR` | Where files land | Optional. Defaults to a `cdse-local-mcp` folder inside your Downloads folder |
| `CDSE_MAX_CALL_BYTES`, `CDSE_MAX_SESSION_BYTES` | Transfer caps | Default 5 GiB per call, 25 GiB per session |

Account passwords are never used or accepted.

**Why S3 rather than OAuth.** The OData download endpoint rejects tokens from a Sentinel Hub
OAuth client (`DAT-ZIP-609`, "Token audience not allowed") and accepts only a password-grant
token. S3 keys avoid passwords entirely, are independently revocable, and are CDSE's
documented high-performance path.

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
git clone https://github.com/konstantinos2018/cdse-local-mcp
cd cdse-local-mcp
uv sync                    # or: pip install -e .
uv run pytest              # unit tests, no network
uv run pytest -m live      # against the real CDSE API; needs no credentials
uv run ruff check . && uv run mypy
```

**Run your working copy in a client** by pointing it at the executable `uv sync` installed,
instead of at `uvx`. It is installed in editable mode, so your changes take effect the next
time the client restarts the server:

```bash
realpath .venv/bin/cdse-local-mcp     # use this as "command"; Windows: .venv\Scripts\cdse-local-mcp.exe
```

**Exercise the tools by hand** with the MCP Inspector. Run it from the repository directory,
where a `.env` file is read; copy [.env.example](.env.example) to start one:

```bash
npx @modelcontextprotocol/inspector uv run cdse-local-mcp
```

**Test through a real client** with the scenarios in
[tests/manual/scenarios.md](tests/manual/scenarios.md). Record each session with the
*Manual test run* issue template.

Design documents:
[CLAUDE.md](CLAUDE.md) for working conventions,
[docs/cdse-apis.md](docs/cdse-apis.md) for verified endpoints and quotas,
[docs/collections.md](docs/collections.md) for per-collection detail,
[docs/downloads.md](docs/downloads.md) for the download architecture.

## License

Apache-2.0. Copyright 2026 Kostas Vlachos.

Contains modified Copernicus data. Copernicus Sentinel data are provided free of charge under
the [Copernicus data policy](https://dataspace.copernicus.eu/terms-and-conditions).
