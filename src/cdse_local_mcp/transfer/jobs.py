"""Background download jobs and their journal.

Downloads outlive a tool call: MCP clients time out around 60 seconds and a Sentinel-2
product takes minutes. So a download tool enqueues a job and returns immediately, and the
caller polls.

The journal is a JSON file under the download root, written atomically on every state
change. It exists so a restarted server can say what happened rather than silently losing
the work, and so partial files can be matched to the job that made them.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as uuid_module
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from cdse_local_mcp.domain.models import Job, JobState
from cdse_local_mcp.errors import CdseError, NotFound
from cdse_local_mcp.transfer.paths import ensure_root

logger = logging.getLogger(__name__)

JOURNAL_NAME = "jobs.json"
_PROGRESS_WRITE_INTERVAL_SECONDS = 2.0
_MAX_JOURNAL_JOBS = 200


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class JobRegistry:
    """Creates, runs and records download jobs."""

    def __init__(self, root: Path) -> None:
        self._root = ensure_root(root)
        self._journal = self._root / JOURNAL_NAME
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._last_write = 0.0
        self._load()

    # -- journal ---------------------------------------------------------------------------

    def _load(self) -> None:
        """Read the journal, marking anything left running as interrupted."""
        if not self._journal.exists():
            return
        try:
            raw = json.loads(self._journal.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read the job journal: %s", exc)
            return

        for entry in raw.get("jobs", []):
            try:
                job = Job.model_validate(entry)
            except ValueError as exc:
                logger.debug("skipping malformed journal entry: %s", exc)
                continue
            # Its task died with the previous process; say so rather than implying progress.
            if job.state in {JobState.RUNNING, JobState.QUEUED}:
                job.state = JobState.INTERRUPTED
                job.error = "The server restarted while this download was in progress."
            self._jobs[job.job_id] = job

    def _write(self, *, force: bool = True) -> None:
        """Persist the journal atomically, throttled for progress updates."""
        now = time.monotonic()
        if not force and now - self._last_write < _PROGRESS_WRITE_INTERVAL_SECONDS:
            return
        self._last_write = now

        recent = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        payload = {"jobs": [j.model_dump(mode="json") for j in recent[:_MAX_JOURNAL_JOBS]]}

        tmp = self._journal.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1))
            tmp.replace(self._journal)
        except OSError as exc:  # a broken journal must not break a download
            logger.warning("could not write the job journal: %s", exc)

    # -- lifecycle -------------------------------------------------------------------------

    def create(self, *, product_id: str, collection: str, bytes_total: int | None) -> Job:
        """Register a queued job."""
        job = Job(
            job_id=uuid_module.uuid4().hex[:12],
            product_id=product_id,
            collection=collection,
            state=JobState.QUEUED,
            bytes_total=bytes_total,
            created_at=_now(),
        )
        self._jobs[job.job_id] = job
        self._write()
        return job

    def start(self, job: Job, runner: Callable[[Job], Awaitable[None]]) -> None:
        """Run ``runner`` in the background, recording the outcome on the job."""

        async def wrapped() -> None:
            job.state = JobState.RUNNING
            self._write()
            try:
                await runner(job)
                job.state = JobState.COMPLETED
            except asyncio.CancelledError:
                job.state = JobState.CANCELLED
                job.error = "Cancelled. The partial file is kept and the download can resume."
                raise
            except CdseError as exc:
                # Keep the hint: a polling caller sees only what is recorded here.
                job.state = JobState.FAILED
                job.error = exc.as_tool_message()
                logger.warning("download job %s failed: %s", job.job_id, exc.message)
            except Exception as exc:  # recorded on the job, never swallowed silently
                job.state = JobState.FAILED
                job.error = str(exc)
                logger.warning("download job %s failed: %r", job.job_id, exc)
            finally:
                job.finished_at = _now()
                self._write()
                self._tasks.pop(job.job_id, None)

        self._tasks[job.job_id] = asyncio.create_task(wrapped(), name=f"download-{job.job_id}")

    def progress(self, job: Job, done: int, total: int | None) -> None:
        """Record progress, writing the journal at most every couple of seconds."""
        job.bytes_done = done
        if total is not None:
            job.bytes_total = total
        self._write(force=False)

    def finish(
        self,
        job: Job,
        *,
        path: Path,
        cached: bool,
        verified: bool,
        outputs: list[Path] | None = None,
        notes: list[str] | None = None,
    ) -> None:
        """Attach the result of a successful transfer."""
        job.path = str(path)
        job.cached = cached
        job.checksum_verified = verified
        job.outputs = [str(p) for p in outputs or []]
        job.notes = list(notes or [])
        self._write()

    # -- queries ---------------------------------------------------------------------------

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise NotFound(
                f"No download job with id {job_id}.",
                hint="Call download_status with no job_id to list recent jobs.",
            )
        return job

    def recent(self, limit: int = 20) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)[:limit]

    def active(self) -> list[Job]:
        return [j for j in self._jobs.values() if not j.state.is_finished]

    def cancel(self, job_id: str) -> Job:
        """Cancel a running job. Its partial file is kept for resuming."""
        job = self.get(job_id)
        task = self._tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            return job

        if not job.state.is_finished:
            job.state = JobState.CANCELLED
            job.finished_at = _now()
            self._write()
        return job


_registry: JobRegistry | None = None


def get_registry(root: Path) -> JobRegistry:
    """Return the process-wide job registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = JobRegistry(root)
    return _registry


def reset_registry() -> None:
    """Drop the process-wide registry. For tests."""
    global _registry
    _registry = None
