"""Tests for the declared Home Assistant compatibility range."""

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
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
    ("2026.10.4", "0.13.375", "3.14"),
    ("2026.11.0", "2026.11.0", "3.14"),
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


def test_resolve_test_dependency_versions_pins_httpx_and_installs_h2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin HTTPX to Home Assistant's constraint and install HTTP/2 support separately."""

    def fake_required_version(ha_ver: str, package_name: str) -> str:
        assert ha_ver == "2026.11.0b5"
        return {"aiodns": "4.0.4", "httpx": "0.28.1"}[package_name]

    monkeypatch.setattr(
        validate_compatibility,
        "_get_required_package_version",
        fake_required_version,
    )

    resolved = validate_compatibility._resolve_test_dependency_versions(
        "2026.11.0b5",
        "2026.11.0b5",
    )

    assert resolved == {
        "aiodns": "4.0.4",
        "httpx": "0.28.1",
        "pytest-homeassistant": "2026.11.0b5",
    }
    required = validate_compatibility._required_test_deps(resolved)
    assert "httpx==0.28.1" in required
    assert "h2" in required
    harness_deps = [
        dep
        for dep in required
        if validate_compatibility.normalize_package_name(dep.split("==")[0])
        in validate_compatibility._TEST_HARNESS_PACKAGES
    ]
    assert harness_deps == ["pytest-homeassistant==2026.11.0b5"]


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


def _setup_repo_venv_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    executable: bool = True,
) -> Path:
    """Configure repo root and create a mock virtual environment python binary."""
    python_file = tmp_path / ".venv" / "bin" / "python"
    python_file.parent.mkdir(parents=True)
    python_file.write_text("#!/bin/sh\nexit 0\n")
    python_file.chmod(0o755 if executable else 0o644)
    monkeypatch.setattr(validate_compatibility, "_REPO_ROOT", str(tmp_path))
    return python_file


def _make_releases_payload(*versions: str) -> dict[str, dict[str, list[dict[str, str]]]]:
    """Construct a mock PyPI releases dictionary from version strings."""
    return {"releases": {ver: [{"filename": f"{ver}.whl"}] for ver in versions}}


def _json_text(payload: object) -> str:
    """Serialize a mock PyPI payload as JSON text."""
    return validate_compatibility.orjson.dumps(payload).decode()


def _lagging_harness_payload(
    root_ha_version: str,
    root_version: str = "0.13.376",
) -> str:
    """Return harness metadata whose root info lags behind its newest release."""
    return _json_text(
        {
            "info": {
                "version": root_version,
                "requires_dist": [f"homeassistant=={root_ha_version}"],
            },
            "releases": {
                "0.13.375": [{"filename": "older-harness.whl"}],
                "0.13.376": [{"filename": "newer-harness.whl"}],
            },
        }
    )


def _legacy_harness_payload(ha_version: str = "2026.10.4") -> str:
    """Return legacy harness metadata pinned to one Home Assistant release."""
    return _json_text(
        {
            "info": {
                "version": "0.13.375",
                "requires_dist": [f"homeassistant=={ha_version}"],
            },
            "releases": {"0.13.375": [{"filename": "legacy.whl"}]},
        }
    )


def _harness_version_url(version: str) -> str:
    """Return the mock PyPI URL for one legacy harness release."""
    return validate_compatibility._PYPI_TEST_HARNESS_VERSION_JSON_URL_TEMPLATE.format(
        version=version
    )


def _mock_remote_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: Mapping[str, str | Exception],
    fetched_urls: list[str] | None = None,
) -> None:
    """Install a fake remote fetcher backed by a URL-to-response mapping."""

    def fake_fetch(url: str) -> str:
        if fetched_urls is not None:
            fetched_urls.append(url)
        response = responses.get(url)
        if isinstance(response, Exception):
            raise response
        if isinstance(response, str):
            return response
        raise AssertionError(f"Unexpected URL fetched: {url}")

    monkeypatch.setattr(validate_compatibility, "_fetch_remote_text", fake_fetch)


