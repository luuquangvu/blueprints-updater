"""Multi-version Home Assistant compatibility test suite.

This script manages virtual environments for testing the integration against
multiple Home Assistant core versions.

SECURITY NOTE:
Commands are intentionally hardcoded as explicit list literals in subprocess.run
calls where possible to satisfy static analysis security audits. This prevents
false positives related to command injection.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

VENVS_ROOT = os.path.join(REPO_ROOT, ".venvs")

REQUIRED_TEST_DEPS = [
    "h2",
    "pytest",
    "pytest-homeassistant-custom-component",
]


def load_matrix_data() -> list[dict[str, str]]:
    """Load compatibility matrix from the repository tools directory."""
    with open("tools/compatibility_matrix.json", encoding="utf-8") as f:
        return json.load(f)


_MATRIX_DATA = load_matrix_data()

TEST_MATRIX = [
    {"ha_ver": entry["ha_version"], "python_ver": entry["python_version"]} for entry in _MATRIX_DATA
]


def validate_version_label(label_name: str, label_value: str) -> str:
    """Validate and sanitize a matrix version label to prevent path injection.

    Builds a fresh string from a whitelist of allowed characters to ensure
    no tainted data flows into path expressions.
    """
    if not isinstance(label_value, str):
        raise ValueError(f"Invalid {label_name} value {label_value!r}; expected a string.")

    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789."
    safe_chars = [c for c in label_value if c in allowed]
    safe_val = "".join(safe_chars)

    if not safe_val or safe_val != label_value:
        raise ValueError(
            f"Invalid {label_name} value {label_value!r}; only alphanumeric and '.' are allowed."
        )

    return safe_val


def ensure_within_root(root_path: str, candidate_path: str) -> str:
    """Return canonical candidate path only if it is contained in canonical root_path.

    Uses realpath and startswith to prevent path traversal and ensure the
    candidate is strictly under root (or is the root itself).
    """
    root = os.path.realpath(root_path)
    candidate = os.path.realpath(candidate_path)

    if candidate == root:
        return candidate

    prefix = root if root.endswith(os.sep) else root + os.sep
    if not candidate.startswith(prefix):
        raise ValueError(f"Resolved path {candidate!r} escapes allowed root {root!r}.")
    return candidate


def get_latest_ha_version() -> str:
    """Fetch the latest Home Assistant version from PyPI."""
    url = "https://pypi.org/pypi/homeassistant/json"
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            if response.status != 200:
                return "latest"
            data = json.loads(response.read().decode("utf-8"))
            version = data["info"]["version"]
            return validate_version_label("pypi_version", version)
    except Exception:
        return "latest"


def get_venv_path(ha_ver: str, py_ver: str) -> str:
    """Construct the virtual environment path for a specific version."""
    ha = validate_version_label("ha_ver", ha_ver)
    py = validate_version_label("py_ver", py_ver)
    venv_name = f"homeassistant_{ha}_python_{py}"

    if os.path.basename(venv_name) != venv_name:
        raise ValueError(f"Invalid venv name: {venv_name}")

    candidate = os.path.join(VENVS_ROOT, venv_name)
    return ensure_within_root(VENVS_ROOT, candidate)


def run_tests_for_version(ha_ver: str, py_ver: str, reinstall: bool) -> tuple[bool, str]:
    """Run the test suite for a specific Home Assistant version."""
    ha_ver_to_install = ha_ver
    if ha_ver == "latest":
        latest_ver = get_latest_ha_version()
        if latest_ver != "latest":
            ha_ver_to_install = latest_ver
            print(f"Latest Home Assistant version: {latest_ver}", flush=True)

    ha_ver_display = ha_ver_to_install
    print(f"TESTING Home Assistant {ha_ver_to_install} (Python {py_ver})", flush=True)

    venv_path = Path(get_venv_path(ha_ver, py_ver))
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
                cwd=REPO_ROOT,
            )
            print(f"STEP_OK: uv venv {venv_path} (Python {py_ver})", flush=True)
            needs_install = True
        else:
            needs_install = reinstall or ha_ver == "latest"

        if not python_bin.exists():
            print(f"VALIDATION_ERROR: python not found at {python_bin}", flush=True)
            return False, ha_ver_display

        if needs_install:
            if ha_ver_to_install == "latest":
                ha_spec = "homeassistant"
            else:
                ha_spec = f"homeassistant=={ha_ver_to_install}"
            print(f"STEP_START: uv pip install {ha_spec}", flush=True)
            subprocess.run(
                [
                    "uv",
                    "--no-config",
                    "pip",
                    "install",
                    "--upgrade",
                    "--python",
                    python_bin,
                    ha_spec,
                    *REQUIRED_TEST_DEPS,
                ],
                check=True,
                capture_output=True,
                text=True,
                cwd=REPO_ROOT,
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
                cwd=REPO_ROOT,
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
                cwd=REPO_ROOT,
            )
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    actual_ver = line.split(":", 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

        ha_ver_display = actual_ver

        env = os.environ.copy()
        env["PYTHONPATH"] = REPO_ROOT
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
                "--quiet",
                "--no-cov",
            ],
            env=env,
            check=True,
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
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
    results = {}

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
            for config in TEST_MATRIX:
                ha_ver = config["ha_ver"]
                py_ver = config["python_ver"]
                venv_path = Path(get_venv_path(ha_ver, py_ver))
                if venv_path.exists():
                    shutil.rmtree(venv_path)

        for config in TEST_MATRIX:
            ha_ver = config["ha_ver"]
            py_ver = config["python_ver"]
            success, ha_version = run_tests_for_version(ha_ver, py_ver, args.reinstall)
            results[(ha_ver, py_ver)] = (ha_version, "PASSED" if success else "FAILED")
    except ValueError as exc:
        print(f"VALIDATION_ERROR: {exc}", flush=True)
        sys.exit(1)

    print("\n", flush=True)
    all_ok = True
    for (_, py_ver), (ha_version, status) in results.items():
        print(f"Home Assistant {ha_version} (Python {py_ver}): {status}", flush=True)
        if status != "PASSED":
            all_ok = False

    if all_ok:
        print("VALIDATION_SUCCESS", flush=True)
    else:
        print("VALIDATION_FAILED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
