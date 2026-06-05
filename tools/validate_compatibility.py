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
from pathlib import Path
from string import ascii_letters, digits

import orjson

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

_VENVS_ROOT = os.path.join(_REPO_ROOT, ".venvs")

_REQUIRED_TEST_DEPS = [
    "httpx[http2]",
    "pytest",
    "pytest-homeassistant-custom-component",
]

_ALNUM_CHARS = ascii_letters + digits
_SEP_CHAR = "."
_ALLOWED_VERSION_CHARS = _ALNUM_CHARS + _SEP_CHAR

_VERSION_PATTERN = re.compile(rf"^[{_ALNUM_CHARS}]+(?:{re.escape(_SEP_CHAR)}[{_ALNUM_CHARS}]+)*$")

_MATRIX_FILE = os.path.join(_REPO_ROOT, "tools", "compatibility_matrix.json")

_PYPI_HA_JSON_URL = "https://pypi.org/pypi/homeassistant/json"


def _load_matrix_data() -> list[dict[str, str]]:
    """Load compatibility matrix from the repository tools directory."""
    with open(_MATRIX_FILE, encoding="utf-8") as f:
        return orjson.loads(f.read())


_MATRIX_DATA = _load_matrix_data()

_TEST_MATRIX = [
    {"ha_ver": entry["ha_version"], "python_ver": entry["python_version"]} for entry in _MATRIX_DATA
]


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
    if not isinstance(label_value, str):
        raise ValueError(f"Invalid {label_name} value {label_value!r}; expected a string.")

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
    """Return canonical candidate path only if it is contained in canonical root_path.

    Uses os.path.abspath and startswith in a specific pattern recognized by
    CodeQL as a robust path injection sanitizer.
    """
    root = os.path.abspath(root_path)
    candidate = os.path.abspath(candidate_path)

    if candidate == root:
        return candidate

    if not candidate.startswith(root + os.sep):
        raise ValueError(f"Resolved path {candidate!r} escapes allowed root {root!r}.")
    return candidate


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


def _run_tests_for_version(ha_ver: str, py_ver: str, reinstall: bool) -> tuple[bool, str]:
    """Run the test suite for a specific Home Assistant version."""
    ha_ver_to_install = ha_ver
    if ha_ver == "latest":
        latest_ver = _get_latest_ha_version()
        ha_ver_to_install = latest_ver

    ha_ver_display = ha_ver_to_install
    print(f"TESTING Home Assistant {ha_ver_to_install} (Python {py_ver})", flush=True)

    venv_path = Path(_get_venv_path(ha_ver_to_install, py_ver))
    python_bin = venv_path / "bin" / "python"
    pytest_bin = venv_path / "bin" / "pytest"

    try:
        if not venv_path.exists():
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
            needs_install = True
        else:
            needs_install = reinstall

        if not python_bin.exists():
            print(f"VALIDATION_ERROR: python not found at {python_bin}", flush=True)
            return False, ha_ver_display

        if needs_install:
            overrides_dir = os.path.join(_REPO_ROOT, "scratch")
            os.makedirs(overrides_dir, exist_ok=True)
            overrides_file = os.path.join(overrides_dir, "overrides.txt")
            with open(overrides_file, "w", encoding="utf-8") as f:
                f.write(f"homeassistant == {ha_ver_to_install}\n")

            ha_spec = f"homeassistant=={ha_ver_to_install}"
            print(f"STEP_START: uv pip install {ha_spec}", flush=True)
            try:
                subprocess.run(
                    [
                        "uv",
                        "--no-config",
                        "pip",
                        "install",
                        "--upgrade",
                        "--overrides",
                        overrides_file,
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
            finally:
                with contextlib.suppress(OSError):
                    os.remove(overrides_file)
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

        if not pytest_bin.exists():
            print(f"VALIDATION_ERROR: pytest not found at {pytest_bin}", flush=True)
            return False, ha_ver_display

        actual_ver = "unknown"
        try:
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
        except subprocess.CalledProcessError:
            pass

        ha_ver_display = actual_ver

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
                "--no-cov",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            cwd=_REPO_ROOT,
        )
        print(f"STEP_OK: uv run pytest (Home Assistant {ha_ver_display})", flush=True)
        return True, ha_ver_display

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
    results: dict[tuple[str, str], tuple[str, str]] = {}

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

        for config in _TEST_MATRIX:
            ha_ver = config["ha_ver"]
            py_ver = config["python_ver"]
            success, ha_version = _run_tests_for_version(ha_ver, py_ver, args.reinstall)
            results[(ha_ver, py_ver)] = (ha_version, "PASSED" if success else "FAILED")
    except ValueError as exc:
        print(f"VALIDATION_ERROR: {exc}", flush=True)
        sys.exit(1)

    print(flush=True)
    all_ok = True
    for (ha_ver, py_ver), (ha_version, status) in results.items():
        display_ver = ha_version if ha_version == ha_ver else f"{ha_ver} → {ha_version}"
        print(f"Home Assistant {display_ver} (Python {py_ver}): {status}", flush=True)
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
