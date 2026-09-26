"""S3 client for the CDSE ``eodata`` bucket.

This is the download transport. The OData ``$value`` endpoint refuses tokens from a Sentinel
Hub OAuth client (``DAT-ZIP-609``, "Token audience not allowed") and only accepts a
password-grant token, which this project does not support. S3 access keys are long-lived,
independently revocable, and need no OAuth at all.

**Products are stored unpacked.** ``s3://eodata/.../S2B_..._.SAFE/`` is a prefix holding
hundreds of objects, not a single zip. Downloading a whole product means listing that prefix
and fetching each object; downloading one band means fetching one object. That is why
selective download is nearly free over S3 and expensive over OData.

boto3 is synchronous, so listing runs in a worker thread. Bytes are streamed with httpx using
**header-based SigV4 signatures**, which keeps transfers async and reuses the resumable,
checksum-verifying machinery in ``transfer/download.py``.

**Presigned URLs do not work here.** CDSE's gateway answers query-string SigV4 with
``403 InvalidAccessKeyId`` even for a key that lists and reads fine with header auth, so the
request headers are signed instead. Verified against the live endpoint on 2026-09-13.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Any
from urllib.parse import quote

import httpx

from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import AuthRequired, NotFound, UpstreamError

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "eodata"
_LIST_PAGE_SIZE = 1000
# SHA-256 of the empty string: the payload hash for a body-less GET.
_EMPTY_PAYLOAD_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


@dataclass(frozen=True)
class S3Object:
    """One object in the bucket."""

    key: str
    size: int

    def relative_to(self, prefix: str) -> str:
        """The object's path relative to a product prefix, for rebuilding the tree."""
        trimmed = prefix.rstrip("/") + "/"
        return self.key[len(trimmed) :] if self.key.startswith(trimmed) else self.key


def split_s3_path(s3_path: str) -> tuple[str, str]:
    """Split an OData ``S3Path`` or ``s3://`` href into bucket and prefix.

    CDSE reports ``/eodata/Sentinel-2/MSI/L1C/2024/07/16/X.SAFE``; STAC asset hrefs use
    ``s3://eodata/...``. Both name the same object tree.
    """
    raw = s3_path.strip()
    if raw.startswith("s3://"):
        raw = raw[len("s3://") :]
    raw = raw.lstrip("/")
    if not raw:
        raise UpstreamError(
            "CDSE reported an empty S3 path for this product.",
            hint="Re-run the search; if it persists the product may not be on S3 yet.",
        )
    bucket, _, prefix = raw.partition("/")
    return bucket or DEFAULT_BUCKET, prefix


