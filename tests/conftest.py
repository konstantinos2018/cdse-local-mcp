"""Shared test fixtures.

Fixtures under ``tests/fixtures`` are trimmed captures of real CDSE responses. They keep the
traps that matter: the null-href assets and the ``Product``/``product`` casing difference.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from cdse_local_mcp.clients.stac import StacClient
from cdse_local_mcp.config import Settings, get_settings
from cdse_local_mcp.tools import discovery

FIXTURES = Path(__file__).parent / "fixtures"
STAC = "https://stac.dataspace.copernicus.eu/v1"


def load_fixture(name: str) -> dict[str, Any]:
    """Read one captured response."""
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture(autouse=True)
def ignore_developer_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never read the developer's real .env during tests.

    pydantic-settings loads .env by default, so a contributor with credentials configured
    would get different results from one without - and a test asserting "no credentials"
    would silently pass for the wrong reason, or fail confusingly. Found exactly that way.
    """
    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def isolated_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every test's downloads in a temporary folder.

    The default download folder is the user's real Downloads directory, so a test that
    forgot to set one would leave files where a person would find them.
    """
    monkeypatch.setenv("CDSE_DOWNLOAD_DIR", str(tmp_path / "downloads"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def fresh_client() -> Iterator[None]:
    """Give every test its own STAC client, and never leak one between tests."""
    discovery.set_client(StacClient(Settings()))
    yield
    discovery.set_client(None)