def _mock_legacy_only_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock a stable Home Assistant release served only by the legacy harness."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_HA_JSON_URL: _json_text(
                _make_releases_payload("2026.10.4")
            ),
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _legacy_harness_payload(),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_JSON_URL: OSError(
                "new harness index unavailable"
            ),
        },
    )


def test_matched_pair_fetches_version_specific_metadata_when_root_info_lags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Version-specific metadata must be fetched when root PyPI info version lags."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _json_text(
                {
                    "info": {
                        "version": "0.13.348",
                        "requires_dist": ["homeassistant==2026.7.4"],
                    },
                    "releases": {
                        "0.13.348": [{"filename": "old.whl"}],
                        "0.13.349": [{"filename": "latest.whl"}],
                    },
                }
            ),
            _harness_version_url("0.13.349"): _json_text(
                {
                    "info": {
                        "version": "0.13.349",
                        "requires_dist": ["homeassistant==2026.8.0"],
                    }
                }
            ),
        },
    )

    assert validate_compatibility._matched_pair_for_package(
        validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
        {
            "2026.7.4": [{"filename": "stable.whl"}],
            "2026.8.0": [{"filename": "matched.whl"}],
        },
    ) == ("2026.8.0", "0.13.349")


@pytest.mark.parametrize(
    ("releases", "expected"),
    [
        (
            ("0.13.9", "0.13.10", "0.13.99", "0.13.100", "0.13.2"),
            ["0.13.100", "0.13.99", "0.13.10", "0.13.9", "0.13.2"],
        ),
        (
            ("0.13.10", "0.13.9+local", "0.13.8-rc1", "0.13.7"),
            ["0.13.10", "0.13.7"],
        ),
    ],
)
def test_published_versions_descending_filters_and_orders(
    releases: tuple[str, ...],
    expected: list[str],
) -> None:
    """Sort published versions by PEP 440 semantics and skip invalid labels."""
    payload = _make_releases_payload(*releases)
    assert (
        validate_compatibility._published_versions_descending(
            payload, "pytest-homeassistant-custom-component"
        )
        == expected
    )


def test_published_versions_descending_raises_when_all_labels_rejected() -> None:
    """Raise ValueError when all published releases have rejected version labels."""
    payload = _make_releases_payload("0.13.9+local", "0.13.8-rc1")
    expected_err = "PyPI returned no published pytest-homeassistant-custom-component releases"
    with pytest.raises(ValueError, match=expected_err):
        validate_compatibility._published_versions_descending(
            payload, "pytest-homeassistant-custom-component"
        )


def test_matched_pair_treats_missing_harness_releases_as_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Treat a harness package with no published releases as an ordinary no-match."""
    monkeypatch.setattr(
        validate_compatibility,
        "_fetch_remote_text",
        lambda _url: '{"info": {"version": "0.0.0"}, "releases": {}}',
    )

    assert (
        validate_compatibility._matched_pair_for_package(
            validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
            {"2026.8.0": [{"filename": "ha.whl"}]},
        )
        is None
    )


def test_matched_pair_propagates_malformed_harness_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Propagate malformed harness release metadata instead of treating it as no match."""
    monkeypatch.setattr(
        validate_compatibility,
        "_fetch_remote_text",
        lambda _url: '{"info": {"version": "0.13.1"}, "releases": []}',
    )

    with pytest.raises(ValueError, match="has no releases object"):
        validate_compatibility._matched_pair_for_package(
            validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
            {"2026.8.0": [{"filename": "ha.whl"}]},
        )


