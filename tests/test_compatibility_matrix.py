"""Tests for the declared Home Assistant compatibility range."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import orjson
import pytest
from packaging.version import Version

from tools import validate_compatibility
from tools.validate_compatibility import (
    PythonVersionIncompatibilityError,
    _parse_requirements_dependency_version,
    _test_matrix,
    _validate_python_bin,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SUPPORTED_RANGE_BOUNDARIES = [
    ("2024.12.0", "0.13.190", "3.12"),
    ("2025.1.4", "0.13.205", "3.12"),
    ("2025.2.0", "0.13.210", "3.13"),
    ("2026.2.3", "0.13.316", "3.13"),
    ("2026.3.1", "0.13.317", "3.14"),
    ("latest", "latest", "3.14"),
]


def test_compatibility_matrix_covers_declared_supported_range() -> None:
    """Keep the compatibility matrix aligned with the HACS support contract."""
    hacs_config = orjson.loads((_REPO_ROOT / "hacs.json").read_bytes())
    matrix = _test_matrix()

    minimum_supported = Version(hacs_config["homeassistant"])
    fixed_rows = [row for row in matrix if row["ha_ver"] != "latest"]
    fixed_versions = [Version(row["ha_ver"]) for row in fixed_rows]
    python_versions = [Version(row["python_ver"]) for row in matrix]
    boundaries = [(row["ha_ver"], row["harness_ver"], row["python_ver"]) for row in matrix]

    assert fixed_versions
    assert fixed_versions[0] == minimum_supported
    assert fixed_versions == sorted(set(fixed_versions))
    assert all(version >= minimum_supported for version in fixed_versions)
    assert boundaries == _SUPPORTED_RANGE_BOUNDARIES
    assert python_versions == sorted(python_versions)


def test_parse_requirements_dependency_version() -> None:
    """Test parsing package constraint version from requirement text."""
    constraints = """
    # Comment line
    invalid/package_name==1.0.0
    unpinned-package >= 2.0.0
    pytest-cov==4.1.0
    pytest-cov==5.0.0
    PyYAML==6.0.1; python_version >= '3.10'
    httpx[http2]==0.27.0
    """
    assert _parse_requirements_dependency_version(constraints, "pytest_cov") == "4.1.0"
    assert _parse_requirements_dependency_version(constraints, "pyyaml") == "6.0.1"
    assert _parse_requirements_dependency_version(constraints, "httpx") == "0.27.0"
    assert _parse_requirements_dependency_version(constraints, "httpx[http2]") == "0.27.0"

    with pytest.raises(ValueError, match="Could not find 'nonexistent-package'"):
        _parse_requirements_dependency_version(constraints, "nonexistent-package")

    invalid_constraints = "broken-package=="
    with pytest.raises(ValueError, match="expected a version after '=='"):
        _parse_requirements_dependency_version(invalid_constraints, "broken-package")

    malformed_constraints = "broken-package==not-a-version"
    with pytest.raises(ValueError, match="Invalid"):
        _parse_requirements_dependency_version(malformed_constraints, "broken-package")


def test_compatibility_main_configures_global_uv_before_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configure the global uv PATH before running an uv-dependent mode."""
    python_bin = Path(".venv/bin/python")
    resolve_global_uv = MagicMock(return_value="/global/bin/uv")
    verify_pair = MagicMock(return_value=True)
    monkeypatch.setattr(validate_compatibility, "resolve_global_uv_path", resolve_global_uv)
    monkeypatch.setattr(validate_compatibility, "_verify_harness_pair", verify_pair)
    monkeypatch.setattr(
        validate_compatibility.sys,
        "argv",
        [
            "validate_compatibility.py",
            "--verify-pair-python",
            str(python_bin),
            "--expected-ha",
            "2026.8.0b3",
            "--expected-harness",
            "0.13.351",
        ],
    )

    validate_compatibility.main()

    resolve_global_uv.assert_called_once_with()
    verify_pair.assert_called_once_with(python_bin, "2026.8.0b3", "0.13.351")


