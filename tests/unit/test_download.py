"""Transfer mechanics: resume, verification, atomicity, cancellation.

This is where silent data corruption lives, so the unhappy paths carry the weight.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import TransferFailed
from cdse_local_mcp.transfer.budget import TransferBudget
from cdse_local_mcp.transfer.download import download_stream
from cdse_local_mcp.transfer.paths import partial_path

PAYLOAD = b"copernicus" * 500
MD5 = hashlib.md5(PAYLOAD).hexdigest()


@pytest.fixture
def budget() -> TransferBudget:
    return TransferBudget(Settings())


def _streamer(
    body: bytes,
    *,
    status: int = 200,
    honour_range: bool = True,
    record: list[int] | None = None,
):
    """Build an open_stream callable backed by an in-memory body."""

    @asynccontextmanager
    async def open_stream(start: int) -> AsyncIterator[httpx.Response]:
        if record is not None:
            record.append(start)
        if start and honour_range:
            content, code = body[start:], 206
        else:
            content, code = body, status
        response = httpx.Response(
            code,
            content=content,
            headers={"Content-Length": str(len(content))},
            request=httpx.Request("GET", "https://example.invalid/product"),
        )
        yield response

    return open_stream


async def test_a_complete_download_is_atomic_and_verified(
    tmp_path: Path, budget: TransferBudget
) -> None:
    target = tmp_path / "product.zip"

    result = await download_stream(
        open_stream=_streamer(PAYLOAD),
        target=target,
        budget=budget,
        expected_size=len(PAYLOAD),
        checksum=MD5,
        checksum_algorithm="MD5",
    )

    assert target.read_bytes() == PAYLOAD
    assert result.checksum_verified is True
    assert result.cached is False
    assert not partial_path(target).exists()
    assert budget.spent_bytes == len(PAYLOAD)


async def test_an_existing_verified_file_is_a_cache_hit(
    tmp_path: Path, budget: TransferBudget
) -> None:
    target = tmp_path / "product.zip"
    target.write_bytes(PAYLOAD)
    calls: list[int] = []

    result = await download_stream(
        open_stream=_streamer(PAYLOAD, record=calls),
        target=target,
        budget=budget,
        expected_size=len(PAYLOAD),
        checksum=MD5,
        checksum_algorithm="MD5",
    )

    assert result.cached is True
    assert result.bytes_written == 0
    assert calls == []  # nothing was transferred
    assert budget.spent_bytes == 0


async def test_an_existing_file_with_a_bad_checksum_is_downloaded_again(
    tmp_path: Path, budget: TransferBudget
) -> None:
    target = tmp_path / "product.zip"
    target.write_bytes(b"x" * len(PAYLOAD))  # right size, wrong content

    result = await download_stream(
        open_stream=_streamer(PAYLOAD),
        target=target,
        budget=budget,
        expected_size=len(PAYLOAD),
        checksum=MD5,
        checksum_algorithm="MD5",
    )

    assert result.cached is False
    assert target.read_bytes() == PAYLOAD


async def test_a_partial_file_is_resumed_not_restarted(
    tmp_path: Path, budget: TransferBudget
) -> None:
    target = tmp_path / "product.zip"
    already = 2000
    partial_path(target).write_bytes(PAYLOAD[:already])
    calls: list[int] = []

    result = await download_stream(
        open_stream=_streamer(PAYLOAD, record=calls),
        target=target,
        budget=budget,
        expected_size=len(PAYLOAD),
        checksum=MD5,
        checksum_algorithm="MD5",
    )

    assert calls == [already]  # asked the server to continue from there
    assert result.resumed_from == already
    assert result.bytes_written == len(PAYLOAD) - already
    assert target.read_bytes() == PAYLOAD


async def test_a_server_ignoring_the_range_restarts_instead_of_corrupting(
    tmp_path: Path, budget: TransferBudget
) -> None:
    """Appending a full body to a partial file would silently produce a corrupt archive."""
    target = tmp_path / "product.zip"
    partial_path(target).write_bytes(PAYLOAD[:2000])

    result = await download_stream(
        open_stream=_streamer(PAYLOAD, honour_range=False),
        target=target,
        budget=budget,
        expected_size=len(PAYLOAD),
        checksum=MD5,
        checksum_algorithm="MD5",
    )

    assert target.read_bytes() == PAYLOAD
    assert result.resumed_from == 0


async def test_a_checksum_mismatch_deletes_the_file_rather_than_returning_it(
    tmp_path: Path, budget: TransferBudget
) -> None:
    target = tmp_path / "product.zip"
    corrupt = b"y" * len(PAYLOAD)

    with pytest.raises(TransferFailed, match="checksum"):
        await download_stream(
            open_stream=_streamer(corrupt),
            target=target,
            budget=budget,
            expected_size=len(corrupt),
            checksum=MD5,
            checksum_algorithm="MD5",
        )

    assert not target.exists()
    assert not partial_path(target).exists()


async def test_a_short_response_is_rejected(tmp_path: Path, budget: TransferBudget) -> None:
    target = tmp_path / "product.zip"

    with pytest.raises(TransferFailed, match="Expected"):
        await download_stream(
            open_stream=_streamer(PAYLOAD[:100]),
            target=target,
            budget=budget,
            expected_size=len(PAYLOAD),
        )

    assert not target.exists()


async def test_an_unsupported_checksum_algorithm_does_not_block_the_download(
    tmp_path: Path, budget: TransferBudget
) -> None:
    """BLAKE3 needs a dependency we do not have; report unverified rather than failing."""
    target = tmp_path / "product.zip"

    result = await download_stream(
        open_stream=_streamer(PAYLOAD),
        target=target,
        budget=budget,
        expected_size=len(PAYLOAD),
        checksum="not-checkable",
        checksum_algorithm="BLAKE3",
    )

    assert target.read_bytes() == PAYLOAD
    assert result.checksum_verified is False


async def test_progress_is_reported_as_bytes_arrive(tmp_path: Path, budget: TransferBudget) -> None:
    seen: list[tuple[int, int | None]] = []

    async def on_progress(done: int, total: int | None) -> None:
        seen.append((done, total))

    await download_stream(
        open_stream=_streamer(PAYLOAD),
        target=tmp_path / "product.zip",
        budget=budget,
        expected_size=len(PAYLOAD),
        progress=on_progress,
    )

    assert seen
    assert seen[-1][0] == len(PAYLOAD)


async def test_cancelling_keeps_the_bytes_already_written(
    tmp_path: Path, budget: TransferBudget, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever reached the disk stays there, so the transfer can resume from it.

    Chunk size is shrunk so the boundary is explicit. In production it is 1 MiB, and up to
    one chunk of buffered-but-unwritten data is lost on cancellation - harmless, because
    resuming simply re-fetches from the last byte on disk.
    """
    monkeypatch.setattr("cdse_local_mcp.transfer.download.CHUNK_SIZE", 256)
    target = tmp_path / "product.zip"

    @asynccontextmanager
    async def slow_stream(start: int) -> AsyncIterator[httpx.Response]:
        async def body() -> AsyncIterator[bytes]:
            yield PAYLOAD[:1000]
            await asyncio.sleep(30)  # cancelled here
            yield PAYLOAD[1000:]

        yield httpx.Response(
            200, content=body(), request=httpx.Request("GET", "https://example.invalid/p")
        )

    task = asyncio.create_task(
        download_stream(
            open_stream=slow_stream,
            target=target,
            budget=budget,
            expected_size=len(PAYLOAD),
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not target.exists()
    partial = partial_path(target).read_bytes()
    assert partial, "the partial file must survive cancellation"
    assert PAYLOAD.startswith(partial), "kept bytes must be a valid prefix, not garbage"
    assert len(partial) <= 1000
