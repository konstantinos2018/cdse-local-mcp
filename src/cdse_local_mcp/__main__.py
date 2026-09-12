"""Console entry point.

Logging goes to stderr. stdout carries the MCP JSON-RPC stream and writing anything to it
corrupts the session.
"""

from __future__ import annotations

import logging
import os
import sys

from cdse_local_mcp.server import build_server


def configure_logging() -> None:
    """Send logs to stderr at the level given by ``CDSE_LOG_LEVEL``."""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("CDSE_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> None:
    """Run the server over stdio."""
    configure_logging()
    build_server().run()


if __name__ == "__main__":
    main()
