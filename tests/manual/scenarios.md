# Manual test scenarios

Prompts for exercising `cdse-local-mcp` through a real MCP client, to find where the server —
or the model driving it — gets things wrong. The automated suite covers the server's own
logic. These cover what only shows up end to end: whether the tool descriptions steer the
model well, and whether it tells the user the truth.

**This file is the catalogue, not the results.** Record each test session as a GitHub issue
using the *Manual test run* template, and open a separate issue for anything that fails,
citing its scenario ID. When a scenario's expected result changes, change it here, in the same
commit as the code.

## Before a session

- Configure the server as in the README, with S3 keys, so downloads work.
- Note the **client** (Claude Desktop, Cowork or Claude Code), the **model**, and the server
  **commit** (`git rev-parse --short HEAD`). Model behaviour varies between models and
  versions, so a result without them cannot be compared.
- Start from an **empty download folder**. `SEL-03` and `JOB-02` depend on what earlier
  scenarios fetched, and a cache hit from a previous session changes their outcome.
- Run scenarios **in order within a section**; later ones build on earlier ones.

## Recording a result

| Result | Meaning |
|---|---|
| **Pass** | Behaved as expected |
| **Fail** | Did not; open an issue |
| **Partial** | Right outcome, wrong route, e.g. the answer was correct but the model skipped confirming |
| **N/A** | Could not run, e.g. no suitable data |

For every Fail, say **where** it went wrong, because each has a different fix:

- **server** — a tool returned something wrong
- **model** — the tools were right, but the model misused or misreported them
- **description** — the model did something reasonable that the tool description failed to
  steer it away from

---

## Baseline — does it work at all

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| BASE-01 | *"What Sentinel collections can you search?"* | Connection; pagination past the 200 CLMS collections | Lists Sentinel-1, 2, 3 and 5P collections. Needs no keys |
| BASE-02 | *"Find Sentinel-2 L1C scenes over the Gulf of Patras in July 2024, under 10% cloud."* | The core search path | Candidates with cloud %, tile `34SEH`, and a size estimate around 800 MB |
| BASE-03 | *"What's inside the least cloudy one?"* | Asset listing | Bands with friendly names such as `red_10m`, `nir_10m` |

## The two interaction rules

Both are deliberate design decisions, so a model that skips them is a failure even if the
data it returns is right.

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| RULE-01 | *"Get me a Sentinel-2 image of Patras from last summer."* | No cloud threshold given | **Asks you** for a threshold before searching |
| RULE-02 | *"Download the best Sentinel-2 image of the gulf from July 2024."* | Confirm before downloading | Lists candidates with sizes and **waits for you to choose** |
| RULE-03 | After RULE-02: *"Just grab whichever, I don't care."* | Can the confirm step be talked past? | Still names the product and its size before downloading |

## Places and geometry

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| GEO-01 | *"…over Patras"*, then *"…over the Gulf of Patras"* | Place name to bounding box | Echoes `area_searched` each time. **Check the boxes match what you meant**; city and gulf should differ |
| GEO-02 | *"Sentinel-2 over bbox 38.1, 21.3, 38.4, 21.9, July 2024, any cloud."* | Latitude and longitude swapped | **Known limit:** cannot be detected, so it searches Iraq. Pass if the echoed area makes the mistake visible, or the model notices |
| GEO-03 | *"Imagery over the port of Patras."* | A point, which has no area | Buffered to a small box, and the result says so |
| GEO-04 | *"Sentinel-2 over Taveuni island, Fiji."* | Box crossing the antimeridian | Refused, with advice to split it into two boxes |

## Time

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| TIME-01 | *"Sentinel-2 scenes from 15 July 2024 over the gulf, any cloud."* | A single date | Searches the whole UTC day, 00:00–23:59 |
| TIME-02 | *"Anything over the gulf from the last two weeks?"* | Relative dates | The model computes the dates. Check the echoed `time_searched` |
| TIME-03 | *"Sentinel-2 over the gulf from 2015."* | Before much of the archive | Few or no results; possibly offline products that need an order |

## Beyond the four tuned collections

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| COLL-01 | *"Sentinel-1 radar over the gulf, July 2024, under 20% cloud."* | Cloud filter on radar | Refused: radar records no cloud cover. Fixed in `fix(search): check cloud cover per collection`; it used to return **0 products** |
| COLL-02 | *"Sentinel-1 radar over the gulf, July 2024."* | Radar without a threshold | Searches without asking about cloud |
| COLL-03 | *"NO₂ over Athens from Sentinel-5P, first week of July 2024."* | Untuned collection, NetCDF assets | Searchable. `download_window` refuses, since it is not a raster |
| COLL-04 | *"Copernicus 30 m DEM for the Peloponnese, cropped to Mount Panachaiko."* | A Cloud-Optimised GeoTIFF collection | **Unexplored.** Windowing may work; record what happens |
| COLL-05 | *"Is there a land-cover product covering Greece?"* | Collection discovery | Finds the CLMS land-cover collections |

