"""Multi-version Home Assistant compatibility test suite.

This script manages virtual environments for testing the integration against
multiple Home Assistant core versions.

SECURITY NOTE:
Commands are intentionally hardcoded as explicit list literals in subprocess.run
calls where possible to satisfy static analysis security audits. This prevents
false positives related to command injection.
"""

import argparse
import contextlib
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from string import ascii_letters, digits
from time import monotonic
from typing import TypedDict

import orjson
from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

try:
    from .validate import (
        exact_homeassistant_requirement,
        normalize_package_name,
        resolve_global_uv_path,
        versions_differ,
    )
except ImportError:
    from validate import (
        exact_homeassistant_requirement,
        normalize_package_name,
        resolve_global_uv_path,
        versions_differ,
    )


_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_VENVS_ROOT = os.path.join(_REPO_ROOT, ".venvs")

_VENV_DEPENDENCY_MARKER = ".blueprints_updater_test_dependencies.json"

_TEST_HARNESS_PACKAGE = "pytest-homeassistant-custom-component"
_HA_CONSTRAINED_TEST_DEPS = ()  # Reserved for future use.
_REQUIRED_TEST_DEPS = (
    *_HA_CONSTRAINED_TEST_DEPS,
    "httpx[http2]",
    "pytest",
    "pytest-asyncio",
    "pytest-cov",
    _TEST_HARNESS_PACKAGE,
    "pytest-timeout",
    "pytest-xdist",
)

_COMPATIBILITY_PYTEST_ARGS = ["--no-cov"]
_COMPATIBILITY_METADATA_PROBE_TIMEOUT_SECONDS = 60
_VENV_CREATE_TIMEOUT_SECONDS = 120
_INSTALL_TIMEOUT_SECONDS = 300
_CLEANUP_TIMEOUT_SECONDS = 30
_CLEANUP_ISSUE_LIMIT = 10
_COMPATIBILITY_PYTEST_TIMEOUT_SECONDS = 300

_ALNUM_CHARS = ascii_letters + digits
_ALLOWED_VERSION_CHARS = f"{_ALNUM_CHARS}."
_ALLOWED_PACKAGE_CHARS = f"{_ALNUM_CHARS}._-"

_VERSION_PATTERN = re.compile(rf"^[{_ALNUM_CHARS}]+(?:\.[{_ALNUM_CHARS}]+)*$")
_PACKAGE_NAME_PATTERN = re.compile(
    rf"^(?:[{_ALNUM_CHARS}]|[{_ALNUM_CHARS}][{_ALNUM_CHARS}._-]*[{_ALNUM_CHARS}])$"
)

_MATRIX_FILE = os.path.join(_REPO_ROOT, "tools", "compatibility_matrix.json")

_PYPI_HA_JSON_URL = "https://pypi.org/pypi/homeassistant/json"
_PYPI_TEST_HARNESS_JSON_URL = "https://pypi.org/pypi/pytest-homeassistant-custom-component/json"

_HA_CONSTRAINTS_GITHUB_URL_TEMPLATE = "https://raw.githubusercontent.com/home-assistant/core/{ha_version}/homeassistant/package_constraints.txt"

_HA_CONSTRAINTS_CDN_URL_TEMPLATE = "https://cdn.jsdelivr.net/gh/home-assistant/core@{ha_version}/homeassistant/package_constraints.txt"


class CompatibilityConfig(TypedDict):
    """Validated Home Assistant compatibility test matrix entry."""

    ha_ver: str
    harness_ver: str
    python_ver: str


class LatestMatchedPair(TypedDict):
    """Newest harness-backed Home Assistant release plus the absolute HA edge."""

    ha_ver: str
    harness_ver: str
    absolute_latest_ha_ver: str


class RequiredTestDependencyMetadata(TypedDict):
    """Validated required test dependency metadata consumed by compatibility CI."""

    required_packages: Sequence[str]
    homeassistant_constraint_packages: Sequence[str]
    test_harness_package: str


def _required_test_dependency_metadata() -> RequiredTestDependencyMetadata:
    """Return dependency package metadata consumed by compatibility CI."""
    return {
        "required_packages": _REQUIRED_TEST_DEPS,
        "homeassistant_constraint_packages": _HA_CONSTRAINED_TEST_DEPS,
        "test_harness_package": _TEST_HARNESS_PACKAGE,
    }


def _expected_required_test_dep_versions(
    test_dependency_versions: dict[str, str],
) -> dict[str, str]:
    """Return expected required test dependency versions.

    Keys are canonical normalized package names.
    """
    return {normalize_package_name(pkg): ver for pkg, ver in test_dependency_versions.items()}


def _required_test_deps(test_dependency_versions: dict[str, str]) -> list[str]:
    """Return install specs for required test dependencies."""
    deps: list[str] = []
    norm_test_deps = {
        normalize_package_name(pkg): ver for pkg, ver in test_dependency_versions.items()
    }
    seen: set[str] = set()
    for package in _REQUIRED_TEST_DEPS:
        base_name = normalize_package_name(package)
        seen.add(base_name)
        if base_name in norm_test_deps:
            deps.append(f"{package}=={norm_test_deps[base_name]}")
        else:
            deps.append(package)
    deps.extend(
        f"{package}=={version}"
        for package, version in sorted(test_dependency_versions.items())
        if normalize_package_name(package) not in seen
    )
    return deps