def test_matched_pair_prefers_newest_matching_ha_over_newest_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer the greatest matching Home Assistant version within the candidate window."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _lagging_harness_payload(
                "2026.9.0"
            ),
            _harness_version_url("0.13.375"): _json_text(
                {
                    "info": {
                        "version": "0.13.375",
                        "requires_dist": ["homeassistant==2026.10.0"],
                    }
                }
            ),
        },
    )

    assert validate_compatibility._matched_pair_for_package(
        validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
        {
            "2026.9.0": [{"filename": "old-ha.whl"}],
            "2026.10.0": [{"filename": "new-ha.whl"}],
        },
    ) == ("2026.10.0", "0.13.375")


def test_matched_pair_stops_after_newest_ha_for_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop scanning once the newest Home Assistant release for the package is matched."""
    ha_releases: dict[str, object] = {"2026.10.4": [{"filename": "ha.whl"}]}
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _lagging_harness_payload(
                "2026.10.4"
            ),
        },
    )

    assert validate_compatibility._matched_pair_for_package(
        validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
        ha_releases,
    ) == ("2026.10.4", "0.13.376")


def test_matched_pair_ignores_failed_lookup_for_later_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable older candidate must not invalidate an already matched pair."""
    ha_releases: dict[str, object] = {
        "2026.9.0": [{"filename": "old-ha.whl"}],
        "2026.10.0": [{"filename": "new-ha.whl"}],
    }
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _lagging_harness_payload(
                "2026.9.0"
            ),
            _harness_version_url("0.13.375"): OSError("transient PyPI failure"),
        },
    )

    assert validate_compatibility._matched_pair_for_package(
        validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
        ha_releases,
    ) == ("2026.9.0", "0.13.376")


def test_latest_matched_pair_surfaces_unusable_harness_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surface harness metadata errors instead of reporting publication lag."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_HA_JSON_URL: _json_text(
                _make_releases_payload("2026.10.4")
            ),
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _json_text(
                {
                    "info": {
                        "version": "0.13.375",
                        "requires_dist": ["homeassistant>=2026.10.4"],
                    },
                    "releases": {"0.13.375": [{"filename": "harness.whl"}]},
                }
            ),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_JSON_URL: _json_text(
                {"info": {"version": "0.1.3"}, "releases": {}}
            ),
        },
    )

    with pytest.raises(
        ValueError,
        match=(
            r"Failed to resolve latest matched harness-backed Home Assistant pair: "
            r".*must pin Home Assistant with one exact version"
        ),
    ):
        validate_compatibility._get_latest_matched_pair()


