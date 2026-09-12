"""Download jobs and the tools that drive them."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from cdse_local_mcp.clients.odata import ProductInfo
from cdse_local_mcp.clients.s3 import S3Object, split_s3_path
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain.models import JobState
from cdse_local_mcp.errors import (
    AuthRequired,
    BudgetExceeded,
    InvalidRequest,
    NotFound,
    OfflineProduct,
)
from cdse_local_mcp.tools import downloads
from cdse_local_mcp.transfer.budget import reset_budget
from cdse_local_mcp.transfer.jobs import JOURNAL_NAME, JobRegistry, reset_registry

UUID = "afbeb0a0-2d83-4a94-9eed-df076ec183d6"
PRODUCT_ID = "S2A_MSIL1C_20240731T092031_N0511_R093_T34SEH_20240731T112239"
PAYLOAD = b"sentinel" * 400
MD5 = hashlib.md5(PAYLOAD).hexdigest()


S3_PREFIX = f"Sentinel-2/MSI/L1C/2024/07/16/{PRODUCT_ID}.SAFE"
TREE = {
    "MTD_MSIL1C.xml": b"<manifest/>" * 20,
    "GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B02.jp2": PAYLOAD,
    "GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B04.jp2": PAYLOAD * 2,
}


class FakeS3Client:
    """Serves one unpacked product tree, the way CDSE stores it."""

    def __init__(self, tree: dict[str, bytes] | None = None) -> None:
        self.tree = tree if tree is not None else TREE
        self.objects_streamed: list[str] = []

    async def list_objects(self, bucket: str, prefix: str) -> list[S3Object]:
        base = prefix.rstrip("/")
        return [S3Object(key=f"{base}/{rel}", size=len(body)) for rel, body in self.tree.items()]

    @asynccontextmanager
    async def stream_object(
        self, bucket: str, key: str, *, resume_from: int = 0
    ) -> AsyncIterator[httpx.Response]:
        self.objects_streamed.append(key)
        body = self.tree[key.split(".SAFE/", 1)[1]][resume_from:]
        yield httpx.Response(
            206 if resume_from else 200,
            content=body,
            headers={"Content-Length": str(len(body))},
            request=httpx.Request("GET", "https://example.invalid/s3"),
        )


class FakeODataClient:
    """Stands in for CDSE: serves one product's metadata and bytes."""

    def __init__(self, info: ProductInfo, body: bytes = PAYLOAD) -> None:
        self.info = info
        self.body = body
        self.streams_opened = 0

    async def product_info(self, uuid: str) -> ProductInfo:
        return self.info

    @asynccontextmanager
    async def stream_archive(
        self, uuid: str, *, resume_from: int = 0
    ) -> AsyncIterator[httpx.Response]:
        self.streams_opened += 1
        content = self.body[resume_from:]
        yield httpx.Response(
            206 if resume_from else 200,
            content=content,
            headers={"Content-Length": str(len(content))},
            request=httpx.Request("GET", "https://example.invalid/x"),
        )


