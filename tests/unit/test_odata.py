"""OData metadata and authenticated streaming, including the cross-host redirect trap."""

from __future__ import annotations

import httpx
import pytest
import respx

from cdse_local_mcp.auth import TokenProvider
from cdse_local_mcp.clients.odata import ODataClient
from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import AuthRequired, NotFound, OfflineProduct
from tests.unit.test_auth import TOKEN_URL, _token_response

UUID = "afbeb0a0-2d83-4a94-9eed-df076ec183d6"
CATALOGUE = f"https://catalogue.dataspace.copernicus.eu/odata/v1/Products({UUID})"
DOWNLOAD = f"https://download.dataspace.copernicus.eu/odata/v1/Products({UUID})/$value"
NODE = "https://zipper.dataspace.copernicus.eu/odata/v1/deliver"

PRODUCT = {
    "Id": UUID,
    "Name": "S2A_MSIL1C_20240731T092031_N0511_R093_T34SEH_20240731T112239.SAFE",
    "ContentLength": 812345678,
    "Online": True,
    "S3Path": "/eodata/Sentinel-2/MSI/L1C/2024/07/31/S2A_MSIL1C_20240731T092031.SAFE",
    "Checksum": [
        {"Value": "e99a18c428cb38d5f260853678922e03", "Algorithm": "MD5"},
        {"Value": "deadbeef", "Algorithm": "BLAKE3"},
    ],
}


def _client() -> ODataClient:
    settings = Settings(client_id="an-id", client_secret="a-secret")  # type: ignore[arg-type]
    return ODataClient(settings, TokenProvider(settings))


@respx.mock
async def test_product_info_projects_what_a_download_decision_needs() -> None:
    respx.get(CATALOGUE).mock(return_value=httpx.Response(200, json=PRODUCT))

    info = await _client().product_info(UUID)

    assert info.name.endswith(".SAFE")
    assert info.content_length == 812345678
    assert info.online is True
    assert info.s3_path is not None


@respx.mock
async def test_md5_is_preferred_because_hashlib_can_verify_it() -> None:
    respx.get(CATALOGUE).mock(return_value=httpx.Response(200, json=PRODUCT))

    info = await _client().product_info(UUID)

    assert info.checksum_algorithm == "MD5"
    assert info.checksum_value == "e99a18c428cb38d5f260853678922e03"


@respx.mock
async def test_a_missing_online_flag_means_online() -> None:
    """Only archived products carry the flag; absence must not read as offline."""
    payload = {k: v for k, v in PRODUCT.items() if k != "Online"}
    respx.get(CATALOGUE).mock(return_value=httpx.Response(200, json=payload))

    assert (await _client().product_info(UUID)).is_offline is False


@respx.mock
async def test_an_archived_product_is_flagged_offline() -> None:
    respx.get(CATALOGUE).mock(return_value=httpx.Response(200, json={**PRODUCT, "Online": False}))

    assert (await _client().product_info(UUID)).is_offline is True


@respx.mock
async def test_an_unknown_uuid_is_not_found() -> None:
    respx.get(CATALOGUE).mock(return_value=httpx.Response(404))

    with pytest.raises(NotFound):
        await _client().product_info(UUID)


@respx.mock
async def test_the_token_is_reattached_across_a_cross_host_redirect() -> None:
    """The trap this client exists for.

    CDSE redirects $value to a delivery host, and httpx strips Authorization on a cross-host
    redirect. Letting it follow redirects automatically yields a 401 that looks like bad
    credentials.
    """
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(DOWNLOAD).mock(return_value=httpx.Response(302, headers={"location": NODE}))
    delivered = respx.get(NODE).mock(return_value=httpx.Response(200, content=b"zipbytes"))

    async with _client().stream_archive(UUID) as response:
        assert response.status_code == 200

    assert delivered.call_count == 1
    assert delivered.calls.last.request.headers["Authorization"] == "Bearer tok-1"


@respx.mock
async def test_a_resumed_download_sends_a_range_header_on_the_final_hop() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(DOWNLOAD).mock(return_value=httpx.Response(302, headers={"location": NODE}))
    delivered = respx.get(NODE).mock(return_value=httpx.Response(206, content=b"tail"))

    async with _client().stream_archive(UUID, resume_from=1024) as response:
        assert response.status_code == 206

    assert delivered.calls.last.request.headers["Range"] == "bytes=1024-"


@respx.mock
async def test_a_rejected_token_invalidates_the_cache_so_a_retry_refetches() -> None:
    respx.post(TOKEN_URL).mock(side_effect=[_token_response("tok-1"), _token_response("tok-2")])
    respx.get(DOWNLOAD).mock(return_value=httpx.Response(401))
    client = _client()

    with pytest.raises(AuthRequired, match="rejected the download token"):
        async with client.stream_archive(UUID):
            pass

    respx.get(DOWNLOAD).mock(return_value=httpx.Response(200, content=b"ok"))
    async with client.stream_archive(UUID) as response:
        assert response.status_code == 200


@respx.mock
async def test_service_unavailable_is_reported_as_an_archived_product() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(DOWNLOAD).mock(return_value=httpx.Response(503))

    with pytest.raises(OfflineProduct) as exc:
        async with _client().stream_archive(UUID):
            pass
    assert "Data Workspace order" in (exc.value.hint or "")


@respx.mock
async def test_a_redirect_loop_gives_up_rather_than_spinning() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    respx.get(DOWNLOAD).mock(return_value=httpx.Response(302, headers={"location": DOWNLOAD}))

    with pytest.raises(Exception, match="redirected"):
        async with _client().stream_archive(UUID):
            pass