def test_matched_pair_bounds_per_release_pypi_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounded recent candidate window fetches the newest candidates in descending order."""
    fetched_urls: list[str] = []

    # 35 releases spanning 0.13.80 through 0.13.114
    harness_releases = {
        f"0.13.{80 + i}": [{"filename": f"harness-{80 + i}.whl"}] for i in range(35)
    }

    responses = {
        validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _json_text(
            {
                "info": {
                    "version": "0.13.80",
                    "requires_dist": ["homeassistant==2026.6.0"],
                },
                "releases": harness_releases,
            }
        ),
    }
    responses.update(
        {
            _harness_version_url(f"0.13.{80 + i}"): _json_text(
                {
                    "info": {
                        "version": "0.13.999",
                        "requires_dist": ["homeassistant==2026.6.0"],
                    }
                }
            )
            for i in range(35)
        }
    )
    _mock_remote_responses(monkeypatch, responses, fetched_urls)

    assert (
        validate_compatibility._matched_pair_for_package(
            validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
            {"2026.7.0": [{"filename": "ha.whl"}]},
        )
        is None
    )

    fetched_version_urls = [
        url
        for url in fetched_urls
        if url != validate_compatibility._PYPI_TEST_HARNESS_JSON_URL
        and "/pytest-homeassistant-custom-component/" in url
    ]
    assert len(fetched_version_urls) == validate_compatibility._MAX_HARNESS_CANDIDATES
    expected_descending_urls = [
        validate_compatibility._PYPI_TEST_HARNESS_VERSION_JSON_URL_TEMPLATE.format(
            version=f"0.13.{114 - i}"
        )
        for i in range(validate_compatibility._MAX_HARNESS_CANDIDATES)
    ]
    assert fetched_version_urls == expected_descending_urls
    # Ensure older candidates (e.g., 0.13.80 through 0.13.89) were never queried
    for older_version in range(80, 90):
        older_url = validate_compatibility._PYPI_TEST_HARNESS_VERSION_JSON_URL_TEMPLATE.format(
            version=f"0.13.{older_version}"
        )
        assert older_url not in fetched_version_urls


def test_matched_pair_falls_back_when_newest_harness_targets_unpublished_ha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The matched pair resolution should skip harnesses targeting unpublished HA releases."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _json_text(
                {
                    "info": {
                        "version": "0.13.349",
                        "requires_dist": ["homeassistant==2026.8.0"],
                    },
                    "releases": {
                        "0.13.349": [{"filename": "matched.whl"}],
                        "0.13.350": [{"filename": "unmatched.whl"}],
                    },
                }
            ),
            _harness_version_url("0.13.350"): _json_text(
                {
                    "info": {
                        "version": "0.13.350",
                        "requires_dist": ["homeassistant==2026.8.1"],
                    }
                }
            ),
        },
    )

    assert validate_compatibility._matched_pair_for_package(
        validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
        {"2026.8.0": [{"filename": "ha.whl"}]},
    ) == ("2026.8.0", "0.13.349")


def test_matched_pair_skips_harness_targeting_other_harness_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ignore harness releases whose Home Assistant target uses another harness family."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _lagging_harness_payload(
                "2026.10.4",
                root_version="0.13.375",
            ),
            _harness_version_url("0.13.376"): _json_text(
                {
                    "info": {
                        "version": "0.13.376",
                        "requires_dist": ["homeassistant==2026.11.0b5"],
                    }
                }
            ),
        },
    )

    assert validate_compatibility._matched_pair_for_package(
        validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
        {
            "2026.10.4": [{"filename": "stable.whl"}],
            "2026.11.0b5": [{"filename": "beta.whl"}],
        },
    ) == ("2026.10.4", "0.13.375")


@pytest.mark.parametrize(
    ("version_payload_or_error", "expected_pattern"),
    [
        (
            OSError("Network connection error"),
            r"Failed to resolve latest matched.*Network connection error",
        ),
        (
            '{"info": "invalid_info_type"}',
            r"Failed to resolve latest matched.*0\.13\.2 PyPI metadata has no info object",
        ),
    ],
)
def test_matched_pair_propagates_candidate_version_errors(
    monkeypatch: pytest.MonkeyPatch,
    version_payload_or_error: str | Exception,
    expected_pattern: str,
) -> None:
    """Version-specific PyPI network and structure errors propagate without falling back."""
    endpoint_responses: dict[str, str | Exception] = {
        validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: (
            '{"info": {"version": "0.13.1", "requires_dist": ["homeassistant==2026.1.0"]}, '
            '"releases": {"0.13.1": [{"filename": "a.whl"}], "0.13.2": [{"filename": "b.whl"}]}}'
        ),
        validate_compatibility._PYPI_TEST_HARNESS_VERSION_JSON_URL_TEMPLATE.format(
            version="0.13.2"
        ): version_payload_or_error,
    }

    _mock_remote_responses(monkeypatch, endpoint_responses)

    with pytest.raises(ValueError, match=expected_pattern):
        validate_compatibility._matched_pair_for_package(
            validate_compatibility._LEGACY_TEST_HARNESS_PACKAGE,
            {"2026.1.0": [{"filename": "ha.whl"}]},
        )


@pytest.mark.parametrize(
    ("ha_ver", "expected_package"),
    [
        ("2024.12.0", "pytest-homeassistant-custom-component"),
        ("2026.10.4", "pytest-homeassistant-custom-component"),
        ("2026.11.0", "pytest-homeassistant"),
    ],
)
def test_test_harness_package_for_ha(ha_ver: str, expected_package: str) -> None:
    """Select the harness family from the Home Assistant release line."""
    assert validate_compatibility._test_harness_package_for_ha(ha_ver) == expected_package


def test_main_resolves_harness_dependency_for_ha(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Expose the harness install name for compatibility CI matrix rows."""
    monkeypatch.setattr(
        validate_compatibility.sys,
        "argv",
        ["validate_compatibility.py", "--resolve-harness-dependency", "2026.11.0"],
    )

    validate_compatibility.main()

    assert capsys.readouterr().out == "pytest-homeassistant\n"


