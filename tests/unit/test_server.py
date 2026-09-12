"""Server wiring: the tools a client actually sees."""

from __future__ import annotations

import pytest

from cdse_local_mcp.server import build_server

DISCOVERY_TOOLS = {"search_products", "list_product_assets", "list_collections"}
DOWNLOAD_TOOLS = {
    "download_product",
    "download_assets",
    "download_status",
    "download_cancel",
}
EXPECTED_TOOLS = DISCOVERY_TOOLS | DOWNLOAD_TOOLS


async def test_every_discovery_tool_is_registered() -> None:
    mcp = build_server()
    for name in EXPECTED_TOOLS:
        assert await mcp.get_tool(name) is not None


async def test_discovery_tools_declare_themselves_read_only() -> None:
    mcp = build_server()
    for name in DISCOVERY_TOOLS:
        tool = await mcp.get_tool(name)
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.open_world_hint is True
        # The wire format keeps the camelCase aliases the MCP spec requires.
        assert tool.annotations.model_dump(by_alias=True, exclude_none=True)["readOnlyHint"]


@pytest.mark.parametrize(
    ("tool_name", "required"),
    [
        ("search_products", {"collection", "start_date"}),
        ("list_product_assets", {"collection", "product_id"}),
    ],
)
async def test_tool_schemas_expose_the_expected_inputs(tool_name: str, required: set[str]) -> None:
    mcp = build_server()
    tool = await mcp.get_tool(tool_name)
    properties = set(tool.parameters.get("properties", {}))
    assert required <= properties


async def test_search_description_tells_the_model_to_confirm_before_downloading() -> None:
    """The docstring is the contract the model reads; these clauses are load-bearing."""
    mcp = build_server()
    description = (await mcp.get_tool("search_products")).description or ""
    assert "does not download" in description
    assert "cloud cover" in description.lower()
    assert "west, south, east, north" in description


async def test_server_instructions_state_the_interaction_rules() -> None:
    mcp = build_server()
    instructions = mcp.instructions or ""
    assert "never filtered silently" in instructions.lower()
    assert "EPSG:4326" in instructions


async def test_download_tools_declare_that_they_write() -> None:
    """A client must be able to tell which tools touch the filesystem."""
    mcp = build_server()
    for name in ("download_product", "download_assets", "download_cancel"):
        annotations = (await mcp.get_tool(name)).annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.idempotent_hint is True


async def test_download_status_is_read_only() -> None:
    annotations = (await build_server().get_tool("download_status")).annotations
    assert annotations is not None
    assert annotations.read_only_hint is True


async def test_the_download_tool_warns_about_its_cost_and_what_it_produces() -> None:
    description = (await build_server().get_tool("download_product")).description or ""
    assert "800 MB" in description
    assert "search_products" in description
    assert "confirm=True" in description
    # The S3 transport yields an unpacked directory, which callers must not mistake for a zip.
    assert "directory" in description
    assert "S3 access keys" in description


async def test_selective_download_is_steered_towards_and_explains_olci() -> None:
    """The cheap tool must be the obviously preferable one, or the model reaches past it."""
    description = (await build_server().get_tool("download_assets")).description or ""
    assert "Almost always the right choice" in description
    assert "geo-coordinates" in description  # OLCI is useless without it
    assert "wqsf" in description
