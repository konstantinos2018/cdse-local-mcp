"""Filesystem sandboxing. This server writes to disk on a model's instruction."""

from __future__ import annotations

from pathlib import Path

import pytest

from cdse_local_mcp.errors import InvalidRequest
from cdse_local_mcp.transfer.paths import partial_path, resolve_target, sanitise_name


def test_a_normal_product_name_survives_intact() -> None:
    name = "S2A_MSIL1C_20240731T092031_N0511_R093_T34SEH_20240731T112239.SAFE.zip"
    assert sanitise_name(name) == name


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "....//....//etc/shadow",
        "subdir/../../escape.zip",
        "C:\\Windows\\System32\\cfg",
    ],
)
def test_traversal_attempts_collapse_to_a_single_safe_component(hostile: str) -> None:
    safe = sanitise_name(hostile)
    assert "/" not in safe
    assert "\\" not in safe
    assert not safe.startswith(".")


@pytest.mark.parametrize("useless", ["///", "..", ".", "...", "   "])
def test_a_name_with_nothing_salvageable_is_rejected(useless: str) -> None:
    """Better to refuse than to invent a filename from nothing."""
    with pytest.raises(InvalidRequest):
        sanitise_name(useless)


def test_resolved_targets_stay_inside_the_download_root(tmp_path: Path) -> None:
    target = resolve_target(tmp_path, "../../escape.zip")
    assert target.is_relative_to(tmp_path.resolve())


def test_absolute_paths_are_confined_to_the_root(tmp_path: Path) -> None:
    target = resolve_target(tmp_path, "/etc/passwd")
    assert target.is_relative_to(tmp_path.resolve())
    assert target.name == "passwd"


def test_a_symlinked_directory_pointing_outside_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "downloads"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(InvalidRequest, match="escapes the download directory"):
        resolve_target(root, "escape", "loot.zip")


def test_the_root_is_created_on_demand(tmp_path: Path) -> None:
    root = tmp_path / "not" / "yet" / "there"
    target = resolve_target(root, "product.zip")
    assert root.exists()
    assert target.parent == root.resolve()


def test_partial_files_are_named_predictably(tmp_path: Path) -> None:
    assert partial_path(tmp_path / "a.zip").name == "a.zip.part"