def test_latest_matched_pair_prefers_new_harness_for_newer_ha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prefer the harness that matches the newest published Home Assistant release."""
    fetched_urls: list[str] = []
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_HA_JSON_URL: _json_text(
                _make_releases_payload("2026.10.4", "2026.11.0b5")
            ),
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _legacy_harness_payload(),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_JSON_URL: _json_text(
                {
                    "info": {"version": "0.1.3"},
                    "releases": {
                        "0.1.3": [{"filename": "legacy-unrelated.whl"}],
                        "2026.11.0b5": [{"filename": "new.whl"}],
                    },
                }
            ),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_VERSION_JSON_URL_TEMPLATE.format(
                version="2026.11.0b5"
            ): _json_text(
                {
                    "info": {
                        "version": "2026.11.0b5",
                        "requires_dist": ["homeassistant==2026.11.0b5"],
                    }
                }
            ),
        },
        fetched_urls,
    )

    assert validate_compatibility._get_latest_matched_pair() == {
        "ha_ver": "2026.11.0b5",
        "harness_package": "pytest-homeassistant",
        "harness_ver": "2026.11.0b5",
        "absolute_latest_ha_ver": "2026.11.0b5",
    }
    assert (
        validate_compatibility._PYPI_NEW_TEST_HARNESS_VERSION_JSON_URL_TEMPLATE.format(
            version="2026.11.0b5"
        )
        in fetched_urls
    )


def test_latest_matched_pair_falls_back_to_legacy_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall back to the legacy harness when new harness metadata is unavailable."""
    _mock_legacy_only_responses(monkeypatch)

    assert validate_compatibility._get_latest_matched_pair() == {
        "ha_ver": "2026.10.4",
        "harness_package": "pytest-homeassistant-custom-component",
        "harness_ver": "0.13.375",
        "absolute_latest_ha_ver": "2026.10.4",
    }


def test_latest_matched_pair_fails_when_expected_harness_lookup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fail instead of downgrading when the newest HA harness lookup fails."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_HA_JSON_URL: _json_text(
                _make_releases_payload("2026.10.4", "2026.11.0b5")
            ),
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _legacy_harness_payload(),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_JSON_URL: OSError(
                "new harness index unavailable"
            ),
        },
    )

    with pytest.raises(
        ValueError,
        match=r"Failed to resolve latest matched.*without downgrading 2026\.11\.0b5",
    ):
        validate_compatibility._get_latest_matched_pair()


def test_latest_matched_pair_treats_missing_new_harness_releases_as_lag(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fall back for publication lag, but not for a failed newest-harness lookup."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_HA_JSON_URL: _json_text(
                _make_releases_payload("2026.10.4", "2026.11.0b5")
            ),
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _legacy_harness_payload(),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_JSON_URL: _json_text(
                {"info": {"version": "0.1.3"}, "releases": {}}
            ),
        },
    )

    assert validate_compatibility._get_latest_matched_pair() == {
        "ha_ver": "2026.10.4",
        "harness_package": "pytest-homeassistant-custom-component",
        "harness_ver": "0.13.375",
        "absolute_latest_ha_ver": "2026.11.0b5",
    }
    assert "STEP_WARNING" not in capsys.readouterr().err