class S3Client:
    """Lists and streams objects from the CDSE object store."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._boto: Any | None = None
        self._http = client or httpx.AsyncClient(
            timeout=settings.request_timeout,
            headers={"User-Agent": "cdse-local-mcp"},
            follow_redirects=True,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> S3Client:
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
            await self._http.aclose()

    def _client(self) -> Any:
        """Build the boto3 client on first use, so importing this module stays cheap."""
        if self._boto is not None:
            return self._boto

        settings = self._settings
        if not settings.has_s3:
            raise AuthRequired(
                "No CDSE S3 access keys are configured.",
                hint=(
                    "Create keys at https://eodata-s3keysmanager.dataspace.copernicus.eu/ and "
                    "set CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY. Downloads use S3; the "
                    "OAuth client cannot authorise them."
                ),
            )

        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise UpstreamError(
                "boto3 is not installed, so downloads are unavailable.",
                hint="boto3 is a required dependency; reinstall the package to restore it.",
            ) from exc

        assert settings.s3_access_key is not None
        assert settings.s3_secret_key is not None

        self._boto = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            region_name=settings.s3_region,
            config=Config(
                signature_version="s3v4",
                # The bucket is 'eodata' and so is the host; virtual-hosted addressing would
                # resolve to eodata.eodata.dataspace.copernicus.eu and fail.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
        return self._boto

    # -- api ------------------------------------------------------------------------------

    async def list_objects(self, bucket: str, prefix: str) -> list[S3Object]:
        """List every object under a prefix, following pagination."""
        return await asyncio.to_thread(self._list_objects_sync, bucket, prefix)

    def _list_objects_sync(self, bucket: str, prefix: str) -> list[S3Object]:
        client = self._client()
        found: list[S3Object] = []
        token: str | None = None

        while True:
            kwargs: dict[str, Any] = {
                "Bucket": bucket,
                "Prefix": prefix.rstrip("/") + "/",
                "MaxKeys": _LIST_PAGE_SIZE,
            }
            if token:
                kwargs["ContinuationToken"] = token

            try:
                page = client.list_objects_v2(**kwargs)
            except Exception as exc:
                raise _translate(exc, f"listing {bucket}/{prefix}") from exc

            for entry in page.get("Contents", []):
                key = entry.get("Key", "")
                # A "directory" placeholder object has no content.
                if key and not key.endswith("/"):
                    found.append(S3Object(key=key, size=int(entry.get("Size", 0))))

            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")

        if not found:
            raise NotFound(
                f"Nothing is stored under {bucket}/{prefix}.",
                hint="Check the product is online; archived products are not on S3.",
            )
        logger.info("listed %d objects under %s/%s", len(found), bucket, prefix)
        return found

    def _signed_headers(self, url: str, extra: dict[str, str]) -> dict[str, str]:
        """Sign a GET with SigV4 in the headers.

        Query-string signing (presigned URLs) is refused by CDSE, so the signature travels in
        the Authorization header. Any extra header passed here, notably Range, is included in
        the signature.
        """
        settings = self._settings
        self._client()  # raises AuthRequired when keys are missing

        try:
            from botocore.auth import S3SigV4Auth
            from botocore.awsrequest import AWSRequest
            from botocore.credentials import Credentials
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise UpstreamError("botocore is not installed, so downloads are unavailable.") from exc

        assert settings.s3_access_key is not None
        assert settings.s3_secret_key is not None

        request = AWSRequest(method="GET", url=url, headers=dict(extra))
        request.headers["X-Amz-Content-SHA256"] = _EMPTY_PAYLOAD_SHA256
        credentials = Credentials(settings.s3_access_key, settings.s3_secret_key.get_secret_value())
        S3SigV4Auth(credentials, "s3", settings.s3_region).add_auth(request)
        return dict(request.headers)

    def object_url(self, bucket: str, key: str) -> str:
        """Path-style URL for an object. The bucket is also the hostname, so virtual-hosted
        addressing would resolve to eodata.eodata.dataspace.copernicus.eu."""
        endpoint = self._settings.s3_endpoint_url.rstrip("/")
        return f"{endpoint}/{bucket}/{quote(key, safe='/~')}"

    @asynccontextmanager
    async def stream_object(
        self, bucket: str, key: str, *, resume_from: int = 0
    ) -> AsyncIterator[httpx.Response]:
        """Open a byte stream for one object, optionally resuming."""
        url = self.object_url(bucket, key)
        extra = {"Range": f"bytes={resume_from}-"} if resume_from > 0 else {}
        headers = await asyncio.to_thread(self._signed_headers, url, extra)

        request = self._http.build_request("GET", url, headers=headers)
        response = await self._http.send(request, stream=True)
        try:
            if response.is_error:
                await response.aread()
                await response.aclose()
                raise _http_error(response, bucket, key)
            yield response
        finally:
            await response.aclose()


def _s3_error_code(response: httpx.Response) -> str:
    """Pull <Code> out of an S3 error body, which says far more than the status alone."""
    match = re.search(r"<Code>([^<]+)</Code>", response.text or "")
    return match.group(1) if match else ""


def _http_error(response: httpx.Response, bucket: str, key: str) -> Exception:
    code = _s3_error_code(response)
    detail = f" ({code})" if code else ""
    if response.status_code in (httpx.codes.FORBIDDEN, httpx.codes.UNAUTHORIZED):
        return AuthRequired(
            f"S3 refused access to {key}{detail}.",
            hint=(
                "Check CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY, and that the keys have not "
                "expired in the S3 keys manager."
            ),
        )
    if response.status_code == httpx.codes.NOT_FOUND:
        return NotFound(f"No object {bucket}/{key}.")
    return UpstreamError(
        f"S3 returned HTTP {response.status_code}{detail} for {key}.",
        hint="Retry shortly; the object store may be busy.",
    )


def _translate(exc: Exception, action: str) -> Exception:
    """Turn a botocore error into one of ours, without leaking credentials."""
    code = ""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = str(response.get("Error", {}).get("Code", ""))

    if code in {"AccessDenied", "InvalidAccessKeyId", "SignatureDoesNotMatch", "403"}:
        return AuthRequired(
            f"S3 refused the request while {action}.",
            hint=(
                "Check CDSE_S3_ACCESS_KEY and CDSE_S3_SECRET_KEY, and that the keys are still "
                "valid in the S3 keys manager."
            ),
        )
    if code in {"NoSuchKey", "NoSuchBucket", "404"}:
        return NotFound(f"Nothing found while {action}.")
    return UpstreamError(
        f"S3 failed while {action}: {type(exc).__name__}",
        hint="Retry shortly; if it persists, check the CDSE service status.",
    )
