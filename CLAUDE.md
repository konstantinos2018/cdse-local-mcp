# CLAUDE.md

Guidance for AI coding agents and human contributors working in this repository.

## What this is

`cdse-local-mcp` is an MCP server that gives LLM agents access to the **Copernicus Data Space
Ecosystem (CDSE)**: catalogue discovery over the STAC and OData APIs, and retrieval of
Sentinel products to local disk over S3. It runs as a local **stdio** server against
any MCP client, and is distributed as a public open-source package on PyPI.

- Repository: `https://github.com/konstantinos2018/cdse-local-mcp`
- Package / console script: `cdse-local-mcp`
- Import path: `cdse_local_mcp`
- Author: Kostas Vlachos
- License: Apache-2.0

**Do not add AI attribution anywhere** — no `Co-Authored-By` trailers, no "generated with"
lines in commits, pull requests, docs or source comments.

**Non-goals for v1.** Do not add these without being asked — they are deliberate exclusions,
not gaps:

- Sentinel Hub processing (Process / Statistical / Catalog APIs)
- openEO process graphs and batch jobs
- Remote HTTP / SSE transport and multi-user session handling
- Raster *analysis*: band math, indices, mosaicking, time-series compositing. The only
  raster work in scope is reading a spatial window out of a product and writing it as a
  GeoTIFF (`transfer/window.py`, via rasterio) — cropping, not computing.
- Anything that writes back to CDSE (on-demand production, subscriptions, orders)

## Commands

```bash
uv sync                        # install, including dev deps
uv run cdse-local-mcp          # run the stdio server
uv run pytest                  # full suite, no network (live tests deselected)
uv run pytest -m live          # live CDSE tests; needs credentials in env
uv run ruff check --fix .      # lint
uv run ruff format .           # format
uv run mypy src                # type check
npx @modelcontextprotocol/inspector uv run cdse-local-mcp   # exercise tools by hand
```

Run `ruff`, `mypy` and `pytest` before declaring any change done.

## Running under an MCP client

User-facing setup is in the README; these are the constraints it exists to satisfy. Each one
failed quietly on a real machine before being written down.

- **Configuration arrives as environment variables from the client's `env` block.** Clients
  launch the server from an arbitrary working directory, and `.env` is resolved against the
  working directory, so clients never see it. Launched that way, the server still started and
  advertised every tool, reporting `s3=False`: search worked and every download failed.
  `.env` is a development convenience; never make anything depend on it.
- **The command runs without a shell and with a minimal `PATH`.** `$(which uv)` is taken as a
  literal program name, and `~/.local/bin` is not on a desktop app's `PATH`. Document absolute
  paths only. The recommended command is the venv's console script, which needs neither `uv`
  nor `--directory`.
- **`~` in `CDSE_DOWNLOAD_DIR` works** because `transfer/paths.py` calls `expanduser()`, not
  because anything in the client expands it. Keep every use of the download root behind
  `ensure_root` / `resolve_target`.
- **Tests must never write to the default download folder.** It is the user's real Downloads
  directory, so `tests/conftest.py` points `CDSE_DOWNLOAD_DIR` at a temporary folder for every
  test. Keep that fixture autouse.
- **The startup log line is the diagnostic**: `ready: N tools, oauth=…, s3=…` on stderr. Keep
  it, and keep it accurate. Claude Desktop writes it to
  `~/.config/Claude/logs/mcp-server-<name>.log` on Linux.
- **Cowork runs in a VM that sees only its shared folder**, while this server runs on the
  host. Paths in tool results are host paths, which Cowork can open only if they fall inside
  that folder.

## Layout and the layering rule

```
src/cdse_local_mcp/
├── server.py            # FastMCP instance + tool registration ONLY
├── config.py            # pydantic-settings: credentials, download root, caps
├── auth.py              # TokenProvider: one per process, client credentials, refresh-ahead
├── errors.py            # typed errors -> structured tool results
├── clients/             # stac.py · odata.py · s3.py   (HTTP/S3 only, no MCP imports)
├── domain/              # models.py · collections.py · geometry.py · timerange.py
├── transfer/            # budget.py · paths.py · download.py · bulk.py · jobs.py · window.py
└── tools/               # discovery.py · downloads.py · windows.py · _shared.py  (thin adapters)
tests/
├── unit/                # mocked; the default suite
├── live/                # @pytest.mark.live, hits real CDSE (needs no credentials)
└── fixtures/            # trimmed captures of real responses
```

