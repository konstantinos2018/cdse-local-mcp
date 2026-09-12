"""Tool definitions.

Modules here are thin adapters: validate input, call a client, shape the result. All CDSE
knowledge lives in ``clients`` and ``domain``. Each module exports a list of :class:`ToolDef`
so ``server.py`` does registration and nothing else.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# Side-effect annotations. Snake_case is the Python field name; the MCP wire format uses the
# camelCase aliases, which the SDK applies on serialisation.
READ_ONLY: dict[str, bool] = {"read_only_hint": True, "open_world_hint": True}
DOWNLOADS: dict[str, bool] = {
    "read_only_hint": False,
    "destructive_hint": False,
    "idempotent_hint": True,
    "open_world_hint": True,
}


@dataclass(frozen=True)
class ToolDef:
    """A tool function paired with its side-effect annotations."""

    fn: Callable[..., Awaitable[Any]]
    annotations: dict[str, bool]
