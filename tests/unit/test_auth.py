"""Token handling. Tokens live 10 minutes, so refresh behaviour is not optional."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from cdse_local_mcp.auth import TokenProvider
from cdse_local_mcp.config import Settings
from cdse_local_mcp.errors import AuthRequired, UpstreamError

TOKEN_URL = (
    "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
)


def _settings(**kwargs: object) -> Settings:
    return Settings(client_id="an-id", client_secret="a-secret", **kwargs)  # type: ignore[arg-type]


def _token_response(token: str = "tok-1", expires_in: int = 600) -> httpx.Response:
    return httpx.Response(
        200, json={"access_token": token, "expires_in": expires_in, "token_type": "Bearer"}
    )


async def test_missing_credentials_name_the_env_vars() -> None:
    provider = TokenProvider(Settings(client_id=None, client_secret=None))
    with pytest.raises(AuthRequired) as exc:
        await provider.token()

    hint = exc.value.hint or ""
    assert "CDSE_CLIENT_ID" in hint
    assert "search works without them" in hint


@respx.mock
async def test_a_token_is_fetched_once_and_then_cached() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    provider = TokenProvider(_settings())

    assert await provider.token() == "tok-1"
    assert await provider.token() == "tok-1"
    assert route.call_count == 1


@respx.mock
async def test_the_client_credentials_grant_is_used_not_a_password() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    await TokenProvider(_settings()).token()

    body = route.calls.last.request.content.decode()
    assert "grant_type=client_credentials" in body
    assert "password" not in body


@respx.mock
async def test_a_token_near_expiry_is_refreshed_early() -> None:
    """A token valid for less than the refresh margin is never handed out."""
    route = respx.post(TOKEN_URL).mock(
        side_effect=[_token_response("tok-1", expires_in=30), _token_response("tok-2")]
    )
    provider = TokenProvider(_settings())

    assert await provider.token() == "tok-1"
    assert await provider.token() == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_concurrent_callers_do_not_stampede_the_token_endpoint() -> None:
    route = respx.post(TOKEN_URL).mock(return_value=_token_response())
    provider = TokenProvider(_settings())

    tokens = await asyncio.gather(*(provider.token() for _ in range(8)))

    assert set(tokens) == {"tok-1"}
    assert route.call_count == 1


@respx.mock
async def test_invalidate_forces_a_fresh_token() -> None:
    route = respx.post(TOKEN_URL).mock(
        side_effect=[_token_response("tok-1"), _token_response("tok-2")]
    )
    provider = TokenProvider(_settings())

    assert await provider.token() == "tok-1"
    provider.invalidate()
    assert await provider.token() == "tok-2"
    assert route.call_count == 2


@respx.mock
async def test_rejected_credentials_are_reported_without_echoing_them() -> None:
    respx.post(TOKEN_URL).mock(
        return_value=httpx.Response(401, json={"error": "invalid_client", "client_id": "an-id"})
    )
    provider = TokenProvider(_settings())

    with pytest.raises(AuthRequired) as exc:
        await provider.token()

    message = exc.value.as_tool_message()
    assert "an-id" not in message
    assert "a-secret" not in message


@respx.mock
async def test_a_broken_identity_service_is_an_upstream_error_not_an_auth_error() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="down"))
    with pytest.raises(UpstreamError):
        await TokenProvider(_settings()).token()


@respx.mock
async def test_a_response_without_a_token_is_rejected() -> None:
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(200, json={"expires_in": 600}))
    with pytest.raises(UpstreamError, match="no access_token"):
        await TokenProvider(_settings()).token()


@respx.mock
async def test_the_header_is_ready_to_use() -> None:
    respx.post(TOKEN_URL).mock(return_value=_token_response())
    header = await TokenProvider(_settings()).authorization_header()
    assert header == {"Authorization": "Bearer tok-1"}
