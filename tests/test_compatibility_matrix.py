"""Tests for the declared Home Assistant compatibility range."""

from pathlib import Path
from unittest.mock import MagicMock

import orjson
import pytest
from packaging.version import Version

from tools import validate_compatibility
from tools.validate_compatibility import (
    _parse_requirements_dependency_version,
    _test_matrix,
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
