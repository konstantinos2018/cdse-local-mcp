"""Typed errors, and the guard that turns them into actionable tool messages.

Tool results must tell the model what to do differently. A traceback does not. Every tool
is wrapped in :func:`tool_guard`, which converts our errors into ``ToolError`` with a hint
attached, and anything unexpected into a short message with the detail sent to the log.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import httpx
from fastmcp.exceptions import ToolError

logger = logging.getLogger(__name__)

P = ParamSpec("P")
R = TypeVar("R")


class CdseError(Exception):
    """Base class for errors that are safe and useful to show a model."""

    code = "cdse_error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_tool_message(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.hint:
            parts.append(f"What to do: {self.hint}")
        return " ".join(parts)


class InvalidRequest(CdseError):
    """The caller's arguments cannot be used as given."""

    code = "invalid_request"


class CloudCoverUnspecified(InvalidRequest):
    """An optical collection was searched without an explicit cloud-cover decision.

    Deliberate: silently returning everything, or silently filtering, both produce answers
    that look right and are not. See the interaction rules in ``CLAUDE.md``.
    """

    code = "cloud_cover_unspecified"


class NotFound(CdseError):
    """The requested product or collection does not exist."""

    code = "not_found"


class AuthRequired(CdseError):
    """The operation needs credentials that are not configured."""

    code = "auth_required"


class UpstreamError(CdseError):
    """CDSE returned an error or unusable response."""

    code = "upstream_error"


class QuotaExceeded(UpstreamError):
    """CDSE rejected the request for rate or quota reasons."""

    code = "quota_exceeded"


def tool_guard(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Convert exceptions raised by a tool into actionable ``ToolError`` messages.

    Apply *below* the FastMCP decorator so the tool signature and schema survive::

        @mcp.tool(...)
        @tool_guard
        async def search_products(...): ...
    """

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await fn(*args, **kwargs)
        except CdseError as exc:
            raise ToolError(exc.as_tool_message()) from exc
        except httpx.TimeoutException as exc:
            raise ToolError(
                "[timeout] CDSE did not respond in time. What to do: retry once; if it "
                "persists, narrow the date range or reduce the requested limit."
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("transport error calling CDSE: %r", exc)
            raise ToolError(
                f"[transport_error] Could not reach CDSE: {type(exc).__name__}. "
                "What to do: check network connectivity and retry."
            ) from exc
        except Exception as exc:
            logger.exception("unhandled error in tool %s", fn.__name__)
            raise ToolError(
                f"[internal_error] {fn.__name__} failed: {type(exc).__name__}. "
                "What to do: report this with the server log."
            ) from exc

    return wrapper
