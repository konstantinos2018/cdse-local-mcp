"""CDSE OAuth client-credentials token provider.

There is exactly one of these per process. No tool or client body mints a token inline:
access tokens live only 10 minutes, so scattering token fetches guarantees a transfer will
eventually die mid-flight holding a stale one.

Account passwords are deliberately unsupported. The password grant would put a reusable
credential in MCP client configuration files, and CDSE's own documentation advises against
embedding it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from types import TracebackType

import httpx

from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import AuthRequired, UpstreamError

logger = logging.getLogger(__name__)

# Refresh this long before expiry, so a transfer starting now does not begin with a token
# that dies seconds later.
REFRESH_MARGIN_SECONDS = 60.0
_DEFAULT_LIFETIME_SECONDS = 600.0


class TokenProvider:
    """Fetches and caches a CDSE access token, refreshing it before it expires."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": "cdse-local-mcp"},
        )
        self._owns_client = client is None
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    async def __aenter__(self) -> TokenProvider:
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

    def _cached(self) -> str | None:
        """The cached token, if it is still comfortably valid."""
        if self._token is not None and time.monotonic() < self._expires_at:
            return self._token
        return None

    async def token(self) -> str:
        """Return a valid access token, fetching or refreshing as needed."""
        cached = self._cached()
        if cached is not None:
            return cached

        async with self._lock:
            # Another caller may have refreshed while we waited for the lock.
            cached = self._cached()
            if cached is not None:
                return cached
            return await self._fetch()

    async def authorization_header(self) -> dict[str, str]:
        """Return a ready-to-use ``Authorization`` header."""
        return {"Authorization": f"Bearer {await self.token()}"}

    def invalidate(self) -> None:
        """Drop the cached token, so the next call fetches a fresh one.

        Call this after a 401: the token may have been revoked before its stated expiry.
        """
        self._token = None
        self._expires_at = 0.0

    async def _fetch(self) -> str:
        settings = self._settings
        if not settings.has_oauth:
            raise AuthRequired(
                "No CDSE OAuth credentials are configured.",
                hint=(
                    "Set CDSE_CLIENT_ID and CDSE_CLIENT_SECRET. Create an OAuth client in the "
                    "Copernicus Data Space dashboard. Catalogue search works without them; "
                    "downloads do not."
                ),
            )

        client_id = settings.client_id
        secret = settings.client_secret
        if client_id is None or secret is None:  # pragma: no cover - has_oauth checked above
            raise AuthRequired("CDSE OAuth credentials are incomplete.")

        response = await self._client.post(
            settings.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret.get_secret_value(),
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if response.status_code in (httpx.codes.UNAUTHORIZED, httpx.codes.BAD_REQUEST):
            # Never echo the response body: it can repeat the submitted client_id.
            raise AuthRequired(
                "CDSE rejected the OAuth client credentials.",
                hint=(
                    "Check CDSE_CLIENT_ID and CDSE_CLIENT_SECRET, and that the client has not "
                    "been deleted or expired in the dashboard."
                ),
            )
        if response.is_error:
            raise UpstreamError(
                f"CDSE token endpoint returned HTTP {response.status_code}.",
                hint="Retry shortly; the identity service may be temporarily unavailable.",
            )

        payload = response.json()
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise UpstreamError(
                "CDSE token response contained no access_token.",
                hint="Retry; if it persists the identity service may have changed.",
            )

        lifetime = float(payload.get("expires_in") or _DEFAULT_LIFETIME_SECONDS)
        self._token = token
        self._expires_at = time.monotonic() + max(lifetime - REFRESH_MARGIN_SECONDS, 0.0)
        logger.info("obtained CDSE access token, valid for %.0fs", lifetime)
        return token