def test_compatibility_main_exits_when_global_uv_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Stop compatibility validation when no uv exists outside the active environment."""
    missing_uv = FileNotFoundError(2, "not found", "global uv executable")
    monkeypatch.setattr(
        validate_compatibility,
        "resolve_global_uv_path",
        MagicMock(side_effect=missing_uv),
    )
    monkeypatch.setattr(validate_compatibility.sys, "argv", ["validate_compatibility.py"])

    with pytest.raises(SystemExit, match="1"):
        validate_compatibility.main()

    output = capsys.readouterr().out
    assert "VALIDATION_ERROR: 'global uv executable' not found." in output


def test_refresh_dependencies_preserves_selection_and_legacy_transitive_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Apply legacy transitive constraints during a targeted dependency refresh."""
    calls: list[tuple[Path, tuple[str, ...], str]] = []

    def record_install(
        python_bin: Path,
        package_args: tuple[str, ...] | list[str],
        step_label: str,
    ) -> None:
        calls.append((python_bin, tuple(package_args), step_label))

    monkeypatch.setattr(validate_compatibility, "_run_uv_pip_install", record_install)
    python_bin = Path("python")
    selected = ("aiodns==3.5.0", "home-assistant-intents==2025.10.1")

    validate_compatibility._refresh_compatibility_dependencies(
        python_bin,
        selected,
        {"aiodns": "3.5.0"},
    )

    assert calls == [
        (
            python_bin,
            (*selected, "pycares<5"),
            "aiodns==3.5.0 home-assistant-intents==2025.10.1",
        )
    ]


def test_validate_python_bin_accepts_valid_virtualenv_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accept valid Python binary paths located inside the workspace."""
    fake_bin = tmp_path / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    python_file = fake_bin / "python"
    python_file.write_text("#!/bin/sh\nexit 0\n")
    python_file.chmod(0o755)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(tmp_path))
    valid_path = Path(".venv/bin/python")
    result = _validate_python_bin(valid_path)
    assert result == valid_path
    assert _validate_python_bin(str(valid_path)) == valid_path


def test_validate_python_bin_rejects_symlink_to_untrusted_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject virtualenv symlinks pointing to unauthorized target directories."""
    untrusted_dir = tmp_path / "untrusted"
    untrusted_bin = untrusted_dir / "bin"
    untrusted_bin.mkdir(parents=True)
    untrusted_python = untrusted_bin / "python"
    untrusted_python.write_text("#!/bin/sh\nexit 0\n")
    untrusted_python.chmod(0o755)

    repo_dir = tmp_path / "repo"
    repo_bin = repo_dir / ".venv" / "bin"
    repo_bin.mkdir(parents=True)
    symlink_python = repo_bin / "python"
    symlink_python.symlink_to(untrusted_python)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(repo_dir))
    with pytest.raises(ValueError, match="outside permitted toolchain directories"):
        _validate_python_bin(Path(".venv/bin/python"))


@pytest.mark.parametrize(
    ("invalid_input", "expected_err"),
    [
        (123, "expected a Path or str"),
        (None, "expected a Path or str"),
        ("", "cannot be empty"),
        ("   ", "cannot be empty"),
        ("--version", "cannot start with '-'"),
        (".venv/bin/python;rm", "character ';' is not allowed"),
        ("../../usr/bin/python3", "directory traversal"),
        (".venv/bin/sh", "must be a python executable"),
        (".venv/bin/pytest", "must be a python executable"),
        (".venv/bin/python.exe", "must be a python executable"),
        ("/usr/bin/python3", "escapes allowed repository root"),
        (".venv/bin/python999", "Python executable not found"),
    ],
)
def test_validate_python_bin_rejects_unsafe_or_invalid_paths(
    invalid_input: object,
    expected_err: str,
) -> None:
    """Reject invalid types, flags, command injection, path traversal, or escaping paths."""
    with pytest.raises(ValueError, match=expected_err):
        _validate_python_bin(invalid_input)