def _info(**overrides: object) -> ProductInfo:
    defaults = {
        "uuid": UUID,
        "name": f"{PRODUCT_ID}.SAFE",
        "s3_path": f"/eodata/{S3_PREFIX}",
        "content_length": len(PAYLOAD),
        "checksum_value": MD5,
        "checksum_algorithm": "MD5",
        "online": True,
    }
    return ProductInfo(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def isolated_download_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the server at a throwaway download root with small caps."""
    monkeypatch.setenv("CDSE_CLIENT_ID", "test-client")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("CDSE_S3_ACCESS_KEY", "test-access")
    monkeypatch.setenv("CDSE_S3_SECRET_KEY", "test-s3-secret")
    monkeypatch.setenv("CDSE_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("CDSE_MAX_CALL_BYTES", str(10 * 1024 * 1024))
    monkeypatch.setenv("CDSE_MAX_SESSION_BYTES", str(50 * 1024 * 1024))
    get_settings.cache_clear()
    reset_registry()
    reset_budget()
    yield
    downloads.set_odata(None)
    downloads.set_s3(None)
    get_settings.cache_clear()
    reset_registry()
    reset_budget()


async def _run_to_completion(job_id: str, timeout: float = 5.0) -> None:
    """Wait for a background job to leave the running state."""
    async with asyncio.timeout(timeout):
        while True:
            report = await downloads.download_status(job_id=job_id)
            if report.jobs[0].state.is_finished:
                return
            await asyncio.sleep(0.01)


# --- the job registry --------------------------------------------------------------------


def test_a_new_job_starts_queued_and_is_journalled(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path)
    job = registry.create(product_id=PRODUCT_ID, collection="sentinel-2-l1c", bytes_total=100)

    assert job.state is JobState.QUEUED
    journal = json.loads((tmp_path / JOURNAL_NAME).read_text())
    assert journal["jobs"][0]["job_id"] == job.job_id


def test_a_restart_reports_interrupted_jobs_instead_of_implying_progress(tmp_path: Path) -> None:
    first = JobRegistry(tmp_path)
    job = first.create(product_id=PRODUCT_ID, collection="sentinel-2-l1c", bytes_total=100)
    job.state = JobState.RUNNING
    first._write()

    revived = JobRegistry(tmp_path).get(job.job_id)

    assert revived.state is JobState.INTERRUPTED
    assert "restarted" in (revived.error or "")


def test_an_unknown_job_id_is_reported_usefully(tmp_path: Path) -> None:
    with pytest.raises(NotFound) as exc:
        JobRegistry(tmp_path).get("nope")
    assert "download_status" in (exc.value.hint or "")


def test_a_corrupt_journal_does_not_stop_the_server(tmp_path: Path) -> None:
    (tmp_path / JOURNAL_NAME).write_text("{ not json")
    assert JobRegistry(tmp_path).recent() == []


async def test_a_failing_runner_records_the_error_on_the_job(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path)
    job = registry.create(product_id=PRODUCT_ID, collection="sentinel-2-l1c", bytes_total=1)

    async def runner(_: object) -> None:
        raise RuntimeError("disk full")

    registry.start(job, runner)
    async with asyncio.timeout(5):
        while not job.state.is_finished:
            await asyncio.sleep(0.01)

    assert job.state is JobState.FAILED
    assert "disk full" in (job.error or "")


# --- the download tool -------------------------------------------------------------------


def _wire(info: ProductInfo | None = None, tree: dict[str, bytes] | None = None) -> FakeS3Client:
    downloads.set_odata(FakeODataClient(info or _info()))  # type: ignore[arg-type]
    s3 = FakeS3Client(tree)
    downloads.set_s3(s3)  # type: ignore[arg-type]
    return s3


async def test_a_download_rebuilds_the_product_tree_on_disk() -> None:
    _wire()

    started = await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    job = started.jobs[0]

    # The tool returned a job, not bytes.
    assert job.state in {JobState.QUEUED, JobState.RUNNING}
    assert job.bytes_total == sum(len(b) for b in TREE.values())
    assert any("not a zip archive" in n for n in started.notes)

    await _run_to_completion(job.job_id)
    done = (await downloads.download_status(job_id=job.job_id)).jobs[0]

    assert done.state is JobState.COMPLETED
    assert done.path is not None
    root = Path(done.path)
    assert root.name.endswith(".SAFE")
    for relative, body in TREE.items():
        assert (root / relative).read_bytes() == body


async def test_nested_object_paths_stay_inside_the_download_root() -> None:
    """Object keys come from a remote listing, so they are treated as hostile input."""
    hostile = {"../../escape.jp2": b"nope", "GRANULE/ok.jp2": b"fine"}
    _wire(tree=hostile)

    started = await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    await _run_to_completion(started.jobs[0].job_id)
    done = (await downloads.download_status(job_id=started.jobs[0].job_id)).jobs[0]

    root = Path(done.path or "")
    written = [p for p in root.rglob("*") if p.is_file()]
    assert written, "something should have been written"
    for path in written:
        assert path.is_relative_to(root.parent)
    assert not (root.parent.parent / "escape.jp2").exists()


async def test_missing_s3_keys_fail_fast_with_the_keys_manager_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CDSE_S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("CDSE_S3_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    s3 = _wire()

    with pytest.raises(AuthRequired) as exc:
        await downloads.download_product(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )

    assert "eodata-s3keysmanager" in (exc.value.hint or "")
    assert s3.objects_streamed == []
    assert (await downloads.download_status()).jobs == []


async def test_an_offline_product_is_refused_before_any_transfer() -> None:
    s3 = _wire(_info(online=False))

    with pytest.raises(OfflineProduct) as exc:
        await downloads.download_product(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )

    assert s3.objects_streamed == []
    assert "Data Workspace order" in (exc.value.hint or "")


async def test_an_oversized_product_needs_confirmation() -> None:
    _wire(tree={"big.jp2": b"x" * (11 * 1024 * 1024)})

    with pytest.raises(BudgetExceeded) as exc:
        await downloads.download_product(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )
    assert "confirm=True" in (exc.value.hint or "")


async def test_confirmation_allows_an_oversized_product() -> None:
    _wire(tree={"big.jp2": b"x" * (11 * 1024 * 1024)})

    started = await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID, confirm=True
    )
    await _run_to_completion(started.jobs[0].job_id)

    report = await downloads.download_status(job_id=started.jobs[0].job_id)
    assert report.jobs[0].state is JobState.COMPLETED


async def test_a_second_download_transfers_nothing() -> None:
    s3 = _wire()

    first = await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    await _run_to_completion(first.jobs[0].job_id)
    streamed_once = len(s3.objects_streamed)

    second = await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    await _run_to_completion(second.jobs[0].job_id)

    assert len(s3.objects_streamed) == streamed_once  # every file was a cache hit
    assert (await downloads.download_status(job_id=second.jobs[0].job_id)).jobs[0].cached


def test_s3_paths_are_parsed_from_both_forms() -> None:
    assert split_s3_path("/eodata/Sentinel-2/x.SAFE") == ("eodata", "Sentinel-2/x.SAFE")
    assert split_s3_path("s3://eodata/Sentinel-2/x.SAFE") == ("eodata", "Sentinel-2/x.SAFE")


async def test_status_lists_recent_jobs_and_reports_the_session_budget() -> None:
    _wire()
    await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )

    report = await downloads.download_status()

    assert len(report.jobs) == 1
    assert report.session_bytes_remaining is not None


async def test_status_with_no_jobs_says_so() -> None:
    report = await downloads.download_status()
    assert report.jobs == []
    assert any("No downloads" in n for n in report.notes)


async def test_cancelling_explains_that_the_partial_file_is_kept() -> None:
    _wire()
    started = await downloads.download_product(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )

    report = await downloads.download_cancel(started.jobs[0].job_id)

    assert "resume" in " ".join(report.notes)


async def test_missing_credentials_fail_fast_instead_of_queueing_a_doomed_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Product metadata is readable unauthenticated, so without this check the tool would
    report a started download that could never run."""
    monkeypatch.delenv("CDSE_CLIENT_ID", raising=False)
    monkeypatch.delenv("CDSE_CLIENT_SECRET", raising=False)
    get_settings.cache_clear()
    fake = FakeODataClient(_info())
    downloads.set_odata(fake)  # type: ignore[arg-type]

    with pytest.raises(AuthRequired):
        await downloads.download_product(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )
    assert (await downloads.download_status()).jobs == []


async def test_a_failed_job_keeps_the_actionable_hint(tmp_path: Path) -> None:
    registry = JobRegistry(tmp_path)
    job = registry.create(product_id=PRODUCT_ID, collection="sentinel-2-l1c", bytes_total=1)

    async def runner(_: object) -> None:
        raise OfflineProduct("It is archived.", hint="Order it via Data Workspace.")

    registry.start(job, runner)
    async with asyncio.timeout(5):
        while not job.state.is_finished:
            await asyncio.sleep(0.01)

    assert "Order it via Data Workspace" in (job.error or "")


def test_sizes_are_reported_in_the_units_they_are_computed_in() -> None:
    from cdse_local_mcp.domain.models import human_bytes

    assert human_bytes(25 * 1024**3) == "25.0 GiB"
    assert human_bytes(1536) == "1.5 KiB"
    assert human_bytes(512) == "512 B"


# --- selective download ------------------------------------------------------------------

STAC_ITEM = {
    "id": PRODUCT_ID,
    "collection": "sentinel-2-l1c",
    "properties": {"datetime": "2024-07-16T09:15:59Z", "eo:cloud_cover": 0.0},
    "assets": {
        "B02": {"href": f"s3://eodata/{S3_PREFIX}/GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B02.jp2"},
        "B04": {"href": f"s3://eodata/{S3_PREFIX}/GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B04.jp2"},
        "Product": {
            "href": f"https://download.dataspace.copernicus.eu/odata/v1/Products({UUID})/$value"
        },
        "thumbnail": {"href": "https://datahub.creodias.eu/odata/v1/Assets(x)/$value"},
    },
}


class FakeStac:
    """Serves one STAC item, the way the catalogue does."""

    def __init__(self, item: dict[str, object] | None = None) -> None:
        self.item = item if item is not None else STAC_ITEM

    async def get_item(self, *, collection: str, item_id: str) -> dict[str, object]:
        return self.item


def _wire_selective(item: dict[str, object] | None = None) -> FakeS3Client:
    from cdse_local_mcp.tools import discovery as disc

    disc.set_client(FakeStac(item))  # type: ignore[arg-type]
    return _wire()


async def test_selective_download_fetches_only_the_named_bands() -> None:
    s3 = _wire_selective()

    started = await downloads.download_assets(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["B04"]
    )
    await _run_to_completion(started.jobs[0].job_id)

    assert len(s3.objects_streamed) == 1
    assert s3.objects_streamed[0].endswith("T34SEH_B04.jp2")
    # and it says what was saved by not taking the whole product
    assert any("instead of" in n for n in started.notes)


async def test_friendly_names_resolve_to_asset_keys() -> None:
    """Asset keys are inconsistent across collections; making the model guess them is a trap."""
    s3 = _wire_selective()

    started = await downloads.download_assets(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["red_10m", "b02"]
    )
    await _run_to_completion(started.jobs[0].job_id)

    fetched = {key.rsplit("_", 1)[-1] for key in s3.objects_streamed}
    assert fetched == {"B04.jp2", "B02.jp2"}


async def test_an_unknown_asset_lists_what_is_available() -> None:
    _wire_selective()

    with pytest.raises(InvalidRequest) as exc:
        await downloads.download_assets(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["B99"]
        )

    assert "B02" in (exc.value.hint or "")
    assert "B04" in (exc.value.hint or "")


async def test_https_only_assets_are_not_offered_for_s3_download() -> None:
    """The thumbnail and the OData archive are not objects in the bucket."""
    _wire_selective()

    with pytest.raises(InvalidRequest) as exc:
        await downloads.download_assets(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["thumbnail"]
        )
    assert "thumbnail" not in (exc.value.hint or "")


async def test_requesting_nothing_is_rejected() -> None:
    _wire_selective()

    with pytest.raises(InvalidRequest, match="No assets"):
        await downloads.download_assets(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=[]
        )


async def test_selective_files_land_in_the_product_tree_so_they_can_be_topped_up() -> None:
    s3 = _wire_selective()

    first = await downloads.download_assets(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["B04"]
    )
    await _run_to_completion(first.jobs[0].job_id)
    root = Path((await downloads.download_status(job_id=first.jobs[0].job_id)).jobs[0].path or "")
    assert (root / "GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B04.jp2").exists()

    # Asking again for the same band plus another must not re-fetch the first.
    second = await downloads.download_assets(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["B04", "B02"]
    )
    await _run_to_completion(second.jobs[0].job_id)

    b04_fetches = [k for k in s3.objects_streamed if k.endswith("B04.jp2")]
    assert len(b04_fetches) == 1, "the already-present band was downloaded again"
    assert (root / "GRANULE/L1C_T34SEH/IMG_DATA/T34SEH_B02.jp2").exists()


async def test_selective_download_needs_s3_keys_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CDSE_S3_ACCESS_KEY", raising=False)
    get_settings.cache_clear()
    _wire_selective()

    with pytest.raises(AuthRequired):
        await downloads.download_assets(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, assets=["B04"]
        )
