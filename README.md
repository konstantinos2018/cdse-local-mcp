# cdse-local-mcp

Ask Claude for Sentinel satellite data in plain language. This MCP server searches the
[Copernicus Data Space Ecosystem](https://dataspace.copernicus.eu/) and downloads whole
products, single bands, or just your area of interest to your computer.

> *"Find Sentinel-2 scenes over the Gulf of Patras in July 2024 under 10% cloud, and crop the
> red and near-infrared bands to the gulf."*

**Status: alpha.** Sentinel-2 works end to end. Sentinel-3 water-quality data can be searched
and downloaded, but not yet cropped to an area.

## Install

### 1. What you need

- **[uv](https://docs.astral.sh/uv/getting-started/installation/)**, which runs the server
  straight from GitHub. There is nothing to clone.
- **CDSE S3 keys**, for downloading. Searching works without them. Create a free
  [Copernicus account](https://dataspace.copernicus.eu/), then generate keys in the
  [S3 keys manager](https://eodata-s3keysmanager.dataspace.copernicus.eu/). Copy the secret
  straight away: it is shown only once.
- **Access to this repository**, while it is private. Once invited, run `gh auth setup-git`
  once with the [GitHub CLI](https://cli.github.com/).

### 2. Add it to Claude

**Claude Code** — run this in a terminal, with your keys filled in:

```bash
claude mcp add cdse --scope user \
  -e CDSE_S3_ACCESS_KEY=your-access-key \
  -e CDSE_S3_SECRET_KEY=your-secret-key \
  -- "$(command -v uvx)" --from git+https://github.com/konstantinos2018/cdse-local-mcp cdse-local-mcp
```

**Claude Desktop and Cowork** — add this to `claude_desktop_config.json`, then quit and reopen
the app:

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

Replace `/absolute/path/to/uvx` with the output of `command -v uvx` (on Windows,
`where uvx`). It must be the full path: the app cannot find `uvx` by name. If the file
already has an `mcpServers` section, add the `"cdse"` entry inside it rather than a second
one.

| OS | `claude_desktop_config.json` location |
|---|---|
| macOS | `~/Library/Application Support/Claude/` |
| Windows | `%APPDATA%\Claude\` |
| Linux | `~/.config/Claude/` |

### 3. Build it once

The first start downloads its dependencies, which can take long enough for Claude to give up.
Run this once first; it exits when done:

```bash
uvx --from git+https://github.com/konstantinos2018/cdse-local-mcp cdse-local-mcp < /dev/null
```

On Windows, use `< NUL` in place of `< /dev/null`.

## Try it

Ask Claude *"list the Sentinel-2 collections"*, then search for a scene and download a band.
Claude shows you the options and their sizes, and waits for you to choose before downloading
anything. Files go to a `cdse-local-mcp` folder inside your Downloads folder.

## Settings

Set these alongside your keys: with `-e NAME=value` in Claude Code, or in the `env` block for
Claude Desktop.

| Variable | Default |
|---|---|
| `CDSE_S3_ACCESS_KEY`, `CDSE_S3_SECRET_KEY` | None. Required for downloads |
| `CDSE_DOWNLOAD_DIR` | `cdse-local-mcp` inside your Downloads folder |
| `CDSE_MAX_CALL_BYTES` | 5 GiB per download |
| `CDSE_MAX_SESSION_BYTES` | 25 GiB per session |

Using Cowork? It can only open files inside the folder you share with it, so point
`CDSE_DOWNLOAD_DIR` there.

## Updating

```bash
uvx --refresh --from git+https://github.com/konstantinos2018/cdse-local-mcp cdse-local-mcp < /dev/null
```

Then restart Claude.

## Troubleshooting

| Problem | Fix |
|---|---|
| Server won't start, or "not found" | Use the full path to `uvx` in Claude Desktop's config |
| Fails or times out the first time | Run step 3, *Build it once* |
| `Git operation failed` | You don't have access to the repository yet; see step 1 |
| Search works, but downloads ask for S3 keys | The keys are missing from your Claude configuration |
| Tools don't appear in Claude Desktop | Quit the app completely, not just its window, and reopen |

Still stuck? The server's log says `s3=True` when your keys arrived. Claude Desktop keeps it
in `~/.config/Claude/logs/` on Linux and `~/Library/Logs/Claude/` on macOS.

## What it can do

| Tool | Does |
|---|---|
| `search_products` | Find scenes by collection, area and date |
| `list_product_assets` | List the files inside a scene, such as individual bands |
| `list_collections` | Browse all 419 CDSE collections |
| `download_assets` | Download only the bands you name — usually what you want |
| `download_window` | Crop an area out of named bands, as GeoTIFFs |
| `download_product` | Download a whole scene |
| `download_status`, `download_cancel` | Follow or stop a download |

Any CDSE collection can be searched. Four have tuned support: Sentinel-2 L1C and L2A, and
Sentinel-3 OLCI Level-1B and Level-2 Water.

## More

- [How it behaves, and why](docs/design.md) — why it asks before downloading, how cloud cover
  works, why it uses S3 keys
- [Development](docs/development.md) — running from source, tests, design documents
- [Changelog](CHANGELOG.md)

## License

Apache-2.0. Copyright 2026 Kostas Vlachos.

Contains modified Copernicus data. Copernicus Sentinel data are provided free of charge under
the [Copernicus data policy](https://dataspace.copernicus.eu/terms-and-conditions).