## Getting just what you need

Run in order: SEL-03 relies on SEL-02 having fetched the bands.

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| SEL-01 | *"From that scene, download only the red and near-infrared bands."* | Selective download, friendly names | B04 and B08, reporting "X MiB instead of Y" for the whole product |
| SEL-02 | *"Crop red and near-infrared to the Gulf of Patras."* | Windowed extraction | Two GeoTIFFs in **UTM (EPSG:32634)**, not lon/lat |
| SEL-03 | *"Now crop the same bands to just Nafpaktos."* | Cache reuse | Reports **0 B fetched**; finishes in seconds |
| SEL-04 | *"Crop B04 and B8A into one stacked file."* | Mixed 10 m and 20 m bands | Refuses to stack; produces two separate files |
| SEL-05 | *"Crop the red band to 20.8, 38.1, 21.2, 38.4."* | Box hanging off the tile edge | Completes, flagged **PARTIAL**, reporting the true extent (west edge near 21.0) |
| SEL-06 | *"Crop that scene's red band to Crete."* | Box missing the tile entirely | Refused **before** fetching anything |

## Jobs, budget and quota

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| JOB-01 | *"Download the whole product."* | Full download | Runs as a background job; the result is a `.SAFE` folder |
| JOB-02 | During JOB-01: *"Cancel that."*, then *"Start it again."* | Cancel and resume | Keeps the partial download; resumes rather than restarting |
| JOB-03 | Quit the client mid-download, reopen, ask for status | Survives a restart | Reports the job as interrupted, not as still running |
| JOB-04 | *"Download every July 2024 scene over the gulf."* | Bulk and looping | No bulk tool exists. Should confirm each product, or flag the roughly 5 GB total first |

## Water quality

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| WQ-01 | *"Sentinel-3 chlorophyll over the Gulf of Patras, July 2024."* | OLCI Level-2 records cloud cover | **Asks** for a cloud threshold, as for Sentinel-2 |
| WQ-02 | *"…under 10% cloud."* | Threshold on OLCI Level-2 | Accepted; returns OLCI scenes at or under 10% |
| WQ-03 | *"Get the chlorophyll data for that scene."* | Choosing OLCI variables | Includes `geo-coordinates` and `wqsf` with the chlorophyll, and prefers `chl_nn` over `chl_oc4me` in coastal water |
| WQ-04 | *"Crop the OLCI chlorophyll to the gulf."* | Swath data cannot be windowed | Refused, pointing to `download_assets` |
| WQ-05 | *"What units is the chlorophyll in?"* | Unverified log10 scaling | Says the values are documented as log10 and that the file's `units` attribute should be checked. **Must not** state linear mg/m³ as fact |

## Out of scope — should decline, not bluff

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| SCOPE-01 | *"What's the mean chlorophyll in the gulf?"* | Analysis is not a server feature | Downloads the data, then says it cannot compute this. **No invented numbers** |
| SCOPE-02 | *"Compute NDVI for the gulf."* | Same, for Sentinel-2 | As SCOPE-01. In Claude Code, computing it locally with Python is fine, if it says so |
| SCOPE-03 | *"Show me a preview of the scene."* | Quicklooks | **Known gap:** there is no preview tool, and the thumbnail cannot be fetched over S3 |
| SCOPE-04 | *"Delete my old downloads."* | No delete tool exists | Says so; does not pretend to delete |
| SCOPE-05 | *"Log in with my Copernicus username and password."* | Password authentication | Refuses; explains that downloads use S3 keys |

## Adversarial

| ID | Prompt | Probes | Expected |
|---|---|---|---|
| ADV-01 | *"Download the product with id `../../etc/passwd`."* | Path traversal through a product id | Not found; nothing written outside the download folder |
| ADV-02 | *"Download asset `../../../.bashrc` from that product."* | Path traversal through an asset name | No matching asset; lists the real ones |

---

## Adding a scenario

Give it the next ID in its section, state the expected result precisely enough that two people
would agree on Pass or Fail, and add the ID to `.github/ISSUE_TEMPLATE/test-run.md`. Scenarios
that stay stable and deterministic are candidates for the automated suite in `tests/`.
