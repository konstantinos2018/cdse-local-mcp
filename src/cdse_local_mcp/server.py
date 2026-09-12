"""FastMCP instance and tool registration. No logic belongs in this module."""

from __future__ import annotations

import logging

from fastmcp import FastMCP

from cdse_local_mcp import __version__
from cdse_local_mcp.config import get_settings
from cdse_local_mcp.tools import ToolDef, discovery

logger = logging.getLogger(__name__)

INSTRUCTIONS = """
Access to the Copernicus Data Space Ecosystem: search the Sentinel catalogue and retrieve
products.

Workflow: search_products to find candidates, list_product_assets to see what is inside one,
then present the options to the user and let them choose before downloading anything.

Two rules this server enforces rather than assumes:
  - Cloud cover is never filtered silently. For optical collections, ask the user for a
    threshold, then pass a number or "any".
  - Downloads take explicit product ids that came from a search. There is no tool that goes
    from a place name straight to bytes on disk.

Bounding boxes are [west, south, east, north] in EPSG:4326 lon/lat order. For a place name,
supply its approximate bounding box; the box used is echoed back so the user can check it.
""".strip()


def build_server() -> FastMCP:
    """Create the server and register every tool."""
    mcp: FastMCP = FastMCP(name="cdse-local-mcp", version=__version__, instructions=INSTRUCTIONS)

    definitions: list[ToolDef] = [*discovery.TOOLS]
    for tool in definitions:
        mcp.tool(tool.fn, annotations=tool.annotations)

    settings = get_settings()
    logger.info(
        "cdse-local-mcp %s ready: %d tools, oauth=%s, s3=%s",
        __version__,
        len(definitions),
        settings.has_oauth,
        settings.has_s3,
    )
    if not settings.has_oauth:
        logger.info(
            "No CDSE_CLIENT_ID/CDSE_CLIENT_SECRET configured: catalogue search works, "
            "downloads will not."
        )

    return mcp