def _dependency_pin_marker_payload(
    test_dependency_versions: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Return the canonical venv marker payload for resolved test dependency versions."""
    return {"test_dependency_versions": dict(sorted(test_dependency_versions.items()))}


def _venv_dependency_marker_matches(
    venv_path: Path,
    test_dependency_versions: dict[str, str],
) -> bool:
    """Return whether a compatibility venv was installed with the same resolved versions."""
    marker_path = venv_path / _VENV_DEPENDENCY_MARKER
    try:
        parsed = orjson.loads(marker_path.read_bytes())
    except (OSError, orjson.JSONDecodeError):
        return False
    return parsed == _dependency_pin_marker_payload(test_dependency_versions)


def _dependency_marker_requires_reinstall(
    created_venv: bool,
    venv_path: Path,
    test_dependency_versions: dict[str, str],
) -> bool:
    """Return whether the resolved version marker requires a full dependency install."""
    if created_venv or _venv_dependency_marker_matches(
        venv_path,
        test_dependency_versions,
    ):
        return False
    print(
        "STEP_INFO: dependency marker changed; reinstalling test dependencies",
        flush=True,
    )
    return True


def _write_venv_dependency_marker(
    venv_path: Path,
    test_dependency_versions: dict[str, str],
) -> None:
    """Persist the resolved dependency version marker for future venv reuse checks."""
    marker_path = venv_path / _VENV_DEPENDENCY_MARKER
    marker_path.write_bytes(
        orjson.dumps(
            _dependency_pin_marker_payload(test_dependency_versions),
            option=orjson.OPT_SORT_KEYS,
        )
    )


def _extract_requirement_base_name(spec: str) -> str:
    """Extract base package name from a requirement specifier."""
    try:
        return Requirement(spec).name
    except InvalidRequirement:
        return spec.split("[", 1)[0].split(";", 1)[0].split("=", 1)[0].strip()


def _parse_requirements_dependency_version(
    requirements_text: str,
    package_name: str,
    source_name: str = "package_constraints.txt",
) -> str:
    """Return the exact package version from a requirements file."""
    base_name = _extract_requirement_base_name(package_name)
    package = _validate_package_name("package_name", base_name)
    for raw_line in requirements_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement_name, separator, version_text = line.partition("==")
        if separator != "==":
            continue
        try:
            req_base = _extract_requirement_base_name(requirement_name.strip())
            requirement_package = _validate_package_name(
                "requirements_package_name",
                req_base,
            )
        except ValueError:
            continue
        if requirement_package != package:
            continue
        version_tokens = version_text.split(";", 1)[0].strip().split()
        if not version_tokens:
            raise ValueError(
                f"Invalid {package!r} requirement in Home Assistant {source_name}; "
                "expected a version after '=='."
            )
        version = version_tokens[0]
        return _validate_version_label(f"{package}_version", version)
    raise ValueError(f"Could not find {package!r} in Home Assistant {source_name}")


def _fetch_remote_text(url: str) -> str:
    """Fetch and decode remote text from a URL.

    Raises urllib.error.URLError, OSError, or UnicodeDecodeError on failure.
    """
    with urllib.request.urlopen(url, timeout=20) as response:
        return response.read().decode("utf-8")


def _get_required_package_version(ha_ver: str, package_name: str) -> str:
    """Fetch the version for a package required by a Home Assistant tag constraints.

    Reads exclusively from Home Assistant package_constraints.txt.
    """
    version = _validate_version_label("ha_ver", ha_ver)
    package = _validate_package_name("package_name", package_name)

    cdn_url = _HA_CONSTRAINTS_CDN_URL_TEMPLATE.format(ha_version=version)
    github_url = _HA_CONSTRAINTS_GITHUB_URL_TEMPLATE.format(ha_version=version)
    last_err: Exception | None = None

    for url in (cdn_url, github_url):
        try:
            requirements_text = _fetch_remote_text(url)
            return _parse_requirements_dependency_version(requirements_text, package)
        except (urllib.error.URLError, OSError, ValueError) as err:
            last_err = err

    raise ValueError(
        f"Failed to fetch Home Assistant package constraints for {version} "
        f"and package {package}: {last_err}"
    ) from last_err


def _resolve_test_dependency_versions(
    ha_ver: str,
    harness_ver: str,
) -> dict[str, str]:
    """Return Home Assistant test dependency versions."""
    del ha_ver
    return {
        _TEST_HARNESS_PACKAGE: _validate_version_label(
            "harness_ver",
            harness_ver,
        )
    }


def _test_dep_packages(test_dependency_versions: dict[str, str]) -> tuple[str, ...]:
    """Return base and resolved test dependency package names."""
    return tuple(dict.fromkeys((*_REQUIRED_TEST_DEPS, *test_dependency_versions)))


def _venv_required_test_dep_versions(
    python_bin: Path,
    test_dependency_versions: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return required test dependency versions installed in a compatibility venv."""
    packages = _test_dep_packages(test_dependency_versions or {})
    code = (
        "import contextlib, importlib.metadata as md, json\n"
        f"packages = {packages!r}\n"
        "versions = {}\n"
        "for package in packages:\n"
        "    with contextlib.suppress(md.PackageNotFoundError):\n"
        "        if '[' in package and package.endswith(']'):\n"
        "            base_name, extras_str = package[:-1].split('[', 1)\n"
        "            extras = [e.strip() for e in extras_str.split(',')]\n"
        "            base_ver = md.version(base_name)\n"
        "            satisfied = True\n"
        "            reqs = md.requires(base_name) or []\n"
        "            for extra in extras:\n"
        "                for req in reqs:\n"
        "                    if ';' in req:\n"
        "                        dep, marker = req.split(';', 1)\n"
        "                        marker_norm = marker.replace(' ', '').replace('\"', \"'\")\n"
        "                        if f\"extra=='{extra}'\" in marker_norm:\n"
        "                            dep_name = ''\n"
        "                            for c in dep.strip():\n"
        "                                if not (c.isalnum() or c in '.-_'):\n"
        "                                    break\n"
        "                                dep_name += c\n"
        "                            try:\n"
        "                                md.version(dep_name)\n"
        "                            except md.PackageNotFoundError:\n"
        "                                satisfied = False\n"
        "                                break\n"
        "                if not satisfied:\n"
        "                    break\n"
        "            if satisfied:\n"
        "                versions[package] = base_ver\n"
        "        else:\n"
        "            versions[package] = md.version(package)\n"
        "print(json.dumps(versions, sort_keys=True))\n"
    )
    try:
        result = subprocess.run(
            [
                "uv",
                "--no-config",
                "run",
                "--no-project",
                "--python",
                str(python_bin),
                "python",
                "-c",
                code,
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=_COMPATIBILITY_METADATA_PROBE_TIMEOUT_SECONDS,
        )
        parsed = orjson.loads(result.stdout)
        if not isinstance(parsed, dict):
            return {}
        return {
            normalize_package_name(str(package)): str(version)
            for package, version in parsed.items()
        }
    except (
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        orjson.JSONDecodeError,
        OSError,
    ) as err:
        print(f"STEP_INFO: Failed to probe venv metadata for {python_bin}: {err!r}", flush=True)
        if isinstance(err, subprocess.CalledProcessError):
            if err.stdout:
                print(f"STEP_INFO: probe stdout: {err.stdout.strip()}", flush=True)
            if err.stderr:
                print(f"STEP_INFO: probe stderr: {err.stderr.strip()}", flush=True)
        return {}


def _missing_required_test_deps(
    python_bin: Path,
    test_dependency_versions: dict[str, str],
) -> tuple[str, ...]:
    """Return required test dependency names that are absent from a compatibility venv.

    Note: Checks unpinned required test dependencies. Required dependencies that are
    pinned in test_dependency_versions are validated for version drift by _stale_test_deps.
    """
    installed = _venv_required_test_dep_versions(python_bin, test_dependency_versions)
    norm_test_deps = {normalize_package_name(p): v for p, v in test_dependency_versions.items()}
    return tuple(
        package
        for package in _REQUIRED_TEST_DEPS
        if normalize_package_name(package) not in norm_test_deps
        and normalize_package_name(package) not in installed
    )


def _stale_test_deps(
    python_bin: Path,
    test_dependency_versions: dict[str, str],
) -> tuple[str, ...]:
    """Return test dependency install specs that drifted from required versions."""
    expected = _expected_required_test_dep_versions(test_dependency_versions)
    if not expected:
        return ()
    installed = _venv_required_test_dep_versions(python_bin, test_dependency_versions)

    stale = {
        package: (installed.get(package), expected_version)
        for package, expected_version in expected.items()
        if installed.get(package) != expected_version
    }
    if stale:
        details = ", ".join(
            f"{package} {old or 'missing'} -> {new}"
            for package, (old, new) in sorted(stale.items())
        )
        print(f"STEP_INFO: refreshing test dependencies: {details}", flush=True)
    return tuple(f"{package}=={version}" for package, (_old, version) in sorted(stale.items()))


def _load_matrix_data() -> list[dict[str, object]]:
    """Load compatibility matrix from the repository tools directory."""
    with open(_MATRIX_FILE, encoding="utf-8") as f:
        loaded = orjson.loads(f.read())
    if not isinstance(loaded, list):
        raise ValueError("Compatibility matrix must be a list")
    matrix: list[dict[str, object]] = []
    for index, entry in enumerate(loaded, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Compatibility matrix entry {index} must be an object")
        matrix.append({str(key): value for key, value in entry.items()})
    return matrix


def _matrix_entry_text(entry: dict[str, object], key: str) -> str:
    """Return a required text field from a matrix entry."""
    return _require_str_field(key, entry.get(key))


def _test_matrix() -> list[CompatibilityConfig]:
    """Return validated compatibility matrix entries."""
    data = _load_matrix_data()
    entries = []
    for idx, entry in enumerate(data, start=1):
        try:
            ha_ver = _validate_version_label(
                "ha_version",
                _matrix_entry_text(entry, "ha_version"),
            )
            harness_ver = _validate_version_label(
                "harness_version",
                _matrix_entry_text(entry, "harness_version"),
            )
            py_ver = _validate_version_label(
                "python_version",
                _matrix_entry_text(entry, "python_version"),
            )
            if (ha_ver == "latest") != (harness_ver == "latest"):
                raise ValueError(
                    "ha_version and harness_version must both be 'latest' or both be fixed"
                )
        except ValueError as err:
            raise ValueError(f"Matrix row {idx}: {err}") from err
        entries.append(
            CompatibilityConfig(
                ha_ver=ha_ver,
                harness_ver=harness_ver,
                python_ver=py_ver,
            )
        )
    return entries


def _require_str_field(label_name: str, value: object) -> str:
    """Return value typed as str, or raise ValueError if it is not a string."""
    if not isinstance(value, str):
        raise ValueError(f"Invalid {label_name} value {value!r}; expected a string.")
    return value


def _validate_version_label(label_name: str, label_value: str) -> str:
    """Validate and sanitize a matrix version label to prevent path injection.

    Uses a strict regex check to enforce structural validity.

    SECURITY NOTE:
    - The regex and `_ALLOWED_VERSION_CHARS` share underlying constant components
      to ensure synchronization while still allowing the regex to enforce strict
      structural validity (e.g., prohibiting consecutive dots).
    - DO NOT simplify the character reconstruction loop (e.g., via comprehension).
      Mapping via integer index to the static `_ALLOWED_VERSION_CHARS` is required
      to completely sever the CodeQL data-flow taint chain.
    - `os.path.basename` is retained to satisfy CodeQL's hardcoded AST sanitizer rules.
    - The loop fails fast on unknown characters, acting as an extra safety net.
    """
    label_value = _require_str_field(label_name, label_value)

    if not label_value:
        raise ValueError(
            f"Invalid {label_name} value {label_value!r}; expected a non-empty version label."
        )

    if not _VERSION_PATTERN.fullmatch(label_value):
        raise ValueError(
            f"Invalid {label_name} value {label_value!r}; must be alphanumeric blocks "
            "separated by a single dot, and cannot contain consecutive, leading, or trailing dots."
        )

    safe_chars: list[str] = []
    for char in label_value:
        idx = _ALLOWED_VERSION_CHARS.find(char)
        if idx == -1:
            raise ValueError(
                f"Invalid {label_name} value {label_value!r}; character {char!r} is not allowed."
            )
        safe_chars.append(_ALLOWED_VERSION_CHARS[idx])

    safe_val = "".join(safe_chars)
    return os.path.basename(safe_val)


def _validate_package_name(label_name: str, package_name: object) -> str:
    """Validate and sanitize a matrix package name to prevent command injection.

    SECURITY NOTE:
    - DO NOT simplify the character reconstruction loop (e.g., via comprehension).
      Mapping via integer index to the static `_ALLOWED_PACKAGE_CHARS` is required
      to completely sever the CodeQL data-flow taint chain.
    - `os.path.basename` is retained to satisfy CodeQL's hardcoded AST sanitizer rules.
    - The loop fails fast on unknown characters, acting as an extra safety net.
    """
    package_name = _require_str_field(label_name, package_name)

    if not _PACKAGE_NAME_PATTERN.fullmatch(package_name):
        raise ValueError(
            f"Invalid {label_name} value {package_name!r}; must be a Python package name."
        )

    safe_chars: list[str] = []
    for char in package_name:
        idx = _ALLOWED_PACKAGE_CHARS.find(char)
        if idx == -1:
            raise ValueError(
                f"Invalid {label_name} value {package_name!r}; character {char!r} is not allowed."
            )
        safe_chars.append(_ALLOWED_PACKAGE_CHARS[idx])

    safe_val = "".join(safe_chars)
    return normalize_package_name(os.path.basename(safe_val))


def _ensure_within_root(root_path: str, candidate_path: str) -> str:
    """Return safe absolute path only if candidate resides within root_path.

    SECURITY: Resolves the root via os.path.realpath (symlink-safe), joins
       with the cleaned path, normalizes via os.path.normpath, and
       verifies containment via startswith.

    Returns the safe absolute path or raises ValueError.
    """
    root = os.path.realpath(root_path)
    fullpath = os.path.realpath(os.path.normpath(os.path.join(root, candidate_path)))

    if fullpath != root and not fullpath.startswith(root + os.sep):
        raise ValueError(f"Resolved path {fullpath!r} escapes allowed root {root!r}.")
    return fullpath


def _format_cmd_str(cmd: object) -> str:
    """Return a human-readable string for a subprocess command."""
    if isinstance(cmd, (list, tuple)):
        return " ".join(str(arg) for arg in cmd)
    return str(cmd)


def _newest_published_version(payload: object, package_name: str) -> str:
    """Return the newest valid version with at least one published artifact."""
    if not isinstance(payload, dict) or not isinstance(
        (releases := payload.get("releases")),
        dict,
    ):
        raise ValueError(f"PyPI metadata for {package_name} has no releases object")
    published_versions: list[tuple[Version, str]] = []
    for raw_version, artifacts in releases.items():
        if not isinstance(raw_version, str) or not isinstance(artifacts, list) or not artifacts:
            continue
        try:
            published_versions.append((Version(raw_version), raw_version))
        except InvalidVersion:
            continue
    if not published_versions:
        raise ValueError(f"PyPI returned no published {package_name} releases")
    return _validate_version_label(
        f"{normalize_package_name(package_name)}_version",
        max(published_versions)[1],
    )


def _get_latest_matched_pair() -> LatestMatchedPair:
    """Resolve the newest harness-backed HA release and the absolute HA edge."""
    try:
        ha_payload = orjson.loads(_fetch_remote_text(_PYPI_HA_JSON_URL))
        harness_payload = orjson.loads(_fetch_remote_text(_PYPI_TEST_HARNESS_JSON_URL))
        if not isinstance(ha_payload, dict):
            raise ValueError("Home Assistant PyPI metadata is not a JSON object")
        if not isinstance(harness_payload, dict):
            raise ValueError("Test harness PyPI metadata is not a JSON object")
        absolute_latest = _newest_published_version(ha_payload, "homeassistant")
        harness_version = _newest_published_version(
            harness_payload,
            _TEST_HARNESS_PACKAGE,
        )
        if not isinstance((harness_info := harness_payload.get("info")), dict):
            raise ValueError("Test harness PyPI metadata has no info object")
        matched_ha = _validate_version_label(
            "harness_homeassistant_version",
            exact_homeassistant_requirement(
                harness_info.get("requires_dist"),
                f"{_TEST_HARNESS_PACKAGE} {harness_version}",
            ),
        )
        if not isinstance((releases := ha_payload.get("releases")), dict):
            raise ValueError("Home Assistant PyPI metadata has no releases object")
        matched_artifacts = next(
            (
                rel_artifacts
                for rel_ver, rel_artifacts in releases.items()
                if isinstance(rel_ver, str)
                and isinstance(rel_artifacts, list)
                and rel_artifacts
                and not versions_differ(rel_ver, matched_ha)
            ),
            None,
        )
        if not matched_artifacts:
            raise ValueError(
                f"Test harness {harness_version} targets unpublished Home Assistant {matched_ha}"
            )
    except (
        urllib.error.URLError,
        OSError,
        orjson.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ) as err:
        raise ValueError(
            f"Failed to resolve latest matched harness-backed Home Assistant pair: {err}"
        ) from err
    return LatestMatchedPair(
        ha_ver=matched_ha,
        harness_ver=harness_version,
        absolute_latest_ha_ver=absolute_latest,
    )


def _get_venv_path(ha_ver: str, py_ver: str) -> str:
    """Construct the virtual environment path for a specific version."""
    ha = _validate_version_label("ha_ver", ha_ver)
    py = _validate_version_label("py_ver", py_ver)

    venv_name = os.path.basename(f"homeassistant_{ha}_python_{py}")

    if os.path.basename(venv_name) != venv_name:
        raise ValueError(f"Invalid venv name: {venv_name}")

    candidate = os.path.join(_VENVS_ROOT, venv_name)
    return _ensure_within_root(_VENVS_ROOT, candidate)


def _determine_dependency_actions(
    reinstall: bool,
    created_venv: bool,
    python_bin: Path,
    test_dependency_versions: dict[str, str],
) -> tuple[bool, tuple[str, ...]]:
    """Determine whether dependencies need a full install or just refreshes."""
    needs_install = reinstall or created_venv
    refresh_deps: tuple[str, ...] = ()
    if not needs_install:
        if missing_deps := _missing_required_test_deps(
            python_bin,
            test_dependency_versions,
        ):
            details = ", ".join(sorted(missing_deps))
            print(f"STEP_INFO: installing missing test dependencies: {details}", flush=True)
            needs_install = True
        else:
            refresh_deps = _stale_test_deps(
                python_bin,
                test_dependency_versions,
            )
    return needs_install, refresh_deps


def _remove_venv_dir(venv_path: Path) -> None:
    """Safely remove a virtual environment directory."""
    if not venv_path.exists():
        return
    try:
        shutil.rmtree(venv_path)
    except OSError as err:
        raise RuntimeError(
            f"Failed to remove virtual environment directory at {venv_path}: {err!r}"
        ) from err

    if venv_path.exists():
        raise RuntimeError(
            f"Virtual environment directory at {venv_path} still exists after removal."
        )


def _ensure_venv(venv_path: Path, py_ver: str) -> bool:
    """Ensure virtual environment exists, creating it if necessary.

    Returns:
        True if a new virtual environment was created, False otherwise.

    Raises:
        RuntimeError: If virtual environment creation succeeded but python binary is missing.
    """
    python_bin = venv_path / "bin" / "python"
    pytest_bin = venv_path / "bin" / "pytest"
    if venv_path.exists() and python_bin.exists() and pytest_bin.exists():
        return False
    if venv_path.exists():
        print(f"STEP_INFO: Re-creating incomplete virtual environment at {venv_path}", flush=True)
        _remove_venv_dir(venv_path)
    print(f"STEP_START: uv venv {venv_path} (Python {py_ver})", flush=True)
    subprocess.run(
        [
            "uv",
            "--no-config",
            "venv",
            "--no-project",
            "--python",
            py_ver,
            venv_path,
            "--quiet",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=_VENV_CREATE_TIMEOUT_SECONDS,
    )
    if not python_bin.exists():
        raise RuntimeError(
            f"Failed to create virtual environment: python binary missing at {python_bin}"
        )
    print(f"STEP_OK: uv venv {venv_path} (Python {py_ver})", flush=True)
    return True


def _reset_venv(venv_path: Path, py_ver: str) -> bool:
    """Remove and recreate a compatibility virtual environment."""
    if venv_path.exists():
        print(f"STEP_INFO: Resetting virtual environment at {venv_path}", flush=True)
        _remove_venv_dir(venv_path)
    return _ensure_venv(venv_path, py_ver)


def _run_uv_pip_install(
    python_bin: Path,
    package_args: Sequence[str],
    step_label: str,
) -> None:
    """Run uv pip install with prereleases enabled for HA dependency pins."""
    print(f"STEP_START: uv pip install {step_label}", flush=True)
    subprocess.run(
        [
            "uv",
            "--no-config",
            "pip",
            "install",
            "--upgrade",
            "--prerelease",
            "allow",
            "--python",
            python_bin,
            *package_args,
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
        timeout=_INSTALL_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: uv pip install {step_label}", flush=True)


def _install_compatibility_dependencies(
    python_bin: Path,
    ha_version: str,
    test_dependency_versions: dict[str, str],
) -> None:
    """Install Home Assistant and required test dependencies."""
    required_test_deps = _required_test_deps(test_dependency_versions)
    ha_spec = f"homeassistant=={ha_version}"
    _run_uv_pip_install(
        python_bin,
        [ha_spec, *required_test_deps],
        ha_spec,
    )


def _refresh_compatibility_dependencies(
    python_bin: Path,
    refresh_dependencies: tuple[str, ...],
) -> None:
    """Upgrade selected compatibility dependencies."""
    _run_uv_pip_install(
        python_bin,
        refresh_dependencies,
        " ".join(refresh_dependencies),
    )


def _cleanup_compatibility_bytecode(target_dir: Path) -> None:
    """Remove stale bytecode caches, reporting missing targets as nonfatal no-ops."""
    print("STEP_START: cleanup __pycache__", flush=True)
    if target_dir.is_symlink():
        message = f"refusing to clean symlinked target directory {target_dir}"
        print(f"STEP_FAILED: cleanup __pycache__: {message}", file=sys.stderr, flush=True)
        raise RuntimeError(message)
    if not target_dir.exists():
        print(
            f"STEP_WARNING: cleanup __pycache__: target directory does not exist: {target_dir}",
            file=sys.stderr,
            flush=True,
        )
        return
    if not target_dir.is_dir():
        message = f"cleanup target path is not a directory: {target_dir}"
        print(f"STEP_FAILED: cleanup __pycache__: {message}", file=sys.stderr, flush=True)
        raise RuntimeError(message)

    deadline = monotonic() + _CLEANUP_TIMEOUT_SECONDS
    issues: list[str] = []
    issue_count = 0

    def report_issue(message: str) -> None:
        nonlocal issue_count
        issue_count += 1
        if len(issues) < _CLEANUP_ISSUE_LIMIT:
            issues.append(message)
            print(
                f"STEP_WARNING: cleanup __pycache__: {message}",
                file=sys.stderr,
                flush=True,
            )

    def report_walk_error(err: OSError) -> None:
        report_issue(f"unable to scan {err.filename or target_dir}: {err}")

    try:
        for root, dirnames, _filenames in os.walk(
            target_dir,
            topdown=True,
            onerror=report_walk_error,
            followlinks=False,
        ):
            if monotonic() > deadline:
                report_issue(f"timed out after {_CLEANUP_TIMEOUT_SECONDS} seconds")
                break
            bytecode_names = [name for name in dirnames if name == "__pycache__"]
            dirnames[:] = [name for name in dirnames if name != "__pycache__"]
            timed_out = False
            for name in bytecode_names:
                if monotonic() > deadline:
                    timed_out = True
                    break
                bytecode_dir = Path(root, name)
                try:
                    shutil.rmtree(bytecode_dir)
                except OSError as err:
                    report_issue(f"unable to remove {bytecode_dir}: {err}")
            if timed_out:
                report_issue(f"timed out after {_CLEANUP_TIMEOUT_SECONDS} seconds")
                break
    except OSError as err:
        report_issue(f"unable to traverse {target_dir}: {err}")
    if issue_count:
        omitted_count = issue_count - len(issues)
        if omitted_count:
            issues.append(f"{omitted_count} additional issue(s) omitted")
        print("STEP_FAILED: cleanup __pycache__", file=sys.stderr, flush=True)
        raise RuntimeError("Compatibility bytecode cleanup failed: " + "; ".join(issues))
    print("STEP_OK: cleanup __pycache__", flush=True)


def _install_dependencies(
    venv_path: Path,
    python_bin: Path,
    ha_ver_to_install: str,
    needs_install: bool,
    refresh_deps: tuple[str, ...],
    test_dependency_versions: dict[str, str],
    *,
    py_ver: str,
    reset_before_install: bool = False,
) -> None:
    """Install or upgrade required test dependencies in the compatibility venv."""
    if needs_install:
        if reset_before_install:
            _reset_venv(venv_path, py_ver)
        _install_compatibility_dependencies(
            python_bin,
            ha_ver_to_install,
            test_dependency_versions,
        )
    elif refresh_deps:
        _refresh_compatibility_dependencies(
            python_bin,
            refresh_deps,
        )
    if needs_install or refresh_deps:
        _cleanup_compatibility_bytecode(venv_path)
        _write_venv_dependency_marker(venv_path, test_dependency_versions)


def _get_installed_ha_version(python_bin: Path) -> str:
    """Get the actually installed Home Assistant version inside the venv."""
    actual_ver = "unknown"
    with contextlib.suppress(subprocess.CalledProcessError, subprocess.TimeoutExpired):
        result = subprocess.run(
            ["uv", "--no-config", "pip", "show", "--python", python_bin, "homeassistant"],
            capture_output=True,
            text=True,
            check=True,
            cwd=_REPO_ROOT,
            timeout=_COMPATIBILITY_METADATA_PROBE_TIMEOUT_SECONDS,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                actual_ver = line.split(":", 1)[1].strip()
                break
    return actual_ver


def _get_installed_harness_pair(python_bin: Path) -> tuple[str, str]:
    """Return installed harness version and its exact Home Assistant requirement."""
    code = (
        "import importlib.metadata as md, json\n"
        f"package = {_TEST_HARNESS_PACKAGE!r}\n"
        "print(json.dumps({\n"
        '    "version": md.version(package),\n'
        '    "requirements": md.requires(package) or [],\n'
        "}, sort_keys=True))\n"
    )
    try:
        result = subprocess.run(
            [
                "uv",
                "--no-config",
                "run",
                "--no-project",
                "--python",
                str(python_bin),
                "python",
                "-c",
                code,
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
            timeout=_COMPATIBILITY_METADATA_PROBE_TIMEOUT_SECONDS,
        )
        payload = orjson.loads(result.stdout)
        if not isinstance(payload, dict) or not isinstance(
            (harness_version := payload.get("version")),
            str,
        ):
            raise ValueError("installed harness metadata has no version")
        required_ha = _validate_version_label(
            "harness_homeassistant_version",
            exact_homeassistant_requirement(
                payload.get("requirements"),
                f"installed {_TEST_HARNESS_PACKAGE} {harness_version}",
            ),
        )
        return (
            _validate_version_label("installed_harness_version", harness_version),
            required_ha,
        )
    except subprocess.CalledProcessError as err:
        raise ValueError(
            "could not inspect installed test harness: "
            f"{err}; stdout={err.stdout!r}; stderr={err.stderr!r}"
        ) from err
    except (
        subprocess.TimeoutExpired,
        orjson.JSONDecodeError,
        OSError,
        ValueError,
    ) as err:
        raise ValueError(f"could not inspect installed test harness: {err}") from err


def _verify_harness_pair(
    python_bin: Path,
    expected_ha_version: str,
    expected_harness_version: str,
) -> bool:
    """Verify that the installed harness exactly targets the installed HA release."""
    try:
        installed_harness, harness_required_ha = _get_installed_harness_pair(python_bin)
    except ValueError as err:
        print(f"HARNESS_MISMATCH: {err}", flush=True)
        return False
    mismatches: list[str] = []
    if versions_differ(installed_harness, expected_harness_version):
        mismatches.append(f"expected harness {expected_harness_version}, found {installed_harness}")
    if versions_differ(harness_required_ha, expected_ha_version):
        mismatches.append(
            f"harness {installed_harness} requires Home Assistant "
            f"{harness_required_ha}, expected {expected_ha_version}"
        )
    if mismatches:
        print(f"HARNESS_MISMATCH: {'; '.join(mismatches)}", flush=True)
        return False
    print(
        f"STEP_OK: harness {installed_harness} matches Home Assistant {expected_ha_version}",
        flush=True,
    )
    return True


def _run_pytest(python_bin: Path, ha_ver_display: str, pytest_args: Sequence[str]) -> None:
    """Run pytest inside the virtual environment."""
    env = os.environ.copy()
    env["PYTHONPATH"] = _REPO_ROOT
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    print(f"STEP_START: uv run pytest (Home Assistant {ha_ver_display})", flush=True)
    subprocess.run(
        [
            "uv",
            "--no-config",
            "run",
            "--no-project",
            "--python",
            python_bin,
            "pytest",
            *pytest_args,
        ],
        env=env,
        check=True,
        cwd=_REPO_ROOT,
        timeout=_COMPATIBILITY_PYTEST_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: uv run pytest (Home Assistant {ha_ver_display})", flush=True)


def _prepare_version_and_deps(
    ha_ver: str,
    harness_ver: str,
) -> tuple[str, str, dict[str, str]]:
    """Resolve one matched harness-backed Home Assistant pair and retrieve test dependencies."""
    ha_ver_to_install = ha_ver
    harness_ver_to_install = harness_ver
    if (ha_ver_to_install == "latest") != (harness_ver_to_install == "latest"):
        raise ValueError(
            "Home Assistant and test harness targets must both be 'latest' or both be fixed"
        )
    if ha_ver_to_install == "latest":
        latest_pair = _get_latest_matched_pair()
        ha_ver_to_install = latest_pair["ha_ver"]
        harness_ver_to_install = latest_pair["harness_ver"]
        absolute_latest = latest_pair["absolute_latest_ha_ver"]
        if absolute_latest != ha_ver_to_install:
            print(
                f"CANARY_LAG: newest Home Assistant {absolute_latest} has no matching "
                f"test harness; gating on {ha_ver_to_install} with harness "
                f"{harness_ver_to_install}",
                flush=True,
            )
    test_dependency_versions = _resolve_test_dependency_versions(
        ha_ver_to_install,
        harness_ver_to_install,
    )
    return ha_ver_to_install, harness_ver_to_install, test_dependency_versions


def _get_python_interpreter_version(python_bin: Path) -> str:
    """Get the Python interpreter version inside the venv."""
    result = subprocess.run(
        [
            "uv",
            "--no-config",
            "run",
            "--no-project",
            "--python",
            str(python_bin),
            "python",
            "-c",
            "import sys; print(sys.version.split()[0])",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=_REPO_ROOT,
        timeout=_COMPATIBILITY_METADATA_PROBE_TIMEOUT_SECONDS,
    )
    return result.stdout.strip()


def _verify_python_version_compatibility(python_bin: Path, ha_ver_to_install: str) -> None:
    """Verify that Python interpreter satisfies Home Assistant's requires-python."""
    try:
        url = f"https://pypi.org/pypi/homeassistant/{ha_ver_to_install}/json"
        requirements_text = _fetch_remote_text(url)
        payload = orjson.loads(requirements_text)
        requires_python = payload.get("info", {}).get("requires_python")
        if not requires_python:
            print(
                f"STEP_INFO: PyPI metadata for HA {ha_ver_to_install} does not specify "
                "requires-python; skipping compatibility verification",
                flush=True,
            )
            return
        actual_py_ver = _get_python_interpreter_version(python_bin)
        spec = SpecifierSet(requires_python)
        if Version(actual_py_ver) not in spec:
            raise ValueError(
                f"Python interpreter {actual_py_ver} at {python_bin} does not satisfy "
                f"Home Assistant {ha_ver_to_install} constraint '{requires_python}'"
            )
    except ValueError:
        raise
    except Exception as err:
        raise ValueError(
            f"Failed to fetch or verify PyPI requires-python for HA {ha_ver_to_install}: {err}"
        ) from err


def _prepare_venv_and_install(
    venv_path: Path,
    python_bin: Path,
    ha_ver: str,
    ha_ver_to_install: str,
    py_ver: str,
    reinstall: bool,
    test_dependency_versions: dict[str, str],
) -> bool:
    """Ensure the virtual environment is prepared and dependencies are installed.

    Returns:
        bool: True if setup succeeded, False if python binary is missing or incompatible.
    """
    created_venv = _ensure_venv(venv_path, py_ver)

    if not python_bin.exists():
        print(f"VALIDATION_ERROR: python not found at {python_bin}", flush=True)
        return False

    try:
        _verify_python_version_compatibility(python_bin, ha_ver_to_install)
    except ValueError as err:
        print(f"VALIDATION_ERROR: {err}", flush=True)
        return False

    installed_ha = _get_installed_ha_version(python_bin)
    marker_requires_reinstall = _dependency_marker_requires_reinstall(
        created_venv,
        venv_path,
        test_dependency_versions,
    )
    needs_reinstall = (
        reinstall
        or (installed_ha != ha_ver_to_install)
        or ha_ver == "latest"
        or marker_requires_reinstall
    )

    needs_install, refresh_deps = _determine_dependency_actions(
        needs_reinstall,
        created_venv,
        python_bin,
        test_dependency_versions,
    )

    _install_dependencies(
        venv_path,
        python_bin,
        ha_ver_to_install,
        needs_install,
        refresh_deps,
        test_dependency_versions,
        py_ver=py_ver,
        reset_before_install=needs_reinstall and not created_venv,
    )
    return True


def _verify_and_run_tests(
    python_bin: Path,
    pytest_bin: Path,
    ha_ver_to_install: str,
    harness_ver_to_install: str,
) -> tuple[bool, str]:
    """Verify virtual environment completeness and run the test suite.

    Returns:
        tuple[bool, str]: (Success status, Installed HA version)
    """
    ha_ver_display = _get_installed_ha_version(python_bin)
    if not pytest_bin.exists():
        print(f"VALIDATION_ERROR: pytest not found at {pytest_bin}", flush=True)
        return False, ha_ver_display

    if ha_ver_display != ha_ver_to_install:
        print(
            f"VALIDATION_ERROR: expected Home Assistant {ha_ver_to_install}, "
            f"found {ha_ver_display}",
            flush=True,
        )
        return False, ha_ver_display

    if not _verify_harness_pair(
        python_bin,
        ha_ver_to_install,
        harness_ver_to_install,
    ):
        return False, ha_ver_display

    _run_pytest(python_bin, ha_ver_display, _COMPATIBILITY_PYTEST_ARGS)
    return True, ha_ver_display


def _run_tests_for_version(
    ha_ver: str,
    harness_ver: str,
    py_ver: str,
    reinstall: bool,
) -> tuple[bool, str]:
    """Run the test suite for a specific Home Assistant version."""
    ha_ver_display = ha_ver

    try:
        (
            ha_ver_to_install,
            harness_ver_to_install,
            test_dependency_versions,
        ) = _prepare_version_and_deps(
            ha_ver,
            harness_ver,
        )

        ha_ver_display = ha_ver_to_install
        print(
            f"TESTING Home Assistant {ha_ver_to_install} with harness "
            f"{harness_ver_to_install} (Python {py_ver})",
            flush=True,
        )

        venv_path = Path(_get_venv_path(ha_ver_to_install, py_ver))
        python_bin = venv_path / "bin" / "python"
        pytest_bin = venv_path / "bin" / "pytest"

        if not _prepare_venv_and_install(
            venv_path=venv_path,
            python_bin=python_bin,
            ha_ver=ha_ver,
            ha_ver_to_install=ha_ver_to_install,
            py_ver=py_ver,
            reinstall=reinstall,
            test_dependency_versions=test_dependency_versions,
        ):
            return False, ha_ver_display

        return _verify_and_run_tests(
            python_bin=python_bin,
            pytest_bin=pytest_bin,
            ha_ver_to_install=ha_ver_to_install,
            harness_ver_to_install=harness_ver_to_install,
        )

    except (ValueError, RuntimeError) as err:
        print(f"VALIDATION_ERROR: {err}", flush=True)
        return False, ha_ver_display
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        ret_code = getattr(e, "returncode", 1)
        if isinstance(e, subprocess.TimeoutExpired):
            cmd_str = _format_cmd_str(e.cmd)
            print(f"STEP_FAILED: {cmd_str} TIMEOUT={e.timeout}", flush=True)
        elif isinstance(e, subprocess.CalledProcessError):
            cmd_str = _format_cmd_str(e.cmd)
            print(f"STEP_FAILED: {cmd_str} EXIT_CODE={ret_code}", flush=True)
            if e.stdout:
                print("\nSTDOUT:", flush=True)
                print(e.stdout, flush=True)
            if e.stderr:
                print("\nSTDERR:", flush=True)
                print(e.stderr, flush=True)
        else:
            cmd_str = getattr(e, "filename", "Unknown command")
            print(f"VALIDATION_ERROR: '{cmd_str}' not found.", flush=True)
        return False, ha_ver_display


def main() -> None:
    """Main entry point for the multi-version test script."""
    os.environ["NO_COLOR"] = "1"
    results: list[tuple[int, str, str, str, str, str]] = []

    if os.name != "posix":
        print("VALIDATION_ERROR: Non-POSIX environment detected", flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Test multiple HA versions.")
    parser.add_argument("--reinstall", action="store_true", help="Force reinstall of dependencies")
    parser.add_argument(
        "--clean", action="store_true", help="Delete all test venvs before starting"
    )
    parser.add_argument(
        "--resolve-latest-json",
        action="store_true",
        help="Print newest harness-backed Home Assistant pair JSON",
    )
    parser.add_argument(
        "--required-test-dependency-metadata-json",
        action="store_true",
        help="Print compatibility test dependency package metadata as JSON and exit",
    )
    parser.add_argument(
        "--parse-constraint-spec",
        help="Parse exact constraint spec for a package from requirements text on stdin",
    )
    parser.add_argument(
        "--verify-pair-python",
        type=Path,
        help="Verify an installed harness pair using this Python executable",
    )
    parser.add_argument("--expected-ha", help="Expected Home Assistant version")
    parser.add_argument("--expected-harness", help="Expected test harness version")
    args = parser.parse_args()

    if args.required_test_dependency_metadata_json:
        print(orjson.dumps(_required_test_dependency_metadata()).decode(), flush=True)
        return

    if args.resolve_latest_json:
        try:
            print(orjson.dumps(_get_latest_matched_pair()).decode(), flush=True)
        except ValueError as err:
            print(f"VALIDATION_ERROR: {err}", flush=True)
            sys.exit(1)
        return

    if args.parse_constraint_spec is not None:
        try:
            constraints_text = sys.stdin.read()
            version = _parse_requirements_dependency_version(
                constraints_text, args.parse_constraint_spec
            )
            print(f"{args.parse_constraint_spec}=={version}", flush=True)
        except (ValueError, OSError) as err:
            print(f"VALIDATION_ERROR: {err}", flush=True)
            sys.exit(1)
        return

    try:
        uv_executable_path = resolve_global_uv_path()
    except FileNotFoundError as err:
        missing_path = err.filename or "global uv executable"
        print(f"VALIDATION_ERROR: {missing_path!r} not found.", flush=True)
        sys.exit(1)
    print(
        f"STEP_INFO: Resolved global uv executable (available on PATH): {uv_executable_path}",
        flush=True,
    )

    if args.verify_pair_python is not None:
        if args.expected_ha is None or args.expected_harness is None:
            parser.error("--verify-pair-python requires --expected-ha and --expected-harness")
        if not _verify_harness_pair(
            args.verify_pair_python,
            _validate_version_label("expected_ha", args.expected_ha),
            _validate_version_label("expected_harness", args.expected_harness),
        ):
            sys.exit(1)
        return

    try:
        if args.clean:
            print("Cleaning up all test venvs...", flush=True)
            if os.path.exists(_VENVS_ROOT):
                shutil.rmtree(_VENVS_ROOT)

        for row_index, config in enumerate(_test_matrix(), start=1):
            ha_ver = config["ha_ver"]
            harness_ver = config["harness_ver"]
            py_ver = config["python_ver"]
            success, ha_version = _run_tests_for_version(
                ha_ver,
                harness_ver,
                py_ver,
                args.reinstall,
            )
            results.append(
                (
                    row_index,
                    ha_ver,
                    harness_ver,
                    py_ver,
                    ha_version,
                    "PASSED" if success else "FAILED",
                )
            )
    except (OSError, ValueError) as exc:
        print(f"VALIDATION_ERROR: {exc}", flush=True)
        sys.exit(1)

    print(flush=True)
    all_ok = True
    for row_index, ha_ver, harness_ver, py_ver, ha_version, status in results:
        display_ver = ha_version if ha_version == ha_ver else f"{ha_ver} → {ha_version}"
        print(
            f"Matrix row {row_index}: Home Assistant {display_ver}, harness "
            f"{harness_ver} (Python {py_ver}): {status}",
            flush=True,
        )
        if status != "PASSED":
            all_ok = False

    print(flush=True)
    if all_ok:
        print("VALIDATION_SUCCESS", flush=True)
    else:
        print("VALIDATION_FAILED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
