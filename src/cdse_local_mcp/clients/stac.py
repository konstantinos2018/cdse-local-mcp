"""Thin async client for the CDSE STAC API.

Transport only: no MCP imports, no presentation. Endpoints come from ``config``, which takes
them from ``docs/cdse-apis.md``.

Two behaviours of the live API are handled here because they are easy to get wrong:

* ``/collections`` returns at most 200 entries per page and every Sentinel collection sorts
  *after* the CLMS ones, so a single-page read finds no Sentinel data at all;
* ``/search`` paginates with an opaque ``token`` carried in the POST body of the ``next``
  link, not a URL query parameter.
"""

from __future__ import annotations

import asyncio
import logging
import random
from types import TracebackType
from typing import Any

import httpx

from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import NotFound, QuotaExceeded, UpstreamError

logger = logging.getLogger(__name__)

_MAX_COLLECTION_PAGES = 10
_HTTP_TOO_MANY_REQUESTS = 429

# Retry only what is plausibly transient. A 4xx other than 429 means the request is wrong and
# repeating it just spends quota.
_RETRY_STATUSES = frozenset({_HTTP_TOO_MANY_REQUESTS, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_MAX_BACKOFF_SECONDS = 20.0


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Seconds to wait before retrying, honouring ``Retry-After`` when present.

    Falls back to exponential backoff with jitter, so concurrent callers do not retry in
    lockstep.
    """
    header = response.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), _MAX_BACKOFF_SECONDS)
        except ValueError:
            logger.debug("could not parse Retry-After=%r, using backoff", header)

    backoff: float = _BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    return min(backoff + random.uniform(0, 0.5), _MAX_BACKOFF_SECONDS)


class StacClient:
    """Async STAC client. Search is unauthenticated."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base = settings.stac_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": "cdse-local-mcp"},
            follow_redirects=True,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> StacClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # -- internals ------------------------------------------------------------------------

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Send a request, retrying transient failures with bounded backoff."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.HTTPError:
                raise  # handled by tool_guard, which knows how to phrase transport failures

            if response.status_code in _RETRY_STATUSES and attempt < _MAX_ATTEMPTS:
                delay = _retry_delay(response, attempt)
                logger.warning(
                    "CDSE returned %d for %s; retrying in %.1fs (attempt %d of %d)",
                    response.status_code,
                    url,
                    delay,
                    attempt,
                    _MAX_ATTEMPTS,
                )
                await asyncio.sleep(delay)
                continue

            return self._handle(response, url)

        raise AssertionError("unreachable: the loop either returns or raises")  # pragma: no cover

    def _handle(self, response: httpx.Response, url: str) -> dict[str, Any]:
        if response.status_code == httpx.codes.NOT_FOUND:
            raise NotFound(
                f"CDSE STAC returned 404 for {url}.",
                hint="Check the collection id and product id; list_collections shows valid ids.",
            )
        if response.status_code == _HTTP_TOO_MANY_REQUESTS:
            raise QuotaExceeded(
                f"CDSE rate-limited the request and it did not recover after "
                f"{_MAX_ATTEMPTS} attempts.",
                hint=(
                    "Wait a minute before retrying, and batch fewer searches. The catalogue "
                    "allows 2000 requests per minute and 10000 per month."
                ),
            )
        if response.is_error:
            raise UpstreamError(
                f"CDSE STAC returned HTTP {response.status_code} for {url}: {response.text[:200]}",
                hint="Check the query parameters; narrow the area or time range and retry.",
            )

        try:
            payload: Any = response.json()
        except ValueError as exc:
            raise UpstreamError(
                f"CDSE STAC returned a non-JSON response for {url}.",
                hint="Retry; if it persists the service may be degraded.",
            ) from exc

        if not isinstance(payload, dict):
            raise UpstreamError(f"CDSE STAC returned an unexpected payload type for {url}.")
        return payload

    # -- api ------------------------------------------------------------------------------

    async def search(
        self,
        *,
        collection: str,
        bbox: list[float],
        datetime_range: str,
        limit: int,
        max_cloud_cover: float | None = None,
        cursor: str | None = None,
        newest_first: bool = True,
    ) -> dict[str, Any]:
        """POST /search. Returns the raw FeatureCollection."""
        body: dict[str, Any] = {
            "collections": [collection],
            "bbox": bbox,
            "datetime": datetime_range,
            "limit": limit,
        }
        if max_cloud_cover is not None:
            body["query"] = {"eo:cloud_cover": {"lte": max_cloud_cover}}
        if newest_first:
            body["sortby"] = [{"field": "properties.datetime", "direction": "desc"}]
        if cursor:
            body["token"] = cursor

        return await self._request("POST", f"{self._base}/search", json=body)

    async def get_item(self, *, collection: str, item_id: str) -> dict[str, Any]:
        """GET one item by id."""
        return await self._request("GET", f"{self._base}/collections/{collection}/items/{item_id}")

    async def list_collections(self, *, page_size: int = 200) -> list[dict[str, Any]]:
        """GET every collection, following ``next`` links.

        Bounded at :data:`_MAX_COLLECTION_PAGES` so a pagination bug upstream cannot spend
        the monthly request quota in a loop.
        """
        url: str | None = f"{self._base}/collections?limit={page_size}"
        found: list[dict[str, Any]] = []

        for _ in range(_MAX_COLLECTION_PAGES):
            if url is None:
                break
            payload = await self._request("GET", url)
            page = payload.get("collections") or []
            found.extend(c for c in page if isinstance(c, dict))
            url = _next_link_href(payload.get("links"))
        else:
            logger.warning(
                "stopped paging collections at %d pages; results may be incomplete",
                _MAX_COLLECTION_PAGES,
            )

        return found


def _next_link_href(links: Any) -> str | None:
    """Extract a GET ``next`` link href, if present."""
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, dict) and link.get("rel") == "next":
            href = link.get("href")
            if isinstance(href, str) and str(link.get("method", "GET")).upper() == "GET":
                return href
    return None


def next_search_cursor(payload: dict[str, Any]) -> str | None:
    """Extract the opaque pagination token from a ``/search`` response.

    The live API returns ``next`` as a POST link whose body carries a ``token``; there is no
    URL to follow.
    """
    links = payload.get("links")
    if not isinstance(links, list):
        return None
    for link in links:
        if not isinstance(link, dict) or link.get("rel") != "next":
            continue
        body = link.get("body")
        if isinstance(body, dict):
            token = body.get("token")
            if isinstance(token, str) and token:
                return token
    return None
