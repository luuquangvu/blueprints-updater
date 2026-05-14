"""Multi-version Home Assistant compatibility test suite.

This script manages virtual environments for testing the integration against
multiple Home Assistant core versions.

SECURITY NOTE:
Commands are intentionally hardcoded as explicit list literals in subprocess.run
calls where possible to satisfy static analysis security audits. This prevents
false positives related to command injection.
"""

import argparse
import os
import shutil
import subprocess
import sys
import tomllib

TEST_MATRIX = [
    ("2024.12", "3.13"),
    ("2026.2", "3.13"),
    ("2026.4", "3.14"),
    ("latest", "3.14"),
]

VENV_BASE = ".venv_homeassistant"


def get_dev_dependencies() -> list[str]:
    """Extract dev dependencies from pyproject.toml."""
    try:
        with open("pyproject.toml", "rb") as f:
            data = tomllib.load(f)
            dev_deps = data.get("dependency-groups", {}).get("dev", [])
            exclude = ["homeassistant", "pytest"]
            return [d for d in dev_deps if not any(d.startswith(ex) for ex in exclude)]
    except Exception as e:
        print(f"VALIDATION_ERROR: Could not parse pyproject.toml: {e}", flush=True)
        return []


def run_tests_for_version(ha_ver: str, py_ver: str, dev_deps: list[str], reinstall: bool) -> bool:
    """Run the test suite for a specific Home Assistant version."""
    print(f"Testing Home Assistant {ha_ver} (Python {py_ver})...", flush=True)

    venv_path = f"{VENV_BASE}_{ha_ver.replace('.', '_')}"
    python_bin = os.path.join(venv_path, "bin", "python")
    pytest_bin = os.path.join(venv_path, "bin", "pytest")

    try:
        if not os.path.exists(venv_path):
            print(f"STEP_START: uv python install {py_ver}", flush=True)
            subprocess.run(
                ["uv", "python", "install", py_ver, "--quiet"],
                check=True,
                capture_output=True,
                text=True,
            )
            print(f"STEP_OK: uv python install {py_ver}", flush=True)

            print(f"STEP_START: uv venv {venv_path}", flush=True)
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
                    *dev_deps,
                    "pytest",
                    "pytest-homeassistant-custom-component",
                    ha_spec,
                    "--quiet",
                ],
                check=True,
                capture_output=True,
                text=True,
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
            )
            print("STEP_OK: cleanup __pycache__", flush=True)

        if not os.path.exists(pytest_bin):
            print(f"VALIDATION_ERROR: pytest not found at {pytest_bin}", flush=True)
            return False

        env = os.environ.copy()
        env["PYTHONPATH"] = os.getcwd()
        env["PYTHONDONTWRITEBYTECODE"] = "1"

        print(f"STEP_START: pytest (Home Assistant {ha_ver})", flush=True)
        subprocess.run(
            [pytest_bin, "--quiet", "--no-cov"],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        print(f"STEP_OK: pytest (Home Assistant {ha_ver})", flush=True)
        return True

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
        return False


def main() -> None:
    """Main entry point for the multi-version test script."""
    os.environ["NO_COLOR"] = "1"

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
        for ha_ver, _ in TEST_MATRIX:
            venv_path = f"{VENV_BASE}_{ha_ver.replace('.', '_')}"
            if os.path.exists(venv_path):
                shutil.rmtree(venv_path)

    dev_deps = get_dev_dependencies()
    results = {}

    for ha_ver, py_ver in TEST_MATRIX:
        success = run_tests_for_version(ha_ver, py_ver, dev_deps, args.reinstall)
        results[ha_ver] = "PASSED" if success else "FAILED"

    print("\n", flush=True)
    all_ok = True
    for ver, status in results.items():
        print(f"Home Assistant {ver:12}: {status}", flush=True)
        if status != "PASSED":
            all_ok = False

    if all_ok:
        print("VALIDATION_SUCCESS", flush=True)
    else:
        print("VALIDATION_FAILED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
