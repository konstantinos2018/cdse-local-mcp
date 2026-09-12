# CDSE API reference

**This file is the single source of truth for CDSE endpoints, identifiers and quotas in this
repository.** Do not write a CDSE URL, collection ID or quota number from memory — take it
from here, or fetch `https://documentation.dataspace.copernicus.eu/` and update this file in
the same change.

Verified against the official documentation on **2026-09-12**. Sources are linked per
section; re-check the Quotas page in particular, since the numbers move.

---

## Authentication

Token endpoint (Keycloak, `CDSE` realm):

```
https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token
```

This repo uses the **client credentials** grant exclusively:

```
POST  Content-Type: application/x-www-form-urlencoded
grant_type=client_credentials&client_id=<CDSE_CLIENT_ID>&client_secret=<CDSE_CLIENT_SECRET>
```

Response carries `access_token`, `expires_in`, `token_type: Bearer`. Use it as
`Authorization: Bearer <token>`.

The documentation also describes a `password` grant with `client_id=cdse-public` (plus a
`totp` parameter for 2FA accounts). **We deliberately do not support it** — it puts a
reusable account password in MCP client configuration files. The docs themselves warn:
"Please do not hardcode the username and password in the application code."

- Access token lifetime: **10 minutes**
- Refreshable within: **60 minutes** of generation
- Max active sessions per account: **100**

