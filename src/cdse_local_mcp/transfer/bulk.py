"""Downloading many objects as one logical product.

A Sentinel product on S3 is a prefix holding hundreds of objects, not a single file, so
"download this product" means rebuilding that tree on disk. Each object goes through the
same resumable, verified, sandboxed path as any other transfer; this module only orchestrates
them and aggregates progress.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from cdse_local_mcp.clients.s3 import S3Client, S3Object
from cdse_local_mcp.transfer.budget import MAX_CONCURRENT_CONNECTIONS, TransferBudget
from cdse_local_mcp.transfer.download import download_stream
from cdse_local_mcp.transfer.paths import resolve_target

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int | None], Awaitable[None]]


@dataclass(frozen=True)
class BulkResult:
    """The outcome of rebuilding one product tree."""

    directory: Path
    files_written: int
    files_cached: int
    bytes_written: int
    total_bytes: int


async def download_objects(
    *,
    client: S3Client,
    bucket: str,
    prefix: str,
    objects: list[S3Object],
    destination_root: Path,
    directory_name: str,
    budget: TransferBudget,
    progress: ProgressCallback | None = None,
) -> BulkResult:
    """Fetch every object under a prefix into ``destination_root/directory_name``.

    Objects are transferred with bounded concurrency; the budget's semaphore is the real
    ceiling, so this only avoids queueing thousands of tasks at once.
    """
    total = sum(obj.size for obj in objects)
    done_by_key: dict[str, int] = {}
    written = 0
    cached = 0
    gate = asyncio.Semaphore(MAX_CONCURRENT_CONNECTIONS)

    async def report() -> None:
        if progress is not None:
            await progress(sum(done_by_key.values()), total)

    async def fetch(obj: S3Object) -> tuple[int, bool]:
        relative = obj.relative_to(prefix)
        target = resolve_target(destination_root, directory_name, *relative.split("/"))

        async def on_progress(done: int, _total: int | None) -> None:
            done_by_key[obj.key] = done
            await report()

        async with gate:
            result = await download_stream(
                open_stream=lambda start: client.stream_object(bucket, obj.key, resume_from=start),
                target=target,
                budget=budget,
                expected_size=obj.size or None,
                progress=on_progress,
            )

        # Settle this object's contribution, so a cached file still counts toward progress.
        done_by_key[obj.key] = result.total_bytes
        await report()
        return result.bytes_written, result.cached

    results = await asyncio.gather(*(fetch(obj) for obj in objects))
    for obj_written, obj_cached in results:
        written += obj_written
        cached += int(obj_cached)

    directory = resolve_target(destination_root, directory_name)
    logger.info(
        "rebuilt %s: %d objects, %d cached, %d bytes transferred",
        directory_name,
        len(objects),
        cached,
        written,
    )
    return BulkResult(
        directory=directory,
        files_written=len(objects) - cached,
        files_cached=cached,
        bytes_written=written,
        total_bytes=total,
    )


def pending_bytes(
    objects: list[S3Object], *, destination_root: Path, directory_name: str, prefix: str
) -> int:
    """Bytes that would actually transfer: objects not already present at full size.

    Lets a budget check count only real traffic, so a request over bands that are already
    cached is not refused for bytes that will never move.
    """
    pending = 0
    for obj in objects:
        target = resolve_target(
            destination_root, directory_name, *obj.relative_to(prefix).split("/")
        )
        if not (target.exists() and target.stat().st_size == obj.size):
            pending += obj.size
    return pending
