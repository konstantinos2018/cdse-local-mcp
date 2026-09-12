"""OData client: product metadata and authenticated archive streaming.

Two hosts, deliberately: metadata comes from the catalogue host, bytes from the download
host. Using the wrong one fails confusingly.

**The redirect trap.** ``Products(<uuid>)/$value`` answers with a redirect to a data node on
a different host, and httpx — like requests — strips the ``Authorization`` header on a
cross-host redirect. Following redirects automatically therefore produces a 401 that looks
like bad credentials. Redirects are followed by hand here, re-attaching the token each hop.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx

from cdse_local_mcp.auth import TokenProvider
from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import (
    AuthRequired,
    NotFound,
    OfflineProduct,
    QuotaExceeded,
    UpstreamError,
)

logger = logging.getLogger(__name__)

_MAX_REDIRECTS = 5
_HTTP_TOO_MANY_REQUESTS = 429


@dataclass(frozen=True)
class ProductInfo:
    """What is needed to decide on, and then verify, a download."""

    uuid: str
    name: str
    content_length: int | None
    checksum_value: str | None
    checksum_algorithm: str | None
    online: bool
    s3_path: str | None

    @property
    def is_offline(self) -> bool:
        return not self.online


def _parse_checksum(raw: Any) -> tuple[str | None, str | None]:
    """Pick a usable checksum, preferring MD5 because hashlib always has it."""
    if not isinstance(raw, list):
        return None, None

    entries = [c for c in raw if isinstance(c, dict) and c.get("Value")]
    for preferred in ("MD5", "BLAKE3"):
        for entry in entries:
            if str(entry.get("Algorithm", "")).upper() == preferred:
                return str(entry["Value"]), preferred
    if entries:
        return str(entries[0]["Value"]), str(entries[0].get("Algorithm") or "unknown")
    return None, None


class ODataClient:
    """Product metadata and authenticated archive downloads."""

    def __init__(
        self,
        settings: Settings,
        tokens: TokenProvider,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = tokens
        self._catalogue = settings.catalogue_odata_url.rstrip("/")
        self._download = settings.download_odata_url.rstrip("/")
        # Redirects are followed by hand so the Authorization header survives each hop.
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": "cdse-local-mcp"},
            follow_redirects=False,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> ODataClient:
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

    async def product_info(self, uuid: str) -> ProductInfo:
        """Fetch the metadata a download decision needs: size, checksum, online state."""
        url = f"{self._catalogue}/Products({uuid})"
        response = await self._client.get(url, params={"$expand": "Attributes"})

        if response.status_code == httpx.codes.NOT_FOUND:
            raise NotFound(
                f"No product with UUID {uuid}.",
                hint="Use the odata_uuid from a search_products result.",
            )
        if response.is_error:
            raise UpstreamError(
                f"CDSE OData returned HTTP {response.status_code} for product {uuid}.",
                hint="Retry shortly; if it persists, re-run the search to get a fresh id.",
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise UpstreamError(f"Unexpected OData payload for product {uuid}.")

        checksum_value, checksum_algorithm = _parse_checksum(payload.get("Checksum"))
        content_length = payload.get("ContentLength")

        return ProductInfo(
            uuid=uuid,
            name=str(payload.get("Name") or uuid),
            content_length=int(content_length) if isinstance(content_length, int | str) else None,
            checksum_value=checksum_value,
            checksum_algorithm=checksum_algorithm,
            # Absent means online: only archived products carry the flag as false.
            online=payload.get("Online") is not False,
            s3_path=payload.get("S3Path"),
        )

    @asynccontextmanager
    async def stream_archive(
        self, uuid: str, *, resume_from: int = 0
    ) -> AsyncIterator[httpx.Response]:
        """Open an authenticated byte stream for a whole product archive.

        ``resume_from`` continues a partial download with a ``Range`` request. The caller
        must check ``response.status_code``: a server that ignores the range answers 200 and
        sends the whole file from the start.
        """
        url = f"{self._download}/Products({uuid})/$value"
        response = await self._send_with_redirects(url, uuid=uuid, resume_from=resume_from)
        try:
            yield response
        finally:
            await response.aclose()

    async def _send_with_redirects(
        self, url: str, *, uuid: str, resume_from: int
    ) -> httpx.Response:
        for hop in range(_MAX_REDIRECTS):
            # Re-attach credentials on every hop: the header does not survive a cross-host
            # redirect, and the token may have expired during a long transfer.
            headers = await self._tokens.authorization_header()
            if resume_from > 0:
                headers["Range"] = f"bytes={resume_from}-"

            request = self._client.build_request("GET", url, headers=headers)
            response = await self._client.send(request, stream=True)

            if response.is_redirect:
                location = response.headers.get("location")
                await response.aclose()
                if not location:
                    raise UpstreamError(
                        f"CDSE redirected the download of {uuid} without a location header."
                    )
                url = str(httpx.URL(url).join(location))
                logger.debug("download redirect %d for %s", hop + 1, uuid)
                continue

            return await self._check_download_response(response, uuid)

        raise UpstreamError(
            f"CDSE redirected the download of {uuid} more than {_MAX_REDIRECTS} times.",
            hint="Retry later; the delivery service may be misconfigured.",
        )

    async def _check_download_response(self, response: httpx.Response, uuid: str) -> httpx.Response:
        if response.is_success:
            return response

        await response.aread()
        await response.aclose()

        if response.status_code == httpx.codes.UNAUTHORIZED:
            self._tokens.invalidate()
            raise AuthRequired(
                "CDSE rejected the download token.",
                hint="Check CDSE_CLIENT_ID and CDSE_CLIENT_SECRET, then retry.",
            )
        if response.status_code == httpx.codes.FORBIDDEN:
            raise AuthRequired(
                f"Access to product {uuid} was refused.",
                hint=(
                    "The account may lack rights to this collection, or the product may need "
                    "a Data Workspace order."
                ),
            )
        if response.status_code == httpx.codes.NOT_FOUND:
            raise NotFound(f"No downloadable product with UUID {uuid}.")
        if response.status_code == _HTTP_TOO_MANY_REQUESTS:
            raise QuotaExceeded(
                "CDSE rate-limited the download.",
                hint=(
                    "Wait before retrying. Only 4 concurrent connections are allowed per account."
                ),
            )
        if response.status_code == httpx.codes.SERVICE_UNAVAILABLE:
            raise OfflineProduct(
                f"Product {uuid} is not available for immediate download.",
                hint=(
                    "It is probably on long-term archive and needs a Data Workspace order: "
                    "25 products per month, one active order at a time."
                ),
            )

        raise UpstreamError(
            f"CDSE returned HTTP {response.status_code} downloading {uuid}.",
            hint="Retry shortly; if it persists, re-run the search for a fresh product id.",
        )
