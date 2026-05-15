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

with open("tools/compatibility_matrix.json", encoding="utf-8") as f:
    _MATRIX_DATA = json.load(f)

TEST_MATRIX = [
    {"ha_ver": entry["ha_version"], "python_ver": entry["python_version"]} for entry in _MATRIX_DATA
]

VENV_BASE = ".venv_homeassistant"

REQUIRED_TEST_DEPS = [
    "pytest-homeassistant-custom-component",
    "h2",
]


def run_tests_for_version(ha_ver: str, py_ver: str, reinstall: bool) -> tuple[bool, str]:
    """Run the test suite for a specific Home Assistant version."""
    ha_ver_display = ha_ver
    print(f"Testing Home Assistant {ha_ver} (Python {py_ver})...", flush=True)

    venv_path = f"{VENV_BASE}_{ha_ver.replace('.', '_')}"
    python_bin = os.path.join(venv_path, "bin", "python")
    pytest_bin = os.path.join(venv_path, "bin", "pytest")

    try:
        if not os.path.exists(venv_path):
            print(f"STEP_START: uv venv {venv_path} (Python {py_ver})", flush=True)
            subprocess.run(
                ["uv", "venv", "--no-project", "--python", py_ver, venv_path, "--quiet"],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"STEP_OK: uv venv {venv_path}", flush=True)
            needs_install = True
        else:
            needs_install = reinstall

        if needs_install:
            ha_spec = "homeassistant" if ha_ver == "latest" else f"homeassistant=={ha_ver}"
            print(f"STEP_START: uv pip install {ha_spec}", flush=True)
            subprocess.run(
                [
                    "uv",
                    "pip",
                    "install",
                    "--python",
                    python_bin,
                    ha_spec,
                    *REQUIRED_TEST_DEPS,
                    "--quiet",
                ],
                check=True,
                capture_output=True,
                text=True,
            )

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
            )
            print("STEP_OK: cleanup __pycache__", flush=True)

        if not os.path.exists(pytest_bin):
            print(f"VALIDATION_ERROR: pytest not found at {pytest_bin}", flush=True)
            return False, ha_ver_display

        actual_ver = "unknown"
        try:
            result = subprocess.run(
                ["uv", "pip", "show", "--python", python_bin, "homeassistant"],
                capture_output=True,
                text=True,
                check=True,
            )
            for line in result.stdout.splitlines():
                if line.startswith("Version:"):
                    actual_ver = line.split(":", 1)[1].strip()
                    break
        except subprocess.CalledProcessError:
            pass

        ha_ver_display = f"{ha_ver} ({actual_ver})" if ha_ver == "latest" else actual_ver

        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        print(f"STEP_START: pytest (Home Assistant {ha_ver_display})", flush=True)
        subprocess.run(
            ["uv", "run", "--no-project", "--python", python_bin, "pytest", "--quiet", "--no-cov"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"STEP_OK: pytest (Home Assistant {ha_ver_display})", flush=True)
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

    if args.clean:
        print("Cleaning up all test venvs...", flush=True)
        for config in TEST_MATRIX:
            ha_ver = config["ha_ver"]
            venv_path = f"{VENV_BASE}_{ha_ver.replace('.', '_')}"
            if os.path.exists(venv_path):
                shutil.rmtree(venv_path)

    for config in TEST_MATRIX:
        ha_ver = config["ha_ver"]
        py_ver = config["python_ver"]
        success, ha_version = run_tests_for_version(ha_ver, py_ver, args.reinstall)
        results[ha_version] = "PASSED" if success else "FAILED"

    print("\n", flush=True)
    all_ok = True
    for ha_version, status in results.items():
        print(f"Home Assistant {ha_version}: {status}", flush=True)
        if status != "PASSED":
            all_ok = False

    if all_ok:
        print("VALIDATION_SUCCESS", flush=True)
    else:
        print("VALIDATION_FAILED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