@pytest.mark.parametrize(
    ("root_env_var", "root_sys_attr"),
    [
        (None, "base_prefix"),
        (None, "prefix"),
        ("UV_PYTHON_INSTALL_DIR", None),
        ("XDG_DATA_HOME", None),
    ],
)
def test_validate_python_bin_accepts_symlink_to_permitted_target_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    root_env_var: str | None,
    root_sys_attr: str | None,
) -> None:
    """Accept virtualenv symlinks pointing to authorized toolchain roots."""
    target_root = tmp_path / "toolchain_root"
    target_bin = target_root / "bin"
    if root_env_var == "XDG_DATA_HOME":
        target_bin = target_root / "uv" / "python" / "bin"
    target_bin.mkdir(parents=True)
    target_python = target_bin / "python3"
    target_python.write_text("#!/bin/sh\nexit 0\n")
    target_python.chmod(0o755)

    if root_env_var is not None:
        monkeypatch.setenv(root_env_var, str(target_root))
    if root_sys_attr is not None:
        monkeypatch.setattr(validate_compatibility.sys, root_sys_attr, str(target_root))

    repo_dir = tmp_path / "repo"
    repo_bin = repo_dir / ".venv" / "bin"
    repo_bin.mkdir(parents=True)
    symlink_python = repo_bin / "python"
    symlink_python.symlink_to(target_python)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(repo_dir))
    valid_path = Path(".venv/bin/python")
    assert _validate_python_bin(valid_path) == valid_path


def test_validate_python_bin_rejects_non_executable_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject Python binary that lacks execute permission."""
    fake_bin = tmp_path / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    python_file = fake_bin / "python"
    python_file.write_text("#!/bin/sh\nexit 0\n")
    python_file.chmod(0o644)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="is not executable"):
        _validate_python_bin(Path(".venv/bin/python"))


def test_validate_python_bin_rejects_broken_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject Python symlinks whose target file does not exist."""
    fake_bin = tmp_path / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    symlink_python = fake_bin / "python"
    symlink_python.symlink_to(tmp_path / "nonexistent" / "python")

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="Python executable not found at"):
        _validate_python_bin(Path(".venv/bin/python"))


def test_validate_python_bin_rejects_symlink_to_non_python_basename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject symlinks whose resolved target basename is not a valid Python name."""
    repo_dir = tmp_path / "repo"
    repo_bin = repo_dir / ".venv" / "bin"
    repo_bin.mkdir(parents=True)
    target_sh = repo_dir / "bin" / "not_python"
    target_sh.parent.mkdir(parents=True)
    target_sh.write_text("#!/bin/sh\nexit 0\n")
    target_sh.chmod(0o755)

    symlink_python = repo_bin / "python"
    symlink_python.symlink_to(target_sh)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(repo_dir))
    with pytest.raises(ValueError, match="must be a python executable"):
        _validate_python_bin(Path(".venv/bin/python"))


def test_validate_python_bin_rejects_target_outside_bin_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject symlinks whose resolved target does not reside in a bin directory."""
    repo_dir = tmp_path / "repo"
    repo_bin = repo_dir / ".venv" / "bin"
    repo_bin.mkdir(parents=True)
    target_py = repo_dir / "scripts" / "python"
    target_py.parent.mkdir(parents=True)
    target_py.write_text("#!/bin/sh\nexit 0\n")
    target_py.chmod(0o755)

    symlink_python = repo_bin / "python"
    symlink_python.symlink_to(target_py)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(repo_dir))
    with pytest.raises(ValueError, match="must reside in a 'bin' directory"):
        _validate_python_bin(Path(".venv/bin/python"))