**Current state**: eight tools — `search_products`, `list_product_assets`,
`list_collections`, `download_product`, `download_assets`, `download_window`,
`download_status`, `download_cancel`. All verified against real CDSE products: a whole
Sentinel-2 L1C scene (66 files, 785 MiB, exact size match), a two-band selective fetch
(51.8 MiB instead of 204.7 MiB), and bbox windows (whole-gulf crop in 12.6 s; a second window
over the same bands in 1.9 s with nothing fetched). Next: OLCI windowing, which is blocked on
the output-format decision under Open decisions.

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
6. **Never mint a token or build an S3 signature inline.** Exactly one `TokenProvider` and
   one `S3Client` per process; tool code asks them, never talks to CDSE directly.
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
  `list_product_assets`, `download_product`, `download_assets`, `download_window`,
  `download_status`, `download_cancel`.
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
- **Annotate side effects** using the shared dicts in `tools/__init__.py` (`READ_ONLY`,
  `DOWNLOADS`) rather than writing them per tool. Use the **snake_case** field names
  (`read_only_hint`); the MCP wire format uses the camelCase aliases and the SDK converts.
  FastMCP is pinned to 4.x — its API differs from 2.x, so check before upgrading.

### Two required interaction rules

These are explicit user decisions, not defaults to be tuned away.

**1. Never filter on cloud cover silently.** `max_cloud_cover` is `Optional[float]` with **no
default**. If the user stated a threshold, apply it. If they did not, do not invent one and do
not quietly return everything as though the question never arose — the tool description
instructs the model to **ask the user for a threshold** before searching an optical
collection. Returning the 5 least-cloudy scenes when the user wanted a specific date is a
silent wrong answer. Collections without `eo:cloud_cover` must reject the parameter with a
clear message: sending it anyway excludes every product, and the user is told no data
exists. Which collections record it is **checked, not assumed**: the four first-class
collections answer from the registry (verified live, and pinned by a live test), and every
other collection from its per-collection STAC queryables. Radar, Sentinel-5P, DEMs and OLCI
Level-1B record none; **OLCI Level-2 Water does**, despite being swath data.

**2. Search, present, confirm, then download.** No tool downloads as a side effect of
searching, and no single call goes from a place name to bytes on disk. The flow is:

1. `search_products` returns a compact candidate list — id, datetime, cloud cover, tile/orbit,
   **estimated download size**, and whether the product is online.
2. The model presents that list and the user picks.
3. `download_*` is called with explicit product ids taken from that list.

Download tools therefore accept only explicit identifiers — never a search query, never
"the best match", never a whole result set. The size estimate exists so the user is
confirming against a real number. The byte budget in `transfer/budget.py` is the backstop for
when this flow is bypassed, not a substitute for it.

## Auth

**S3 access keys for every download. No account password, ever** — that was a deliberate
decision; do not add a password grant.

| Env var | Purpose |
|---|---|
| `CDSE_S3_ACCESS_KEY` | Required for any download, whole, selective or windowed |
| `CDSE_S3_SECRET_KEY` | Pairs with the above |
| `CDSE_CLIENT_ID` | Optional, **unused in v1**; reserved for Sentinel Hub APIs |
| `CDSE_CLIENT_SECRET` | Pairs with the above |
| `CDSE_DOWNLOAD_DIR` | Download root (default: `<Downloads>/cdse-local-mcp`, honouring a localised XDG Downloads folder on Linux) |
| `CDSE_MAX_CALL_BYTES` | Per-call transfer cap (default 5 GiB) |
| `CDSE_MAX_SESSION_BYTES` | Per-session transfer budget (default 25 GiB) |

**Why S3 and not OAuth.** A Sentinel Hub `sh-*` client-credentials token is rejected by the
OData download service with `DAT-ZIP-609 "Token audience not allowed"`; only a
password-grant `cdse-public` token is accepted there, and this project does not support
account passwords. S3 keys are long-lived, independently revocable, and need no OAuth at all.
Verified against the live API on 2026-09-13; see [docs/cdse-apis.md](docs/cdse-apis.md).