def test_latest_matched_pair_falls_back_when_final_release_has_no_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fall back to the newest beta harness while the final HA release is unmatched."""
    _mock_remote_responses(
        monkeypatch,
        {
            validate_compatibility._PYPI_HA_JSON_URL: _json_text(
                _make_releases_payload("2026.10.4", "2026.11.0b6", "2026.11.0")
            ),
            validate_compatibility._PYPI_TEST_HARNESS_JSON_URL: _legacy_harness_payload(),
            validate_compatibility._PYPI_NEW_TEST_HARNESS_JSON_URL: _json_text(
                {
                    "info": {
                        "version": "2026.11.0b6",
                        "requires_dist": ["homeassistant==2026.11.0b6"],
                    },
                    "releases": {"2026.11.0b6": [{"filename": "beta-harness.whl"}]},
                }
            ),
        },
    )

    assert validate_compatibility._get_latest_matched_pair() == {
        "ha_ver": "2026.11.0b6",
        "harness_package": validate_compatibility._NEW_TEST_HARNESS_PACKAGE,
        "harness_ver": "2026.11.0b6",
        "absolute_latest_ha_ver": "2026.11.0",
    }


def test_resolve_latest_json_remains_machine_readable_during_fallback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Keep --resolve-latest-json stdout parseable when one harness fails."""
    _mock_legacy_only_responses(monkeypatch)
    monkeypatch.setattr(
        validate_compatibility.sys,
        "argv",
        ["validate_compatibility.py", "--resolve-latest-json"],
    )

    validate_compatibility.main()

    captured = capsys.readouterr()
    assert orjson.loads(captured.out) == {
        "ha_ver": "2026.10.4",
        "harness_package": "pytest-homeassistant-custom-component",
        "harness_ver": "0.13.375",
        "absolute_latest_ha_ver": "2026.10.4",
    }
    assert "STEP_WARNING" in captured.err


def test_required_test_dependency_metadata_lists_both_harnesses() -> None:
    """Expose both supported harness families to compatibility CI."""
    metadata = validate_compatibility._required_test_dependency_metadata()

    assert set(metadata["test_harness_packages"]) == {
        "pytest-homeassistant-custom-component",
        "pytest-homeassistant",
    }
    assert "pytest-homeassistant-custom-component" not in metadata["required_packages"]
    assert "pytest-homeassistant" not in metadata["required_packages"]
    assert "h2" in metadata["required_packages"]
    assert "h2" not in metadata["homeassistant_constraint_packages"]
    assert "httpx" in metadata["homeassistant_constraint_packages"]


@pytest.mark.parametrize(
    ("checker_func", "tool_cmd", "step_label"),
    [
        (
            validate_compatibility._run_ty,
            ["ty", "check", "--python"],
            "uv run ty check (Home Assistant 2026.10.0b4)",
        ),
        (
            validate_compatibility._run_pyright,
            ["pyright", "--pythonpath"],
            "uv run pyright (Home Assistant 2026.10.0b4)",
        ),
    ],
)
def test_static_type_checkers_invoke_with_correct_args_and_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    checker_func: Callable[[Path, str], None],
    tool_cmd: list[str],
    step_label: str,
) -> None:
    """Static type checkers should be invoked with locked dependencies and python path."""
    python_file = _setup_repo_venv_python(tmp_path, monkeypatch)
    recorded_commands: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded_commands.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(
        validate_compatibility,
        "resolve_global_uv_path",
        lambda: "/custom/bin/uv",
    )
    monkeypatch.setattr(validate_compatibility.subprocess, "run", fake_run)
    checker_func(python_file, "2026.10.0b4")

    assert len(recorded_commands) == 1
    cmd, kwargs = recorded_commands[0]
    assert cmd == [
        "/custom/bin/uv",
        "run",
        "--locked",
        "--no-sync",
        *tool_cmd,
        str(python_file),
    ]
    assert kwargs.get("check") is True
    assert kwargs.get("cwd") == str(tmp_path)
    assert kwargs.get("timeout") == validate_compatibility._STATIC_ANALYSIS_TIMEOUT_SECONDS
    env = kwargs.get("env")
    assert isinstance(env, dict)
    assert env.get("UV_MANAGED_PYTHON") == "1"

    out = capsys.readouterr().out
    assert f"STEP_START: {step_label}" in out
    assert f"STEP_OK: {step_label}" in out