def test_validate_python_bin_rejects_non_executable_resolved_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject Python binary when resolved target is not executable."""
    fake_bin = tmp_path / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    python_file = fake_bin / "python"
    python_file.write_text("#!/bin/sh\nexit 0\n")
    python_file.chmod(0o755)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(tmp_path))

    real_access = validate_compatibility.os.access
    target_path_str = str(python_file.resolve())
    check_count = 0

    def fake_access(path: str | os.PathLike[str] | int, mode: int) -> bool:
        nonlocal check_count
        if str(path) == target_path_str and mode == validate_compatibility.os.X_OK:
            check_count += 1
            return check_count <= 1
        return real_access(path, mode)

    monkeypatch.setattr(validate_compatibility.os, "access", fake_access)
    with pytest.raises(ValueError, match=r"Resolved Python binary for .* is not executable"):
        _validate_python_bin(Path(".venv/bin/python"))


def test_validate_python_bin_rejects_unresolved_target_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject Python binary when resolved target is not a file."""
    fake_bin = tmp_path / ".venv" / "bin"
    fake_bin.mkdir(parents=True)
    python_file = fake_bin / "python"
    python_file.write_text("#!/bin/sh\nexit 0\n")
    python_file.chmod(0o755)

    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(tmp_path))

    real_isfile = validate_compatibility.os.path.isfile
    target_path_str = str(python_file.resolve())
    check_count = 0

    def fake_isfile(path: str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> bool:
        nonlocal check_count
        if str(path) == target_path_str:
            check_count += 1
            return check_count <= 1
        return real_isfile(path)

    monkeypatch.setattr(validate_compatibility.os.path, "isfile", fake_isfile)
    with pytest.raises(ValueError, match="Resolved Python executable not found for"):
        _validate_python_bin(Path(".venv/bin/python"))


@pytest.fixture
def fake_venv_env(tmp_path: Path) -> tuple[Path, Path]:
    """Provide a temporary virtual environment path and mock python binary."""
    venv_path = tmp_path / ".venv-test"
    python_bin = venv_path / "bin" / "python"
    python_bin.parent.mkdir(parents=True, exist_ok=True)
    python_bin.write_text("#!/bin/sh\nexit 0\n")
    return venv_path, python_bin


def _run_prepare_venv(
    venv_path: Path,
    python_bin: Path,
    *,
    test_deps: dict[str, str] | None = None,
) -> bool:
    """Run _prepare_venv_and_install with standard matrix parameters."""
    return validate_compatibility._prepare_venv_and_install(
        venv_path=venv_path,
        python_bin=python_bin,
        ha_ver="2026.2.3",
        ha_ver_to_install="2026.2.3",
        py_ver="3.13",
        reinstall=False,
        test_dependency_versions={"aiodns": "3.5.0"} if test_deps is None else test_deps,
    )


def test_prepare_venv_and_install_resets_stale_venv_before_compatibility_verification(
    monkeypatch: pytest.MonkeyPatch,
    fake_venv_env: tuple[Path, Path],
) -> None:
    """Ensure stale environments verify Python compatibility before installation."""
    venv_path, python_bin = fake_venv_env
    call_order: list[str] = []

    def fake_install(*args: object, **kwargs: object) -> None:
        call_order.append("install")
        assert kwargs.get("reset_before_install") is True

    monkeypatch.setattr(validate_compatibility, "_ensure_venv", lambda *_: False)
    monkeypatch.setattr(validate_compatibility, "_get_installed_ha_version", lambda *_: "2026.2.2")
    monkeypatch.setattr(
        validate_compatibility,
        "_dependency_marker_requires_reinstall",
        lambda *_: True,
    )
    monkeypatch.setattr(
        validate_compatibility,
        "_determine_dependency_actions",
        lambda *_: (True, ()),
    )
    monkeypatch.setattr(validate_compatibility, "_install_dependencies", fake_install)
    monkeypatch.setattr(
        validate_compatibility,
        "_verify_python_version_compatibility",
        lambda *_: call_order.append("verify"),
    )

    success = _run_prepare_venv(venv_path, python_bin)

    assert success is True
    assert call_order == ["verify", "install"]


def test_prepare_venv_and_install_retries_and_recovers_on_python_incompatibility(
    monkeypatch: pytest.MonkeyPatch,
    fake_venv_env: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Reset and retry when existing virtual environment fails Python compatibility."""
    venv_path, python_bin = fake_venv_env
    call_order: list[str] = []
    verify_attempts = 0

    def fake_reset(path: Path, py: str) -> bool:
        call_order.append("reset")
        return True

    def fake_verify(py_bin: Path, ha_ver: str) -> None:
        nonlocal verify_attempts
        verify_attempts += 1
        call_order.append("verify")
        if verify_attempts == 1:
            raise PythonVersionIncompatibilityError("Incompatible interpreter")

    def fake_install(*args: object, **kwargs: object) -> None:
        call_order.append("install")
        assert kwargs.get("reset_before_install") is False

    monkeypatch.setattr(validate_compatibility, "_reset_venv", fake_reset)
    monkeypatch.setattr(validate_compatibility, "_ensure_venv", lambda *_: False)
    monkeypatch.setattr(validate_compatibility, "_get_installed_ha_version", lambda *_: "2026.2.3")
    monkeypatch.setattr(
        validate_compatibility,
        "_dependency_marker_requires_reinstall",
        lambda *_: False,
    )
    monkeypatch.setattr(
        validate_compatibility,
        "_determine_dependency_actions",
        lambda *_: (True, ()),
    )
    monkeypatch.setattr(validate_compatibility, "_install_dependencies", fake_install)
    monkeypatch.setattr(validate_compatibility, "_verify_python_version_compatibility", fake_verify)

    success = _run_prepare_venv(venv_path, python_bin)

    assert success is True
    assert call_order == ["verify", "reset", "verify", "install"]
    captured = capsys.readouterr()
    assert "automatically resetting environment" in captured.out


@pytest.mark.parametrize(
    ("created_venv", "exc", "expect_reset_warning"),
    [
        (
            False,
            PythonVersionIncompatibilityError("Incompatible Python interpreter for HA 2026.2.3"),
            True,
        ),
        (
            False,
            ValueError("Incompatible Python interpreter for HA 2026.2.3"),
            False,
        ),
        (
            True,
            PythonVersionIncompatibilityError("Incompatible Python interpreter for HA 2026.2.3"),
            False,
        ),
    ],
)
def test_prepare_venv_and_install_reports_error_when_compatibility_verification_fails(
    monkeypatch: pytest.MonkeyPatch,
    fake_venv_env: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    created_venv: bool,
    exc: Exception,
    expect_reset_warning: bool,
) -> None:
    """Report error and return False when Python version compatibility check fails."""
    venv_path, python_bin = fake_venv_env
    reset_called = False

    def fake_reset(*args: object, **kwargs: object) -> bool:
        nonlocal reset_called
        reset_called = True
        return True

    def failing_verify(py_bin: Path, ha_ver: str) -> None:
        raise exc

    monkeypatch.setattr(validate_compatibility, "_reset_venv", fake_reset)
    monkeypatch.setattr(validate_compatibility, "_ensure_venv", lambda *_: created_venv)
    monkeypatch.setattr(validate_compatibility, "_get_installed_ha_version", lambda *_: "2026.2.3")
    monkeypatch.setattr(
        validate_compatibility,
        "_dependency_marker_requires_reinstall",
        lambda *_: False,
    )
    monkeypatch.setattr(
        validate_compatibility,
        "_verify_python_version_compatibility",
        failing_verify,
    )

    success = _run_prepare_venv(venv_path, python_bin)

    assert success is False
    assert reset_called is expect_reset_warning
    captured = capsys.readouterr()
    if expect_reset_warning:
        assert "automatically resetting environment" in captured.out
    else:
        assert "automatically resetting environment" not in captured.out
    assert "VALIDATION_ERROR: Incompatible Python interpreter for HA 2026.2.3" in captured.out


def test_prepare_venv_and_install_returns_false_when_python_bin_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Report error and return False when python binary does not exist."""
    venv_path = tmp_path / ".venv-test"
    python_bin = venv_path / "bin" / "python"

    monkeypatch.setattr(validate_compatibility, "_ensure_venv", lambda *_: False)

    success = _run_prepare_venv(venv_path, python_bin, test_deps={})

    assert success is False
    captured = capsys.readouterr()
    assert f"VALIDATION_ERROR: python not found at {python_bin}" in captured.out
