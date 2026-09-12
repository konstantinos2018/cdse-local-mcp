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
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.domain.models import JobState
from cdse_local_mcp.errors import AuthRequired, BudgetExceeded, NotFound, OfflineProduct
from cdse_local_mcp.tools import downloads
from cdse_local_mcp.transfer.budget import reset_budget
from cdse_local_mcp.transfer.jobs import JOURNAL_NAME, JobRegistry, reset_registry

UUID = "afbeb0a0-2d83-4a94-9eed-df076ec183d6"
PRODUCT_ID = "S2A_MSIL1C_20240731T092031_N0511_R093_T34SEH_20240731T112239"
PAYLOAD = b"sentinel" * 400
MD5 = hashlib.md5(PAYLOAD).hexdigest()


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
        "content_length": len(PAYLOAD),
        "checksum_value": MD5,
        "checksum_algorithm": "MD5",
        "online": True,
        "s3_path": "/eodata/Sentinel-2/MSI/L1C/x.SAFE",
    }
    return ProductInfo(**{**defaults, **overrides})  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def isolated_download_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Point the server at a throwaway download root with small caps."""
    monkeypatch.setenv("CDSE_CLIENT_ID", "test-client")
    monkeypatch.setenv("CDSE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("CDSE_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    monkeypatch.setenv("CDSE_MAX_CALL_BYTES", str(10 * 1024 * 1024))
    monkeypatch.setenv("CDSE_MAX_SESSION_BYTES", str(50 * 1024 * 1024))
    get_settings.cache_clear()
    reset_registry()
    reset_budget()
    yield
    downloads.set_odata(None)
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


async def test_a_download_runs_as_a_job_and_lands_on_disk() -> None:
    downloads.set_odata(FakeODataClient(_info()))  # type: ignore[arg-type]

    started = await downloads.download_product_archive(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    job_id = started.jobs[0].job_id

    # The tool returned immediately, without bytes in the response.
    assert started.jobs[0].state in {JobState.QUEUED, JobState.RUNNING}
    assert "Poll download_status" in " ".join(started.notes)

    await _run_to_completion(job_id)
    done = (await downloads.download_status(job_id=job_id)).jobs[0]

    assert done.state is JobState.COMPLETED
    assert done.path is not None
    written = Path(done.path)
    assert written.read_bytes() == PAYLOAD
    assert written.name.endswith(".zip")
    assert done.checksum_verified is True


async def test_an_offline_product_is_refused_before_any_transfer() -> None:
    fake = FakeODataClient(_info(online=False))
    downloads.set_odata(fake)  # type: ignore[arg-type]

    with pytest.raises(OfflineProduct) as exc:
        await downloads.download_product_archive(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )

    assert fake.streams_opened == 0
    assert "Data Workspace order" in (exc.value.hint or "")


async def test_an_oversized_product_needs_confirmation() -> None:
    downloads.set_odata(FakeODataClient(_info(content_length=900 * 1024 * 1024)))  # type: ignore[arg-type]

    with pytest.raises(BudgetExceeded) as exc:
        await downloads.download_product_archive(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )
    assert "confirm=True" in (exc.value.hint or "")


async def test_confirmation_allows_an_oversized_product() -> None:
    big = b"x" * (11 * 1024 * 1024)
    downloads.set_odata(  # type: ignore[arg-type]
        FakeODataClient(
            _info(content_length=len(big), checksum_value=None, checksum_algorithm=None), big
        )
    )

    started = await downloads.download_product_archive(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID, confirm=True
    )
    await _run_to_completion(started.jobs[0].job_id)

    assert (await downloads.download_status(job_id=started.jobs[0].job_id)).jobs[
        0
    ].state is JobState.COMPLETED


async def test_a_second_download_of_the_same_product_is_a_cache_hit() -> None:
    fake = FakeODataClient(_info())
    downloads.set_odata(fake)  # type: ignore[arg-type]

    first = await downloads.download_product_archive(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    await _run_to_completion(first.jobs[0].job_id)

    second = await downloads.download_product_archive(
        collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
    )
    await _run_to_completion(second.jobs[0].job_id)

    report = await downloads.download_status(job_id=second.jobs[0].job_id)
    assert report.jobs[0].cached is True
    assert fake.streams_opened == 1  # the second job transferred nothing


async def test_status_lists_recent_jobs_and_reports_the_session_budget() -> None:
    downloads.set_odata(FakeODataClient(_info()))  # type: ignore[arg-type]
    await downloads.download_product_archive(
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
    downloads.set_odata(FakeODataClient(_info()))  # type: ignore[arg-type]
    started = await downloads.download_product_archive(
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

    with pytest.raises(AuthRequired) as exc:
        await downloads.download_product_archive(
            collection="sentinel-2-l1c", product_id=PRODUCT_ID, odata_uuid=UUID
        )

    assert "CDSE_CLIENT_ID" in (exc.value.hint or "")
    assert fake.streams_opened == 0
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