@pytest.mark.parametrize(
    "checker_func",
    [
        validate_compatibility._run_ty,
        validate_compatibility._run_pyright,
    ],
)
def test_static_type_checkers_reject_invalid_python_binary(
    checker_func: Callable[[Path, str], None],
) -> None:
    """Reject invalid or missing Python binaries before invoking static type checkers."""
    with pytest.raises(ValueError, match="must be a python executable"):
        checker_func(Path(".venv/bin/nonexistent"), "2026.10.0b4")

    with pytest.raises(ValueError, match="Python executable not found"):
        checker_func(Path("nonexistent/bin/python"), "2026.10.0b4")


@pytest.mark.parametrize(
    ("is_latest", "ha_ver_to_install", "harness_ver_to_install", "expect_type_checks"),
    [
        (True, "2026.10.0b4", "0.13.370", True),
        (False, "2024.12.0", "0.13.190", False),
    ],
)
def test_verify_and_run_tests_conditional_static_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_latest: bool,
    ha_ver_to_install: str,
    harness_ver_to_install: str,
    expect_type_checks: bool,
) -> None:
    """Verify latest compatibility runs ty/pyright while historical compatibility skips them."""
    pytest_bin = tmp_path / "bin" / "pytest"
    pytest_bin.parent.mkdir(parents=True)
    pytest_bin.write_text("", encoding="utf-8")
    python_bin = tmp_path / "bin" / "python"
    python_bin.write_text("", encoding="utf-8")

    monkeypatch.setattr(
        validate_compatibility,
        "_get_installed_ha_version",
        lambda _p: ha_ver_to_install,
    )
    monkeypatch.setattr(
        validate_compatibility,
        "_verify_harness_pair",
        lambda *_args: True,
    )

    ty_calls: list[tuple[Path, str]] = []
    pyright_calls: list[tuple[Path, str]] = []
    pytest_calls: list[tuple[Path, str, Sequence[str]]] = []

    monkeypatch.setattr(
        validate_compatibility,
        "_run_ty",
        lambda p, ha: ty_calls.append((p, ha)),
    )
    monkeypatch.setattr(
        validate_compatibility,
        "_run_pyright",
        lambda p, ha: pyright_calls.append((p, ha)),
    )
    monkeypatch.setattr(
        validate_compatibility,
        "_run_pytest",
        lambda p, ha, args: pytest_calls.append((p, ha, args)),
    )

    success, ha_ver = validate_compatibility._verify_and_run_tests(
        python_bin=python_bin,
        pytest_bin=pytest_bin,
        ha_ver_to_install=ha_ver_to_install,
        harness_ver_to_install=harness_ver_to_install,
        is_latest=is_latest,
    )

    assert success
    assert ha_ver == ha_ver_to_install
    if expect_type_checks:
        assert ty_calls == [(python_bin, ha_ver_to_install)]
        assert pyright_calls == [(python_bin, ha_ver_to_install)]
    else:
        assert not ty_calls
        assert not pyright_calls
    assert pytest_calls == [
        (python_bin, ha_ver_to_install, validate_compatibility._COMPATIBILITY_PYTEST_ARGS)
    ]


