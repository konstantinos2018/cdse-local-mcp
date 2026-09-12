"""Filesystem sandboxing.

This server writes to a user's disk on a language model's instruction, and product names come
from a remote API. Treat every path component as hostile: resolve it, then prove it is inside
the download root before opening anything.
"""

from __future__ import annotations

import re
from pathlib import Path

from cdse_local_mcp.errors import InvalidRequest

# Anything outside this is replaced. Deliberately strict: product names are remote input.
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME_LENGTH = 200


def sanitise_name(name: str) -> str:
    """Reduce a remote-supplied filename to something safe to write.

    Strips directory separators entirely — a name is a single path component, never a path.
    """
    candidate = name.strip().replace("\\", "/").split("/")[-1]
    candidate = _UNSAFE_CHARS.sub("_", candidate).strip("._")

    if not candidate:
        raise InvalidRequest(
            f"{name!r} does not yield a usable filename.",
            hint="Supply a product id or asset name containing ordinary characters.",
        )
    return candidate[:_MAX_NAME_LENGTH]


def ensure_root(root: Path) -> Path:
    """Create the download root if needed and return its resolved path."""
    resolved = root.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_target(root: Path, *parts: str) -> Path:
    """Resolve a path inside the download root, refusing anything that escapes it.

    Catches ``..`` traversal, absolute paths, and symlinks pointing outside the root. The
    check is made on the *resolved* path, because that is the one that will be opened.
    """
    safe_root = ensure_root(root)
    safe_parts = [sanitise_name(part) for part in parts if part]
    if not safe_parts:
        raise InvalidRequest("No filename was given for the download.")

    candidate = safe_root.joinpath(*safe_parts)

    # Resolve the deepest existing ancestor: the leaf usually does not exist yet, but a
    # symlinked parent directory would still escape the root.
    probe = candidate
    while not probe.exists() and probe != safe_root:
        probe = probe.parent
    resolved_ancestor = probe.resolve()

    if resolved_ancestor != safe_root and safe_root not in resolved_ancestor.parents:
        raise InvalidRequest(
            "The download path escapes the download directory.",
            hint=f"Files may only be written under {safe_root}.",
        )

    final = safe_root.joinpath(*safe_parts)
    if not final.is_relative_to(safe_root):  # pragma: no cover - defence in depth
        raise InvalidRequest("The download path escapes the download directory.")
    return final


def partial_path(target: Path) -> Path:
    """The temporary path a transfer writes to before it is complete."""
    return target.with_name(target.name + ".part")
