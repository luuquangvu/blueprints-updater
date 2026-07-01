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
import uuid
from collections.abc import Generator
from pathlib import Path
from string import ascii_letters, digits
from typing import Any, TypedDict

import orjson

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_VENVS_ROOT = os.path.join(_REPO_ROOT, ".venvs")

_REQUIRED_TEST_DEPS = [
    "httpx[http2]",
    "pytest",
    "pytest-homeassistant-custom-component",
]

_COMPATIBILITY_PYTEST_ARGS = ["--no-cov"]
_COMPATIBILITY_METADATA_PROBE_TIMEOUT_SECONDS = 60

_ALNUM_CHARS = ascii_letters + digits
_ALLOWED_VERSION_CHARS = f"{_ALNUM_CHARS}."

_VERSION_PATTERN = re.compile(rf"^[{_ALNUM_CHARS}]+(?:\.[{_ALNUM_CHARS}]+)*$")

_MATRIX_FILE = os.path.join(_REPO_ROOT, "tools", "compatibility_matrix.json")

_PYPI_HA_JSON_URL = "https://pypi.org/pypi/homeassistant/json"


class CompatibilityConfig(TypedDict):
    """Validated Home Assistant compatibility test matrix entry."""

    ha_ver: str
    python_ver: str


def _load_matrix_data() -> list[dict[str, Any]]:
    """Load compatibility matrix from the repository tools directory."""
    with open(_MATRIX_FILE, encoding="utf-8") as f:
        loaded = orjson.loads(f.read())
    if not isinstance(loaded, list):
        raise ValueError("Compatibility matrix must be a list")
    matrix: list[dict[str, Any]] = []
    for index, entry in enumerate(loaded, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Compatibility matrix entry {index} must be an object")
        matrix.append({str(key): value for key, value in entry.items()})
    return matrix


def _matrix_entry_text(entry: dict[str, Any], key: str) -> str:
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
            py_ver = _validate_version_label(
                "python_version",
                _matrix_entry_text(entry, "python_version"),
            )
        except ValueError as err:
            raise ValueError(f"Matrix row {idx}: {err}") from err
        entries.append(
            CompatibilityConfig(
                ha_ver=ha_ver,
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


def _get_latest_ha_version() -> str:
    """Fetch the latest Home Assistant version from PyPI.

    Returns:
        The latest version string from PyPI.

    Raises:
        ValueError: If fetching or parsing the version fails.
    """
    try:
        with urllib.request.urlopen(_PYPI_HA_JSON_URL, timeout=20) as response:
            data = orjson.loads(response.read())
            version = data["info"]["version"]
    except (urllib.error.URLError, OSError, orjson.JSONDecodeError, KeyError) as err:
        raise ValueError(f"Failed to fetch latest Home Assistant version from PyPI: {err}") from err

    return _validate_version_label("pypi_version", version)


def _get_venv_path(ha_ver: str, py_ver: str) -> str:
    """Construct the virtual environment path for a specific version."""
    ha = _validate_version_label("ha_ver", ha_ver)
    py = _validate_version_label("py_ver", py_ver)

    venv_name = os.path.basename(f"homeassistant_{ha}_python_{py}")

    if os.path.basename(venv_name) != venv_name:
        raise ValueError(f"Invalid venv name: {venv_name}")

    candidate = os.path.join(_VENVS_ROOT, venv_name)
    return _ensure_within_root(_VENVS_ROOT, candidate)


@contextlib.contextmanager
def _overrides_file(ha_ver: str) -> Generator[str]:
    """Write a HA version-pin overrides file and remove it on exit.

    Yields the absolute path to the overrides file.
    """
    overrides_dir = os.path.join(_REPO_ROOT, "scratch")
    os.makedirs(overrides_dir, exist_ok=True)
    overrides_path = os.path.join(overrides_dir, f"overrides_{uuid.uuid4().hex}.txt")
    with open(overrides_path, "w", encoding="utf-8") as f:
        f.write(f"homeassistant == {ha_ver}\n")
    try:
        yield overrides_path
    finally:
        with contextlib.suppress(OSError):
            os.remove(overrides_path)


def _missing_required_test_deps(python_bin: Path) -> tuple[str, ...]:
    """Return required test dependency names that are absent from a compatibility venv."""
    code = (
        "import contextlib, importlib.metadata as md, json\n"
        f"packages = {_REQUIRED_TEST_DEPS!r}\n"
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
        installed = orjson.loads(result.stdout)
        if not isinstance(installed, dict):
            installed = {}
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
        installed = {}

    missing = []
    missing.extend(pkg for pkg in _REQUIRED_TEST_DEPS if pkg not in installed)
    return tuple(missing)


def _ensure_venv(venv_path: Path, py_ver: str) -> bool:
    """Ensure virtual environment exists, creating it if necessary.

    Returns:
        True if a new virtual environment was created, False otherwise.
    """
    python_bin = venv_path / "bin" / "python"
    if venv_path.exists() and python_bin.exists():
        return False
    if venv_path.exists():
        print(f"STEP_INFO: Re-creating incomplete virtual environment at {venv_path}", flush=True)
        shutil.rmtree(venv_path, ignore_errors=True)
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
    )
    print(f"STEP_OK: uv venv {venv_path} (Python {py_ver})", flush=True)
    return True


def _install_dependencies(
    python_bin: Path,
    ha_ver_to_install: str,
) -> None:
    """Install or upgrade required test dependencies in the compatibility venv."""
    ha_spec = f"homeassistant=={ha_ver_to_install}"
    print(f"STEP_START: uv pip install {ha_spec}", flush=True)
    with _overrides_file(ha_ver_to_install) as overrides_path:
        subprocess.run(
            [
                "uv",
                "--no-config",
                "pip",
                "install",
                "--upgrade",
                "--overrides",
                overrides_path,
                "--python",
                python_bin,
                ha_spec,
                *_REQUIRED_TEST_DEPS,
            ],
            check=True,
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
    print(f"STEP_OK: uv pip install {ha_spec}", flush=True)

    print("STEP_START: cleanup __pycache__", flush=True)
    subprocess.run(
        [
            "find",
            ".",
            "-name",
            "__pycache__",
            "-type",
            "d",
            "-exec",
            "rm",
            "-rf",
            "{}",
            "+",
        ],
        check=True,
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    print("STEP_OK: cleanup __pycache__", flush=True)


def _get_installed_ha_version(python_bin: Path) -> str:
    """Get the actually installed Home Assistant version inside the venv."""
    actual_ver = "unknown"
    with contextlib.suppress(subprocess.CalledProcessError):
        result = subprocess.run(
            ["uv", "--no-config", "pip", "show", "--python", python_bin, "homeassistant"],
            capture_output=True,
            text=True,
            check=True,
            cwd=_REPO_ROOT,
        )
        for line in result.stdout.splitlines():
            if line.startswith("Version:"):
                actual_ver = line.split(":", 1)[1].strip()
                break
    return actual_ver


def _run_pytest(python_bin: Path, ha_ver_display: str) -> None:
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
            *_COMPATIBILITY_PYTEST_ARGS,
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        cwd=_REPO_ROOT,
    )
    print(f"STEP_OK: uv run pytest (Home Assistant {ha_ver_display})", flush=True)


def _run_tests_for_version(ha_ver: str, py_ver: str, reinstall: bool) -> tuple[bool, str]:
    """Run the test suite for a specific Home Assistant version."""
    ha_ver_to_install = ha_ver
    ha_ver_display = ha_ver
    try:
        if ha_ver_to_install == "latest":
            latest_ver = _get_latest_ha_version()
            ha_ver_to_install = latest_ver

        ha_ver_display = ha_ver_to_install
        print(f"TESTING Home Assistant {ha_ver_to_install} (Python {py_ver})", flush=True)

        venv_path = Path(_get_venv_path(ha_ver_to_install, py_ver))
        python_bin = venv_path / "bin" / "python"
        pytest_bin = venv_path / "bin" / "pytest"

        created_venv = _ensure_venv(venv_path, py_ver)

        if not python_bin.exists():
            print(f"VALIDATION_ERROR: python not found at {python_bin}", flush=True)
            return False, ha_ver_display

        installed_ha_version = _get_installed_ha_version(python_bin)
        missing_deps = _missing_required_test_deps(python_bin)

        if reinstall or created_venv or installed_ha_version != ha_ver_to_install or missing_deps:
            if missing_deps:
                print(
                    f"STEP_INFO: installing missing test dependencies: {', '.join(missing_deps)}",
                    flush=True,
                )
            _install_dependencies(python_bin, ha_ver_to_install)
            installed_ha_version = _get_installed_ha_version(python_bin)

        if not pytest_bin.exists():
            print(f"VALIDATION_ERROR: pytest not found at {pytest_bin}", flush=True)
            return False, ha_ver_display

        ha_ver_display = installed_ha_version
        if ha_ver_display != ha_ver_to_install:
            print(
                f"VALIDATION_ERROR: expected Home Assistant {ha_ver_to_install}, "
                f"found {ha_ver_display}",
                flush=True,
            )
            return False, ha_ver_display
        _run_pytest(python_bin, ha_ver_display)
        return True, ha_ver_display

    except ValueError as err:
        print(f"VALIDATION_ERROR: {err}", flush=True)
        return False, ha_ver_display
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        ret_code = getattr(e, "returncode", 1)
        if isinstance(e, subprocess.CalledProcessError):
            cmd_val = e.cmd
            cmd_str = (
                " ".join(str(arg) for arg in cmd_val)
                if isinstance(cmd_val, (list, tuple))
                else str(cmd_val)
            )
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
    results: list[tuple[int, str, str, str, str]] = []

    if os.name != "posix":
        print("VALIDATION_ERROR: Non-POSIX environment detected", flush=True)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="Test multiple HA versions.")
    parser.add_argument("--reinstall", action="store_true", help="Force reinstall of dependencies")
    parser.add_argument(
        "--clean", action="store_true", help="Delete all test venvs before starting"
    )
    args = parser.parse_args()

    if not shutil.which("uv"):
        print("VALIDATION_ERROR: 'uv' is not installed.", flush=True)
        sys.exit(1)

    try:
        if args.clean:
            print("Cleaning up all test venvs...", flush=True)
            if os.path.exists(_VENVS_ROOT):
                shutil.rmtree(_VENVS_ROOT)

        for row_index, config in enumerate(_test_matrix(), start=1):
            ha_ver = config["ha_ver"]
            py_ver = config["python_ver"]
            success, ha_version = _run_tests_for_version(ha_ver, py_ver, args.reinstall)
            results.append(
                (row_index, ha_ver, py_ver, ha_version, "PASSED" if success else "FAILED")
            )
    except (OSError, ValueError) as exc:
        print(f"VALIDATION_ERROR: {exc}", flush=True)
        sys.exit(1)

    print(flush=True)
    all_ok = True
    for row_index, ha_ver, py_ver, ha_version, status in results:
        display_ver = ha_version if ha_version == ha_ver else f"{ha_ver} → {ha_version}"
        print(
            f"Matrix row {row_index}: Home Assistant {display_ver} (Python {py_ver}): {status}",
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
