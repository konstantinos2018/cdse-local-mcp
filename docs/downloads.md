# Download architecture

Why downloading EO data through an MCP server is harder than it looks, and the design that
handles it. Read this before touching anything under `transfer/` or `tools/downloads.py`.

The short version: **the MCP channel moves descriptions of data, the filesystem moves data.**
Every rule below follows from that.

---

## The five constraints

### 1. The MCP channel cannot carry bytes

Tool results are JSON-RPC frames over stdio, and they land in the model's context window.
Binary must be base64-encoded, inflating it by a third. The scale mismatch is not marginal:

| Thing | Size |
|---|---|
| Quicklook JPEG | ~100 KB |
| One Sentinel-2 10 m band (JP2) | ~100 MB |
| Sentinel-2 L2A `.SAFE` archive | ~800 MB – 1 GB |
| Sentinel-1 GRD | ~1 – 8 GB |

**Design.** Tool results carry `path`, `size_bytes`, `checksum`, `content_type` and product
metadata. Never payloads. Completed files are also exposed as MCP **resources** with
`file://` URIs, so a client can reference or read them on its own terms instead of having
them forced into context.

The single exception: a visual asset smaller than `INLINE_ASSET_MAX_BYTES` (200 KB) may be
returned as image content — after an explicit size check, never optimistically.

### 2. Tool calls are short-lived; transfers are not

MCP clients commonly time out a tool call around 60 s. At the CDSE ceiling of 20 MB/s per
connection, a 1 GB product needs ≥50 s in ideal conditions and minutes in practice. An
offline product needs an order and can take hours. A synchronous download tool is therefore
broken by construction.

**Design — downloads are jobs.**

| Tool | Behaviour |
|---|---|
| `download_assets` | Validates, resolves transport, estimates size, enqueues, returns `job_id` immediately |
| `download_status` | Returns state, bytes transferred, ETA, completed paths, errors |
| `download_cancel` | Cancels a queued or running job; partial `.part` files are retained for resume |

Progress is additionally reported in-band through FastMCP's `Context.report_progress()` and
`ctx.info()` for clients that render it.

Job state lives in a **journal** — a JSON file under the download root, written atomically
after each state change. It carries job records and the cumulative byte counter. A restarted
server reads it and can report honestly on what happened and resume partial transfers,
rather than losing the work silently.

### 3. Token lifetime is shorter than a large transfer

CDSE access tokens are valid for **10 minutes**. A single authenticated `$value` transfer of
a multi-gigabyte product will get a 401 mid-flight. S3 access keys, by contrast, are
long-lived.

**Design — S3 first, OData as fallback.**

| Situation | Transport |
|---|---|
| Specific files inside a product, or >200 MB, and S3 keys configured | **S3**: resolve the product's `S3Path`, ranged parallel GETs from the `eodata` bucket |
| No S3 keys configured | **OData node traversal**: `Products(<uuid>)/Nodes(<path>)/$value` |
| Whole product, explicitly requested | **OData** `Products(<uuid>)/$value`, resumable via `Range` |
| Quicklook / thumbnail | **OData** `Products(<uuid>)/Assets`, inline if under the size cap |

Exactly one `TokenProvider` exists, refreshing ahead of expiry behind an `asyncio.Lock`. No
tool or client body mints a token inline. Long OData transfers re-authenticate on retry
rather than assuming the initial token survives.

### 4. Quota is the scarce resource, and the model cannot see it

Free tier: **4 concurrent connections**, 20 MB/s each, ~12 TB per rolling 30 days, 10 000
OData requests/month. An agent in a loop will exhaust a month of quota without any local
symptom.

**Design — one chokepoint, `transfer/budget.py`.**

- A process-wide `asyncio.Semaphore(4)` that **every** byte-moving request acquires. No
  ad-hoc `httpx.stream()` or `boto3` call anywhere else in the codebase.
