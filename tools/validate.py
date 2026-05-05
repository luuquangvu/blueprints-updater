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
    """Run a shell command safely and return its exit code."""
    if not cmd:
        return 0

    executable = cmd[0]
    if executable not in ALLOWED_TOOLS:
        _handle_disallowed_command(executable)

    env = os.environ.copy()
    if env_extra:
        env |= env_extra

    if executable == "uv":
        return subprocess.run(["uv", *cmd[1:]], env=env, shell=False).returncode
    if executable == "npx":
        return subprocess.run(["npx", *cmd[1:]], env=env, shell=False).returncode
    if executable == "pytest":
        return subprocess.run(["pytest", *cmd[1:]], env=env, shell=False).returncode
    if executable == "ruff":
        return subprocess.run(["ruff", *cmd[1:]], env=env, shell=False).returncode
    if executable == "ty":
        return subprocess.run(["ty", *cmd[1:]], env=env, shell=False).returncode
    if executable == "pyright":
        return subprocess.run(["pyright", *cmd[1:]], env=env, shell=False).returncode
    if executable == "interrogate":
        return subprocess.run(["interrogate", *cmd[1:]], env=env, shell=False).returncode
    if executable == "prettier":
        return subprocess.run(["prettier", *cmd[1:]], env=env, shell=False).returncode

    return subprocess.run([executable, *cmd[1:]], env=env, shell=False).returncode


def run_pipeline() -> None:
    """Execute the full validation pipeline."""
    if os.name == "nt" and not IS_INSIDE_CONTAINER:
        _run_in_docker(["uv", "run", "tools/validate.py"])
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


def _handle_disallowed_command(arg0: str) -> None:
    """Handle disallowed command attempts by printing error and exiting."""
    print(f"Error: Command '{arg0}' is not allowed.")
    print("Only specific validation tools are permitted through this proxy.")
    print(f"Allowed: {', '.join(sorted(ALLOWED_TOOLS))}")
    sys.exit(1)


def proxy_single_command(args: list[str]) -> None:
    """Proxy a single command to the correct environment with security checks."""
    if not args:
        return

    base_cmd = args[0]
    if base_cmd == "npx":
        if len(args) < 2:
            _handle_disallowed_command(base_cmd)
        check_cmd = args[1]
    elif base_cmd == "uv":
        if len(args) < 3 or args[1] != "run":
            _handle_disallowed_command(base_cmd)
        check_cmd = args[2]
    else:
        check_cmd = base_cmd

    if check_cmd not in ALLOWED_TOOLS:
        _handle_disallowed_command(check_cmd)

    if os.name == "nt" and not IS_INSIDE_CONTAINER:
        _run_in_docker(args)
    else:
        exit_code = run_command(args)
        sys.exit(exit_code)


def _run_in_docker(args: list[str]) -> None:
    """Run a command inside the Docker validator container."""
    print(f"Windows host detected. Proxying to Docker: {' '.join(args)}")

    try:
        if not sys.stdin.isatty():
            subprocess.run(
                ["docker", "compose", "run", "--rm", "-T", "validate", *args],
                check=True,
            )
        else:
            subprocess.run(
                ["docker", "compose", "run", "--rm", "validate", *args],
                check=True,
            )
    except FileNotFoundError:
        print("\nERROR: Docker is required to run validation on Windows.")
        print("Please ensure Docker Desktop is installed and 'docker' is in your PATH.")
        sys.exit(1)


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
