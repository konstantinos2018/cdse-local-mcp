"""Getting bytes onto disk correctly.

The rules this module exists to enforce:

* write to ``<name>.part``, fsync, then rename, so a reader never sees a half file;
* resume from a partial file rather than starting again;
* verify length and checksum, and delete rather than hand back a corrupt path;
* an already-verified file is a cache hit, not a re-transfer;
* a cancelled transfer keeps its ``.part`` so it can be resumed.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path

import httpx

from cdse_local_mcp.errors import TransferFailed
from cdse_local_mcp.transfer.budget import TransferBudget
from cdse_local_mcp.transfer.paths import partial_path

logger = logging.getLogger(__name__)

CHUNK_SIZE = 1024 * 1024
_HTTP_PARTIAL_CONTENT = 206

# hashlib has md5 everywhere; BLAKE3 needs a third-party package we do not depend on.
_SUPPORTED_ALGORITHMS = {"MD5"}

StreamOpener = Callable[[int], AbstractAsyncContextManager[httpx.Response]]
ProgressCallback = Callable[[int, int | None], Awaitable[None]]


@dataclass(frozen=True)
class TransferResult:
    """What happened, in terms a caller can report honestly."""

    path: Path
    bytes_written: int
    total_bytes: int
    resumed_from: int
    cached: bool
    checksum_verified: bool


def _stat_size(path: Path) -> int | None:
    """Size of a file, or None if it does not exist."""
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return None


def _hash_file(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm.lower())
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


async def verify_checksum(path: Path, expected: str, algorithm: str) -> bool:
    """Compare a file against a CDSE checksum, off the event loop."""
    if algorithm.upper() not in _SUPPORTED_ALGORITHMS:
        logger.warning(
            "cannot verify %s checksum for %s: algorithm unsupported", algorithm, path.name
        )
        return False
    actual = await asyncio.to_thread(_hash_file, path, algorithm)
    return actual.lower() == expected.lower()


async def _is_already_complete(
    target: Path, expected_size: int | None, checksum: str | None, algorithm: str | None
) -> bool:
    """Decide whether an existing file can be handed back without transferring."""
    size = await asyncio.to_thread(_stat_size, target)
    if size is None:
        return False

    if expected_size is not None and size != expected_size:
        logger.info("%s exists but is the wrong size; re-downloading", target.name)
        return False

    if checksum and algorithm and algorithm.upper() in _SUPPORTED_ALGORITHMS:
        if await verify_checksum(target, checksum, algorithm):
            return True
        logger.warning("%s exists but fails its checksum; re-downloading", target.name)
        return False

    # No usable checksum: a matching size is the strongest evidence available.
    return expected_size is not None


async def download_stream(
    *,
    open_stream: StreamOpener,
    target: Path,
    budget: TransferBudget,
    expected_size: int | None = None,
    checksum: str | None = None,
    checksum_algorithm: str | None = None,
    progress: ProgressCallback | None = None,
) -> TransferResult:
    """Stream a remote resource to ``target``, resuming and verifying as needed."""
    if await _is_already_complete(target, expected_size, checksum, checksum_algorithm):
        size = await asyncio.to_thread(_stat_size, target) or 0
        logger.info("%s is already present and verified", target.name)
        return TransferResult(
            path=target,
            bytes_written=0,
            total_bytes=size,
            resumed_from=0,
            cached=True,
            checksum_verified=bool(checksum),
        )

    part = partial_path(target)
    resume_from = await asyncio.to_thread(_stat_size, part) or 0
    if resume_from:
        logger.info("resuming %s from %d bytes", target.name, resume_from)

    async with budget.slot(), open_stream(resume_from) as response:
        # A server that ignores Range replies 200 and restarts the content, so appending
        # would silently corrupt the file.
        if resume_from and response.status_code != _HTTP_PARTIAL_CONTENT:
            logger.warning("server ignored the range request; restarting %s", target.name)
            part.unlink(missing_ok=True)
            resume_from = 0

        total = _expected_total(response, resume_from, expected_size)
        written = await _write_body(
            response, part, resume_from=resume_from, total=total, progress=progress
        )

    final_size = await asyncio.to_thread(_stat_size, part) or 0
    _check_size(part, final_size, expected_size)

    verified = False
    if checksum and checksum_algorithm:
        verified = await verify_checksum(part, checksum, checksum_algorithm)
        if not verified and checksum_algorithm.upper() in _SUPPORTED_ALGORITHMS:
            part.unlink(missing_ok=True)
            raise TransferFailed(
                f"{target.name} failed its {checksum_algorithm} checksum and was deleted.",
                hint="Retry the download; if it fails again the product may be corrupt at source.",
            )

    part.replace(target)
    budget.record(written)
    logger.info("downloaded %s (%d bytes)", target.name, final_size)

    return TransferResult(
        path=target,
        bytes_written=written,
        total_bytes=final_size,
        resumed_from=resume_from,
        cached=False,
        checksum_verified=verified,
    )


def _expected_total(
    response: httpx.Response, resume_from: int, expected_size: int | None
) -> int | None:
    """Total size of the finished file, from whatever the response reveals."""
    length = response.headers.get("Content-Length")
    if length is not None:
        try:
            return int(length) + resume_from
        except ValueError:
            logger.debug("unparseable Content-Length=%r", length)
    return expected_size


async def _write_body(
    response: httpx.Response,
    part: Path,
    *,
    resume_from: int,
    total: int | None,
    progress: ProgressCallback | None,
) -> int:
    """Append the response body to the partial file, reporting progress as it goes."""
    written = 0
    done = resume_from
    mode = "ab" if resume_from else "wb"

    part.parent.mkdir(parents=True, exist_ok=True)
    handle = part.open(mode)
    try:
        async for chunk in response.aiter_bytes(CHUNK_SIZE):
            handle.write(chunk)
            written += len(chunk)
            done += len(chunk)
            if progress is not None:
                await progress(done, total)
    except asyncio.CancelledError:
        # Keep the partial file: it is what makes the job resumable.
        logger.info("transfer cancelled; %s keeps %d bytes", part.name, done)
        raise
    finally:
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()

    return written


def _check_size(part: Path, actual: int, expected: int | None) -> None:
    if expected is not None and actual != expected:
        part.unlink(missing_ok=True)
        raise TransferFailed(
            f"Expected {expected} bytes but received {actual}; the partial file was deleted.",
            hint="Retry the download. A repeated mismatch suggests a problem at CDSE.",
        )
