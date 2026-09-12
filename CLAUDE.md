# CLAUDE.md

Guidance for Claude Code and human contributors working in this repository.

## What this is

`copernicus-dataspace-mcp` is an MCP server that gives LLM agents access to the **Copernicus
Data Space Ecosystem (CDSE)**: catalogue discovery over the STAC and OData APIs, and
retrieval of Sentinel products to local disk over S3 and OData. It runs as a local **stdio**
server (Claude Desktop, Claude Code, any MCP client) and is distributed as a public
open-source package on PyPI.

**Non-goals for v1.** Do not add these without being asked — they are deliberate exclusions,
not gaps:

- Sentinel Hub processing (Process / Statistical / Catalog APIs)
- openEO process graphs and batch jobs
- Remote HTTP / SSE transport and multi-user session handling
- Raster analysis, reprojection, mosaicking, or any GDAL-dependent processing
- Anything that writes back to CDSE (on-demand production, subscriptions, orders)

## Commands

```bash
uv sync --all-extras           # install, including dev deps
uv run cdse-mcp                # run the stdio server
uv run pytest                  # full suite, no network (live tests deselected)
uv run pytest -m live          # live CDSE tests; needs credentials in env
uv run ruff check --fix .      # lint
uv run ruff format .           # format
uv run mypy src                # type check
npx @modelcontextprotocol/inspector uv run cdse-mcp   # exercise tools by hand
```

Run `ruff`, `mypy` and `pytest` before declaring any change done.

## Layout and the layering rule

```
src/copernicus_dataspace_mcp/
├── server.py            # FastMCP instance + tool registration ONLY
├── config.py            # pydantic-settings: credentials, download root, caps
├── auth.py              # TokenProvider: client credentials, refresh, lock
├── errors.py            # typed errors -> structured tool results
├── clients/             # stac.py · odata.py · s3.py    (HTTP/S3 only, no MCP imports)
├── domain/              # models.py · collections.py · geometry.py
├── transfer/            # budget.py · jobs.py · download.py
└── tools/               # discovery.py · products.py · downloads.py   (thin adapters)
tests/
├── unit/                # mocked; the default suite
├── live/                # @pytest.mark.live, hits real CDSE
└── fixtures/            # recorded JSON responses
```

**The layering rule.** `tools/` modules are thin adapters: validate input, call a client or
transfer function, shape the result. All CDSE knowledge lives in `clients/` and `domain/` and
must be unit-testable without importing FastMCP. A tool function body should rarely exceed 30
lines.

**Module size ceiling: ~400 lines.** If a module approaches it, split it. A single oversized
`server.py` is the specific failure mode this repo exists to avoid; a predecessor reached
3,000 lines and became unmaintainable.

## Hard rules

1. **Never write to stdout.** `print()` corrupts the JSON-RPC stream and silently breaks the
   server. Logging goes to `stderr` via the `logging` module.
2. **Never return file bytes through a tool result.** Return paths, sizes, checksums and
   metadata. The only exception is a visual asset under `INLINE_ASSET_MAX_BYTES` (200 KB),
   returned as image content after an explicit size check.
3. **Never do a long transfer inside a tool call.** Downloads are jobs — see
   [docs/downloads.md](docs/downloads.md).
4. **Every byte-moving request passes through `transfer/budget.py`.** One process-wide
   `asyncio.Semaphore(4)` and one byte budget, no exceptions, no ad-hoc `httpx.stream` calls
   elsewhere.
5. **Every write path is confined to the download root.** `resolve()` then
   `is_relative_to(root)`; reject `..`, absolute paths and symlink escapes. No tool deletes or
   moves files outside it, and any destructive tool is dry-run by default.
6. **Never mint a token inline.** Exactly one `TokenProvider` instance; tool and client code
   asks it for a valid token.
7. **Never invent an endpoint URL, collection ID or quota number.** They come from
   [docs/cdse-apis.md](docs/cdse-apis.md) or a fresh fetch of
   `documentation.dataspace.copernicus.eu`. If you fetch and find a discrepancy, update that
   file in the same change.
8. **Never log or echo a secret.** Redact `Authorization` headers, client secrets and S3 keys
   in every error path and debug log.
9. **No network calls in the default test suite.** Mocked with `respx` against recorded
   fixtures.

## Tool design conventions

Tools are a user interface whose user is a language model. Design accordingly.

- **Names** are `verb_noun`, lowercase, stable: `search_products`, `get_product`,
  `list_product_assets`, `download_assets`, `download_status`.
- **The docstring is the contract.** Say what the tool does, when to prefer it over a
  sibling, what the units and CRS are, and what it costs (quota, bytes, time). Write it for a
  model that cannot see the implementation.
- **Pydantic v2 models for every input and output.** No bare dicts crossing the tool
  boundary; field descriptions are part of the schema the model reads.
- **Guard the response token budget.** Default `limit=10`, hard max 100, always return a
  pagination cursor. Return projected summaries, never raw STAC items or full OData
  attribute bags — a single Sentinel-2 item is several KB of mostly irrelevant metadata.