@pytest.mark.parametrize(
    ("fail_check", "expected_exit_code"),
    [
        (False, None),
        (True, 1),
    ],
)
def test_main_verify_pair_python_with_check_types(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_check: bool,
    expected_exit_code: int | None,
) -> None:
    """Verify pair python with --check-types invokes checkers and handles failure."""
    python_file = _setup_repo_venv_python(tmp_path, monkeypatch)
    monkeypatch.setattr(validate_compatibility, "resolve_global_uv_path", lambda: "/mock/bin/uv")
    monkeypatch.setattr(validate_compatibility, "_verify_harness_pair", lambda *_args: True)

    ty_calls: list[tuple[Path, str]] = []
    pyright_calls: list[tuple[Path, str]] = []

    def mock_pyright(p: Path, ha: str) -> None:
        if fail_check:
            raise subprocess.CalledProcessError(1, ["pyright"])
        pyright_calls.append((p, ha))

    monkeypatch.setattr(validate_compatibility, "_run_ty", lambda p, ha: ty_calls.append((p, ha)))
    monkeypatch.setattr(validate_compatibility, "_run_pyright", mock_pyright)
    cli_args = [
        "validate_compatibility.py",
        "--verify-pair-python",
        str(python_file),
        "--expected-ha",
        "2026.10.0b4",
        "--expected-harness",
        "0.13.370",
        "--check-types",
    ]
    monkeypatch.setattr(validate_compatibility.sys, "argv", cli_args)

    if expected_exit_code is None:
        validate_compatibility.main()
        assert ty_calls == [(python_file, "2026.10.0b4")]
        assert pyright_calls == [(python_file, "2026.10.0b4")]
    else:
        with pytest.raises(SystemExit) as raised:
            validate_compatibility.main()
        assert raised.value.code == expected_exit_code
        assert "VALIDATION_ERROR: static type check failed:" in capsys.readouterr().out


@pytest.mark.parametrize(
    "run_action",
    [
        "venv",
        "pip",
        "pytest",
        "pyright",
        "ty",
    ],
)
def test_subprocess_commands_set_uv_managed_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_action: str,
) -> None:
    """Ensure subprocess invocations set UV_MANAGED_PYTHON in their environment."""
    captured_env: dict[str, str] = {}

    def fake_subprocess_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal captured_env
        if "env" in kwargs and isinstance(kwargs["env"], dict):
            captured_env = kwargs["env"]
        if run_action == "venv":
            venv_path = Path(cmd[6])
            (venv_path / "bin").mkdir(parents=True)
            (venv_path / "bin" / "python").write_text("", encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(validate_compatibility, "resolve_global_uv_path", lambda: "uv")
    monkeypatch.setattr(validate_compatibility.subprocess, "run", fake_subprocess_run)
    if run_action == "venv":
        assert validate_compatibility._ensure_venv(tmp_path / "test_venv", "3.14")
    elif run_action == "pip":
        validate_compatibility._run_uv_pip_install(
            Path("python"),
            ["homeassistant==2026.6.0"],
            "homeassistant",
        )
    elif run_action == "pytest":
        validate_compatibility._run_pytest(Path("python"), "2026.6.0", ["-k", "test"])
    elif run_action in {"pyright", "ty"}:
        python_file = _setup_repo_venv_python(tmp_path, monkeypatch)
        runner = (
            validate_compatibility._run_pyright
            if run_action == "pyright"
            else validate_compatibility._run_ty
        )
        runner(python_file, "2026.6.0")

    assert captured_env.get("UV_MANAGED_PYTHON") == "1"


def test_main_sets_uv_managed_python(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Ensure main entry point sets UV_MANAGED_PYTHON in os.environ."""
    monkeypatch.setattr(
        validate_compatibility.os, "environ", dict(validate_compatibility.os.environ)
    )
    validate_compatibility.os.environ.pop("UV_MANAGED_PYTHON", None)
    monkeypatch.setattr(
        validate_compatibility.sys,
        "argv",
        ["validate_compatibility.py", "--validate-matrix"],
    )
    validate_compatibility.main()
    capsys.readouterr()

    assert validate_compatibility.os.environ.get("UV_MANAGED_PYTHON") == "1"