Source: [APIs/Token](https://documentation.dataspace.copernicus.eu/APIs/Token.html)

---

## STAC API — primary for search

```
Base:        https://stac.dataspace.copernicus.eu/v1/
Collections: /collections            /collections/{collectionId}
Items:       /collections/{collectionId}/items
Search:      /search                 (GET and POST with a JSON body)
Queryables:  /queryables             /collections/{collectionId}/queryables
```

Supported extensions: **Filter (CQL2)**, Query, Fields, Sort, Free-text search.

Search is unauthenticated. Prefer STAC for spatial/temporal queries, property filtering and
asset discovery.

### Collection IDs (confirmed subset)

| STAC collection ID | Contents |
|---|---|
| `sentinel-1-grd` | Sentinel-1 Ground Range Detected |
| `sentinel-2-l1c` | Sentinel-2 top-of-atmosphere |
| `sentinel-2-l2a` | Sentinel-2 surface reflectance |
| `sentinel-2-global-mosaics` | Quarterly global mosaics |
| `sentinel-3-olci-l1b` | OLCI Level-1B |
| `sentinel-5p-l2` | Sentinel-5P Level-2 atmospheric products |
| `copernicus-dem-cog` | Copernicus DEM, COG-tiled |
| `clms-*` | CLMS products, e.g. `clms-ba-global-300m-daily-v4`, `clms-ndvi-global-300m-10daily-v3` |

Call `/collections` at runtime rather than shipping an exhaustive hardcoded list; the table
above is for defaults and tests.

Source: [APIs/STAC](https://documentation.dataspace.copernicus.eu/APIs/STAC.html)

---

## OData API — product metadata and download

Two different hosts. Using the wrong one produces confusing failures.

```
Catalogue queries:  https://catalogue.dataspace.copernicus.eu/odata/v1/Products
Download / nodes:   https://download.dataspace.copernicus.eu/odata/v1/Products(<uuid>)
```

### Query shape

```
/Products?$filter=Collection/Name eq 'SENTINEL-2'
         and ContentDate/Start gt 2024-01-01T00:00:00.000Z
         and OData.CSC.Intersects(area=geography'SRID=4326;POLYGON((...))')
         &$orderby=ContentDate/Start desc
         &$top=20
         &$expand=Attributes
```

OData uses collection **names** (`SENTINEL-1`, `SENTINEL-2`, `SENTINEL-3`, `SENTINEL-5P`)
plus a `productType` attribute filter — a different identifier space from STAC collection
IDs. Map between them in `domain/collections.py`.

### Download endpoints

| Endpoint | Purpose |
|---|---|
| `/Products(<uuid>)/$value` | Whole product archive |
| `/Products(<uuid>)/Nodes` | List top-level nodes inside the product |
| `/Products(<uuid>)/Nodes(<name>)/Nodes(...)` | Traverse the product tree |
| `/Products(<uuid>)/Nodes(<path>)/$value` | **Selective download of one file** |
| `/Products(<uuid>)/Assets` | Quicklooks and thumbnails |

Node traversal is the mechanism that makes single-band download possible — ~100 MB instead
of a ~1 GB `.SAFE`. It is the default path in this server.

### Attributes that matter

- `S3Path` — location in the `eodata` bucket, format `/bucket/collection/path/to/product`;
  the handoff point to the S3 transport
- `ContentLength` — size in bytes, used for the pre-transfer estimate
- `Checksum` — MD5 / BLAKE3 for integrity verification
- `Online` — `false` means the product is on long-term archive and cannot be downloaded
  directly; it needs a Data Workspace order

Sources: [APIs/OData](https://documentation.dataspace.copernicus.eu/APIs/OData.html),
[OData basics notebook](https://documentation.dataspace.copernicus.eu/notebook-samples/geo/odata_basics.html)

---

## S3 API — primary download transport

```
Endpoint:          https://eodata.dataspace.copernicus.eu/
OTC-pinned:        https://eodata.ams.dataspace.copernicus.eu/
Bucket:            eodata
Key manager:       https://eodata-s3keysmanager.dataspace.copernicus.eu/
```

Keys are created in the key manager with an expiry date; **the secret is shown once**.
Object keys come from the product's `S3Path` attribute, e.g.
`s3://eodata/Sentinel-2/MSI/L2A/2024/01/15/<product>.SAFE/`.

Preferred for anything over ~200 MB or spanning multiple files: long-lived credentials (so a
transfer cannot die on a 10-minute token expiry), ranged GETs, and parallel reads inside the
4-connection ceiling.

Gotchas:

- Recursive listing/copying counts **each file** against the request quota — an `.SAFE`
  directory holds hundreds of objects.
- Temporary credentials from the credentials API need ~5 s to propagate before first use.

Source: [APIs/S3](https://documentation.dataspace.copernicus.eu/APIs/S3.html)

---

## Quotas and limits (free tier)

These are the constraints the server is built around. They reset on the first day of the
month unless noted.

| Limit | Value |
|---|---|
| OData / catalogue requests | 10 000 per month |
| OData request rate | 2 000 per minute |
| Data transfer | up to 12 TB per rolling 30 days |
| **Concurrent connections** | **4** |
| Bandwidth per connection | 20 MB/s |
| Active sessions per account | 100 |
| Access token lifetime | 10 minutes (refreshable within 60) |
| Data Workspace (offline data) | 25 products/month, 1 active order, 0.1 TB transfer |

For reference, not used in v1: Sentinel Hub 50 000 requests/month, 300 req/min, 10 000
processing units/month, 300 PU/min; openEO 10 000 credits/month, 12 req/min synchronous,
2 concurrent requests, 2 simultaneous batch jobs.

Commercial tiers exist with higher ceilings; assume the free tier.

Source: [Quotas](https://documentation.dataspace.copernicus.eu/Quotas.html)

---

## Out of scope for v1

Recorded so they are not rediscovered, and so nobody wires them in by accident:

| Service | Host |
|---|---|
| Sentinel Hub (Process / Statistical / Catalog) | `sh.dataspace.copernicus.eu` |
| Sentinel Hub dashboard (OAuth clients) | `shapps.dataspace.copernicus.eu/dashboard/` |
| openEO | `openeo.dataspace.copernicus.eu` |
| On-Demand Production, Traceability, Subscriptions | see [APIs overview](https://documentation.dataspace.copernicus.eu/APIs.html) |