- A per-session byte budget, persisted in the journal, so the accounting is real rather than
  per-call.
- A per-call size cap. `download_assets` estimates total bytes from `ContentLength` *before*
  enqueueing and refuses anything above the cap unless the caller passes `confirm=True`.
  The refusal names the size and the cap so the model can narrow its request.
- Retries are bounded with exponential backoff and jitter, honouring `Retry-After`. Never
  poll in a tight loop — polling spends the 10 000-request budget too.

### 5. Whole-product download is almost always wrong

A user asking "get me the NDVI bands for this field in June" needs two 100 MB JP2s, not two
1 GB archives. The agent will pick the blunt instrument if it is the more obvious one.

**Design — selective download is the default path.**

- `list_product_assets` (OData `Nodes()` traversal, or STAC assets) is the discovery step and
  is cheap; its docstring points the model here before any transfer.
- `download_assets` takes an explicit list of asset/node paths. Band selection is the normal
  case.
- `download_product_archive` is a **separate** tool for whole archives, documented as
  expensive, so it cannot be reached by accident or by filling in a default.
- No tool accepts "download everything matching this search". Product identifiers must be
  explicit.

---

## Filesystem safety

The server writes to the user's disk on an LLM's instruction. Treat every path as hostile.

- **Sandbox.** One download root from `CDSE_DOWNLOAD_DIR` (default
  `~/.cache/copernicus-dataspace-mcp`). Every candidate path is `resolve()`d and validated
  with `is_relative_to(root)`. Reject `..` segments, absolute paths, and symlinks that escape
  the root. Product-derived names are sanitised — they come from a remote API, not from us.
- **Atomic writes.** Stream to `<name>.part`, `fsync`, then `rename` into place. A reader
  never sees a partial file, and an interrupted transfer leaves a resumable remnant.
- **Resume.** Continue from the `.part` length with an HTTP `Range` header or a ranged S3
  GET. Verify the server honoured the range before appending.
- **Verify.** Check `ContentLength` and the `Checksum` attribute (MD5/BLAKE3) on completion.
  On mismatch, delete the file and report failure — never hand back a corrupt path.
- **Idempotence.** If the destination already exists with a matching checksum, return it as a
  cache hit with `cached: true` and transfer nothing.
- **Disk headroom.** Check available space against the estimate before enqueueing; fail early
  with a clear message rather than filling the user's disk.
- **No cleanup tool by default.** If one is ever added it is dry-run by default, confined to
  the download root, and never recursive-by-glob.

## Offline / long-term archive products

Products with `Online: false` are on tape and cannot be streamed. Check the attribute
**before** enqueueing, and return an actionable result:

> Product `<id>` is offline (long-term archive) and cannot be downloaded directly. It
> requires a Data Workspace order. Free-tier limits: 25 products/month, one active order at
> a time, 0.1 TB transfer.

Do not enqueue a job that will fail, and do not silently retry.

## Tool annotations

| Tool | Annotations |
|---|---|
| `search_products`, `get_product`, `list_product_assets`, `download_status` | `readOnlyHint=True`, `openWorldHint=True` |
| `download_assets`, `download_product_archive` | `readOnlyHint=False`, `destructiveHint=False`, `idempotentHint=True`, `openWorldHint=True` |
| `download_cancel` | `readOnlyHint=False`, `destructiveHint=False`, `idempotentHint=True` |

## Required tests

Download code is where quiet data corruption and quota burn live. `tests/unit/` must cover:

- path traversal attempts (`..`, absolute paths, escaping symlink, hostile product names)
- checksum mismatch → file deleted, failure reported
- resume from a `.part` file, including a server that ignores `Range`
- budget refusal above the per-call cap, and `confirm=True` overriding it
- semaphore ceiling actually capping concurrency at 4
- cancellation mid-transfer leaving a resumable remnant and a consistent journal
- `Online: false` short-circuiting before any transfer
- cache hit on an existing verified file
