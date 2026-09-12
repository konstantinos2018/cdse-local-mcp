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
from cdse_local_mcp.config import Settings
from cdse_local_mcp.tools import discovery

FIXTURES = Path(__file__).parent / "fixtures"
STAC = "https://stac.dataspace.copernicus.eu/v1"


def load_fixture(name: str) -> dict[str, Any]:
    """Read one captured response."""
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


@pytest.fixture(autouse=True)
def fresh_client() -> Iterator[None]:
    """Give every test its own STAC client, and never leak one between tests."""
    discovery.set_client(StacClient(Settings()))
    yield
    discovery.set_client(None)
