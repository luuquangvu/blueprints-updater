"""Tests for path manipulation and safety helpers in utils.py."""

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from custom_components.blueprints_updater.utils import (
    get_blueprint_rel_path,
    get_relative_path,
)


def _make_tree(root: Path, rel: str) -> Path:
    """Helper to create a file at root/rel and return its Path."""
    target = root.joinpath(*rel.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x", encoding="utf-8")
    return target


def test_get_relative_path_under_blueprints_root(hass, tmp_path: Path) -> None:
    """A blueprint under the blueprints root returns a forward-slashed relative path."""
    blueprints_root = tmp_path / "blueprints"
    file_path = _make_tree(blueprints_root, "foo/bar/baz.yaml")

    with patch.object(hass.config, "path", return_value=str(blueprints_root)):
        rel = get_relative_path(hass, str(file_path))

    assert rel == "foo/bar/baz.yaml"
    assert "\\" not in rel


def test_get_relative_path_raises_on_escape_attempt(hass, tmp_path: Path) -> None:
    """Paths that escape the root cause get_relative_path to raise."""
    blueprints_root = tmp_path / "blueprints"
    blueprints_root.mkdir(parents=True, exist_ok=True)

    outside_path = tmp_path / "outside.txt"
    outside_path.write_text("x", encoding="utf-8")

    with (
        patch.object(hass.config, "path", return_value=str(blueprints_root)),
        pytest.raises(ValueError, match="escapes"),
    ):
        get_relative_path(hass, str(outside_path))


def test_get_blueprint_rel_path_with_hass(hass, tmp_path: Path, caplog) -> None:
    """Test get_blueprint_rel_path with real HomeAssistant instance (mocked config)."""
    blueprints_root = tmp_path / "blueprints"
    blueprints_root.mkdir(parents=True, exist_ok=True)

    with (
        patch.object(hass.config, "path", return_value=str(blueprints_root)),
        caplog.at_level(logging.DEBUG),
    ):
        file_path = _make_tree(blueprints_root, "automation/test.yaml")
        rel = get_blueprint_rel_path(hass, str(file_path))
        assert rel == "automation/test.yaml"

        outside_path = tmp_path / "evil.yaml"
        outside_path.write_text("content", encoding="utf-8")
        rel = get_blueprint_rel_path(hass, str(outside_path))
        assert rel is None
        assert "escapes" in caplog.text.lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows-only test")
def test_get_relative_path_normalizes_windows_separators(hass, tmp_path: Path) -> None:
    """Windows-style separators are normalized to forward slashes.

    This test only runs on Windows because it relies on OS-native path handling
    of backslashes to verify the integration's normalization logic.
    """
    blueprints_root = tmp_path / "blueprints"
    file_path = _make_tree(blueprints_root, "foo/bar.yaml")

    win_root = str(blueprints_root).replace("/", "\\")
    win_file = str(file_path).replace("/", "\\")

    with patch.object(hass.config, "path", return_value=win_root):
        rel = get_relative_path(hass, win_file)

    assert rel == "foo/bar.yaml"
    assert "\\" not in rel
