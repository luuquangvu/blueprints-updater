"""Unified cross-platform validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Pytest, Prettier).
It handles environment detection:
- Windows Host: Routes all commands through Docker Compose.
- Linux Host/Inside Docker: Executes tools natively.
"""

import contextlib
import os
import subprocess
import sys
from typing import Any

VALIDATION_STEPS: list[dict[str, Any]] = [
    {"name": "Ruff Format", "cmd": ["uv", "run", "ruff", "format"]},
    {"name": "Ruff Check", "cmd": ["uv", "run", "ruff", "check", "--fix"]},
    {"name": "Ty Check", "cmd": ["uv", "run", "ty", "check"]},
    {"name": "Pyright", "cmd": ["uv", "run", "pyright"]},
    {"name": "Interrogate", "cmd": ["uv", "run", "interrogate"]},
    {"name": "Pytest", "cmd": ["uv", "run", "pytest"]},
    {"name": "Prettier", "cmd": ["npx", "prettier", "--write", "."]},
]

ALLOWED_TOOLS = {
    "uv",
    "npx",
    "pytest",
    "ruff",
    "ty",
    "pyright",
    "interrogate",
    "prettier",
}


def _check_container() -> bool:
    """Check if running inside a Docker or OCI container."""
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    with (
        contextlib.suppress(FileNotFoundError, PermissionError),
        open("/proc/1/cgroup", encoding="utf-8") as f,
    ):
        content = f.read()
        if any(x in content for x in ("docker", "containerd", "kubepods")):
            return True
    return False


IS_INSIDE_CONTAINER = _check_container()


def run_command(cmd: list[str], env_extra: dict | None = None) -> int:
    """Run a shell command and return its exit code."""
    env = os.environ.copy()
    if env_extra:
        env |= env_extra

    if IS_INSIDE_CONTAINER:
        env["UV_LINK_MODE"] = "copy"

    result = subprocess.run(cmd, env=env)
    return result.returncode


def run_pipeline() -> None:
    """Execute the full validation pipeline."""
    if os.name == "nt" and not IS_INSIDE_CONTAINER:
        print("Windows host detected. Routing full pipeline through Docker...")
        try:
            subprocess.run(
                ["docker", "compose", "run", "--rm", "validate", "uv", "run", "tools/validate.py"],
                check=True,
            )
        except FileNotFoundError:
            print("\nERROR: Docker is required to run the validation pipeline on Windows.")
            print("Please ensure Docker Desktop is installed and 'docker' is in your PATH.")
            sys.exit(1)
        return

    print("Starting Unified Validation Pipeline")
    print("-" * 40)

    for step in VALIDATION_STEPS:
        print(f"\n>>> STEP: {step['name']}")
        cmd_to_run: list[str] = step["cmd"]
        exit_code = run_command(cmd_to_run)

        if exit_code != 0:
            print(f"\nFAILED: {step['name']} (Exit code: {exit_code})")
            sys.exit(exit_code)

    print("\n" + "=" * 40)
    print("ALL VALIDATION STEPS PASSED SUCCESSFULLY!")
    print("=" * 40)


def proxy_single_command(args: list[str]) -> None:
    """Proxy a single command to the correct environment with security checks."""
    if not args:
        return

    base_cmd = args[0]
    if base_cmd == "uv" and len(args) > 2 and args[1] == "run":
        check_cmd = args[2]
    elif base_cmd == "npx" and len(args) > 1:
        check_cmd = args[1]
    else:
        check_cmd = base_cmd

    if check_cmd not in ALLOWED_TOOLS:
        print(f"Error: Command '{check_cmd}' is not allowed.")
        print("Only specific validation tools are permitted through this proxy.")
        print(f"Allowed: {', '.join(sorted(ALLOWED_TOOLS))}")
        sys.exit(1)

    if os.name == "nt" and not IS_INSIDE_CONTAINER:
        print(f"Windows host detected. Proxying to Docker: {' '.join(args)}")
        cmd = ["docker", "compose", "run", "--rm", "validate", *args]
        try:
            subprocess.run(cmd, check=True)
        except FileNotFoundError:
            print("\nERROR: Docker is required to run validation on Windows.")
            print("Please ensure Docker Desktop is installed and 'docker' is in your PATH.")
            sys.exit(1)
    else:
        exit_code = run_command(args)
        sys.exit(exit_code)


def main() -> None:
    """Main entry point."""
    args = sys.argv[1:]

    try:
        if not args:
            run_pipeline()
        else:
            proxy_single_command(args)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        print("\nValidation interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