- **Errors are structured and actionable.** Raise the typed errors in `errors.py`; a tool
  result should tell the model what to do differently (narrow the date range, request a
  smaller asset, order the offline product), never surface a traceback.
- **Annotate side effects.** Read-only tools get `readOnlyHint=True`. Download tools get
  `readOnlyHint=False`, `destructiveHint=False`, `idempotentHint=True`, `openWorldHint=True`.

## Auth

Client-credentials flow only. **No account password is ever accepted or stored** — that was a
deliberate decision, do not add a password grant.

| Env var | Purpose |
|---|---|
| `CDSE_CLIENT_ID` | OAuth client id for token exchange |
| `CDSE_CLIENT_SECRET` | OAuth client secret |
| `CDSE_S3_ACCESS_KEY` | Optional; enables the S3 download transport |
| `CDSE_S3_SECRET_KEY` | Optional; pairs with the above |
| `CDSE_DOWNLOAD_DIR` | Download root (default: `~/.cache/copernicus-dataspace-mcp`) |

Access tokens live **10 minutes**. `TokenProvider` refreshes ahead of expiry behind an
`asyncio.Lock`, caches in memory only, and never touches disk. Catalogue search works
unauthenticated, so discovery tools must degrade gracefully when no credentials are
configured — report the limitation rather than failing at import time.

## CDSE domain gotchas

These cost real debugging time. [docs/cdse-apis.md](docs/cdse-apis.md) has the full reference.

- **Two catalogue APIs, different identifiers.** STAC uses `sentinel-2-l2a`; OData uses
  collection name `SENTINEL-2` plus a `productType` attribute filter. Map between them in
  `domain/collections.py` — never hardcode either in `tools/`.
- **Which to use.** STAC for geospatial/temporal search, CQL2 filtering and asset discovery.
  OData for UUID-addressed product metadata, node traversal and download URLs. STAC item IDs
  and OData UUIDs are different keys for the same product; keep both on the domain model.
- **Two hosts.** Catalogue queries go to `catalogue.dataspace.copernicus.eu`; downloads to
  `download.dataspace.copernicus.eu`. Using the wrong one fails confusingly.
- **Geometry.** Bounding boxes are `[west, south, east, north]` in EPSG:4326 — lon/lat, not
  lat/lon. Validate and normalise in `domain/geometry.py`; reject antimeridian-crossing
  boxes with a clear message rather than returning silently empty results.
- **Time.** All instants are UTC ISO-8601 with an explicit `Z`. OData needs
  `2024-01-15T00:00:00.000Z` precision.
- **Cloud cover** is `eo:cloud_cover` in STAC and the `cloudCover` attribute in OData, and
  is absent entirely for SAR and many non-optical collections. Never assume it exists.
- **Offline products.** Older archives have `Online: false` and cannot be downloaded directly;
  they need a Data Workspace order (25 products/month, one active order). Check `Online`
  before enqueueing a transfer.
- **Quota is finite and shared.** 10 000 OData requests/month, 12 TB per rolling 30 days,
  4 concurrent connections. Cache aggressively; never poll in a tight loop.

## Testing

- `tests/unit/` is the default suite: `respx`-mocked HTTP, fixtures in `tests/fixtures/`, no
  network, deterministic, fast.
- When adding a client method, record a real response once and commit a **trimmed, anonymised**
  fixture. Do not commit multi-megabyte payloads or anything carrying a token.
- `tests/live/` is marked `@pytest.mark.live`, skipped unless CDSE credentials are in the
  environment, and must stay small — it spends the user's monthly quota.
- Download logic needs explicit tests for the nasty paths: path-traversal attempts, checksum
  mismatch, resume from a partial `.part` file, budget refusal, cancellation mid-transfer.

## Security and release

- No secrets, credentials, personal paths or account identifiers in the repo, tests, fixtures
  or docs — env var *names* only.
- No telemetry, no analytics, no phoning home.
- Pin no upper bounds unnecessarily; do keep a lower bound on `fastmcp`.
- Semantic versioning; every user-visible change gets a `CHANGELOG.md` entry. Tool renames
  and schema changes are **breaking** — agent configurations depend on them.
- Release: `uv build` then publish to PyPI from CI on a tag.

## Style

- Python ≥3.11, `async` throughout, full type hints — `mypy` runs strict on `src/`.
- `ruff format`, 100-column lines.
- No bare `except`; catch specific exceptions and wrap them in a typed error from
  `errors.py`.
- Comment *why*, not *what*. Match the density of the surrounding code.

## Open decisions

Ask before assuming; these are not yet settled.

- GitHub owner, final package name on PyPI, and author attribution
- License (Apache-2.0 recommended, not yet confirmed)
- Priority collections that get first-class defaults and recorded fixtures
- Shipped default for the per-session byte budget
- Whether CI runs a scheduled live smoke test or stays mocked-only