**Capability tiers by credential.** Tools degrade gracefully and name the missing credential;
nothing fails at import time.

| Configured | Available |
|---|---|
| nothing | all discovery: `search_products`, `list_product_assets`, `list_collections`, and OData product metadata |
| + S3 keys | every download: `download_product`, `download_assets`, `download_window` |
| + OAuth client | nothing in v1 |

Every download tool checks `settings.has_s3` **before doing any work**. Product metadata is
readable without credentials, so a tool that checked later would queue a job that cannot
possibly run and report success.

`auth.py`'s `TokenProvider` is built and tested but has no caller in v1. If Sentinel Hub
APIs are ever added it is ready: one instance, refresh-ahead behind an `asyncio.Lock`,
memory-only. Read `expires_in` rather than assuming a lifetime — the Quotas page says 600 s,
a client-credentials token came back with 1800 s.

README states that S3 keys are required and links to the keys manager; it does not walk
through creating them.

## CDSE domain gotchas

These cost real debugging time. [docs/cdse-apis.md](docs/cdse-apis.md) has the full reference.

- **Two catalogue APIs, different identifiers.** STAC uses `sentinel-2-l1c`; OData uses
  collection name `SENTINEL-2` plus a `productType` attribute filter. Map between them in
  `domain/collections.py` — never hardcode either in `tools/`.
- **The official docs list stale STAC collection IDs.** Verified live IDs are in
  [docs/cdse-apis.md](docs/cdse-apis.md); the doc page's `sentinel-5p-l2`,
  `sentinel-3-olci-l1b` and `copernicus-dem-cog` do not exist. Trust `/collections`.
- **`/collections` needs pagination** — 419 collections, 200 per page, and every Sentinel
  collection sorts *after* the CLMS ones. Reading page one only finds no Sentinel data at all.
- **Naming conventions differ by product family.** Sentinel and DEM use hyphens
  (`sentinel-2-l1c`); CLMS uses underscores with a `_cog` suffix
  (`clms_ndvi_global_300m_10daily_v3_cog`).
- **Which to use.** STAC for geospatial/temporal search, CQL2 filtering and asset discovery.
  OData for node traversal and attributes not in STAC. STAC item IDs and OData UUIDs are
  different keys for the same product; keep both on the domain model. Note that a STAC item's
  `Product` asset href **already contains the OData UUID**, so search alone is usually
  enough — do not make a second OData call to find it.
- **Band assets are individually addressable.** Sentinel-2 items expose `B01`–`B12`, `B8A`
  and `TCI` as separate `s3://eodata/…` JP2 objects. This is what makes per-band and windowed
  download possible; prefer it over the whole archive.
- **Granule CRS is UTM, not WGS84.** Sentinel-2 rasters sit in a UTM zone (Gulf of Patras →
  EPSG:32634). Reproject a user bbox into the source CRS before building a read window, or
  the crop lands somewhere else entirely. Band resolutions differ (10/20/60 m) — build the
  window per band.
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
- **Product metadata is readable without credentials; only the bytes need them.** So a
  download tool must check `settings.has_s3` *before* doing anything, or it will happily
  queue a job that cannot possibly run and report success.
- **httpx strips `Authorization` on cross-host redirects, and `$value` always redirects** to
  a delivery host. Following redirects automatically yields a 401 that looks like bad
  credentials. `ODataClient` follows them by hand, re-attaching the token each hop.
- **`Online` absent means online.** Only archived products carry the flag, so treat a missing
  value as available rather than defaulting to offline.
- **Do not read JPEG 2000 windows remotely.** GDAL over `/vsis3` works but needs ~89 small
  reads per window: 16–110 s against 4.9 s to fetch the whole band and crop locally, and it
  bypasses the budget semaphore. `download_window` fetches then crops, by measured decision —
  see `transfer/window.py`. Revisit only for Cloud-Optimised GeoTIFF collections.
- **S3 refuses presigned URLs.** Query-string SigV4 returns `403 InvalidAccessKeyId` on keys
  that list and read fine with header auth, so `S3Client` signs request headers with
  `S3SigV4Auth`. Do not "simplify" it back to `generate_presigned_url`.
