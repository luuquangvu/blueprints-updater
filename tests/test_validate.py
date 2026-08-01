"""Tests for the unified validation entry point."""

import os
import shutil
import stat
from pathlib import Path

import pytest

from tools import validate


def _create_executable(path: Path) -> None:
    """Create a minimal executable at path."""
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)


def test_resolve_global_uv_path_skips_active_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Select global uv even when the active environment appears first on PATH."""
    environment_root = tmp_path / "environment"
    environment_uv = environment_root / "bin" / "uv"
    global_uv = tmp_path / "global" / "bin" / "uv"
    _create_executable(environment_uv)
    _create_executable(global_uv)
    monkeypatch.setattr(validate.sys, "prefix", str(environment_root))
    monkeypatch.setenv("PATH", os.pathsep.join((str(environment_uv.parent), str(global_uv.parent))))

    assert validate.resolve_global_uv_path() == str(global_uv.resolve())
    assert os.environ["PATH"] == str(global_uv.parent.resolve())
    assert shutil.which("uv") == str(global_uv.resolve())


def test_resolve_global_uv_path_keeps_interpreter_bin_outside_virtual_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not mistake a global interpreter's bin directory for an active venv."""
    interpreter_root = tmp_path / "interpreter"
    global_uv = interpreter_root / "bin" / "uv"
    _create_executable(global_uv)
    monkeypatch.setattr(validate.sys, "base_prefix", str(interpreter_root))
    monkeypatch.setattr(validate.sys, "prefix", str(interpreter_root))
    monkeypatch.setenv("PATH", str(global_uv.parent))

    assert validate.resolve_global_uv_path() == str(global_uv.resolve())
    assert os.environ["PATH"] == str(global_uv.parent.resolve())


@pytest.mark.parametrize(
    "unsafe_entry",
    ["relative/bin", "/safe/../bin", "/safe/$USER/bin", "~/bin"],
)
def test_validate_path_entry_rejects_unsafe_paths(unsafe_entry: str) -> None:
    """Reject relative, traversing, expanded, and unsupported PATH expressions."""
    assert validate._validate_path_entry(unsafe_entry) == ""


def test_validate_path_entry_reconstructs_safe_absolute_path() -> None:
    """Reconstruct ordinary absolute PATH entries from the fixed allowlist."""
    assert validate._validate_path_entry("/home/test-user/.local/bin") == (
        "/home/test-user/.local/bin"
    )


def test_pipeline_exits_when_global_uv_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail validation before any steps when PATH only contains environment uv."""
    environment_root = tmp_path / "environment"
    environment_uv = environment_root / "bin" / "uv"
    _create_executable(environment_uv)
    monkeypatch.setattr(validate.sys, "prefix", str(environment_root))
    monkeypatch.setenv("PATH", str(environment_uv.parent))

    with pytest.raises(SystemExit, match="1"):
        validate._run_pipeline()

    output = capsys.readouterr().out
    assert "VALIDATION_ERROR: 'global uv executable' not found." in output
    assert "VALIDATION_FAILED" in output


def test_resolve_global_uv_path_rejects_symlink_into_active_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject a global PATH entry whose uv symlinks back into the active venv."""
    environment_root = tmp_path / "environment"
    environment_uv = environment_root / "bin" / "uv"
    _create_executable(environment_uv)

    spoofed_dir = tmp_path / "spoofed" / "bin"
    spoofed_dir.mkdir(parents=True)
    (spoofed_dir / "uv").symlink_to(environment_uv)

    monkeypatch.setattr(validate.sys, "prefix", str(environment_root))
    monkeypatch.setenv("PATH", str(spoofed_dir))

    with pytest.raises(FileNotFoundError):
        validate.resolve_global_uv_path()