- **Products are stored unpacked on S3.** A `.SAFE` is a prefix of hundreds of objects, not a
  zip, so a whole-product download rebuilds a directory tree. Object keys come from a remote
  listing, so every one goes through the path sandbox.
- **Only the per-collection queryables tell the truth about filterable properties.** The global
  `/queryables` is the union across all collections and claims `eo:cloud_cover` everywhere.
- **Scene cloud cover and pixel quality flags are different things.** OLCI Level-2 has both: an
  `eo:cloud_cover` to choose scenes by, and `wqsf` flags to mask pixels once read. One does
  not replace the other.
- **Rate limiting arrives far sooner than the documented 2000/minute.** A handful of
  requests in quick succession can return 429 — running the four live tests together did it.
  `StacClient` retries 429 and 5xx three times with backoff, honouring `Retry-After`, and
  retries nothing else: repeating a 400 only spends quota. Keep that behaviour.
- **A lat/lon swap is usually undetectable.** `[38.1, 21.3, 38.4, 21.9]` is a perfectly valid
  box in Iraq, not a malformed Gulf of Patras. Validation catches it only when a longitude
  exceeds 90 in a latitude slot. This is why every search echoes `area_searched` back — that
  echo is the actual safeguard, so never drop it. `tests/unit/test_geometry.py` pins the
  limitation so nobody later claims swaps are caught.

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

## Commits

[CONTRIBUTING.md](CONTRIBUTING.md) is authoritative. In short:

```
<type>(<scope>): <short summary>

<body: what and why, never how, wrapped at 72 columns>

<footer: Closes #123, Refs #456, BREAKING CHANGE:>
```

- Subject in the **imperative** ("Add", not "Added"), **50 characters or less**, no trailing
  period. It completes the sentence "If applied, this commit will…".
- Types: `feat`, `fix`, `refactor`, `docs`, `style`, `test`, `chore`, `perf`.
- **One logical change per commit.** If the subject needs the word "and", split it —
  `git add -p` stages part of a diff.
- No AI attribution in any trailer, per the rule at the top of this file.

## Style

- Python ≥3.11, `async` throughout, full type hints — `mypy` runs strict on `src/`.
- `ruff format`, 100-column lines.
- No bare `except`; catch specific exceptions and wrap them in a typed error from
  `errors.py`.
- Comment *why*, not *what*. Match the density of the surrounding code.

## First-class collections

All 419 CDSE collections work generically — anything in `/collections` is searchable. Four
are **first-class**: tuned vocabularies, recorded fixtures, worked examples. Specifics in
[docs/collections.md](docs/collections.md); vocabularies in `domain/collections.py`.

| Collection | Notes |
|---|---|
| `sentinel-2-l1c`, `sentinel-2-l2a` | **Build these first**, full-product download as the first working path. Gridded UTM JP2s, band enums, `eo:cloud_cover` |
| `sentinel-3-olci-2-wfr-*` | Water quality: chlorophyll, TSM, KD490, ADG443, plus per-pixel lat/lon. Swath NetCDF |
| `sentinel-3-olci-1-efr-*` | OLCI L1B top-of-atmosphere radiances. Swath NetCDF |

**Order of work**: Sentinel-2 discovery → Sentinel-2 full-product download → Sentinel-2
selective/band download → OLCI discovery → OLCI variable subsetting.

**Sentinel-3 breaks the Sentinel-2 mental model.** OLCI is swath data with a per-pixel
lat/lon array and no CRS or affine transform, stored as one NetCDF per variable. Gridded-raster
code applied to it fails *silently*, producing plausible output located nowhere. Read
[docs/collections.md](docs/collections.md) before writing any OLCI code.

Adding a collection here means fixtures recorded, vocabulary encoded, and one end-to-end
test. Do not expand the list casually — each entry is maintenance.

## Open decisions

Ask before assuming. Once decided they are recorded above and this section shrinks — it is
the list of things a session must not guess at.

- **Deferred:** CI. Mocked-only on push is the intended baseline and a scheduled live smoke
  test is a later question. No CI configuration is written yet — do not add workflows
  unprompted.
- Whether OLCI variable subsets are emitted as CSV, Parquet or NetCDF by default
- Whether OLCI chlorophyll and TSM are log10-scaled must be confirmed against a real file's
  CF attributes, not assumed — see the flagged item in [docs/collections.md](docs/collections.md)
