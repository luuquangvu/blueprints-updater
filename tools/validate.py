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

PIPELINE_ORDER = [
    ["uv", "run", "ruff", "format"],
    ["uv", "run", "ruff", "check", "--fix"],
    ["uv", "run", "ty", "check"],
    ["uv", "run", "pyright"],
    ["uv", "run", "interrogate"],
    ["uv", "run", "pytest"],
    ["npx", "prettier", "--write", "."],
]


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


def execute(cmd: list[str], env: dict | None = None) -> int | None:
    """Unified execution dispatcher using structural pattern matching."""
    if not cmd:
        return 0
    if env is None:
        env = os.environ.copy()

    match cmd:
        # --- Ruff Patterns ---
        case ["uv", "run", "ruff", *extra]:
            return subprocess.run(["uv", "run", "ruff", *extra], env=env, shell=False).returncode
        case ["ruff", *extra]:
            return subprocess.run(["ruff", *extra], env=env, shell=False).returncode

        # --- Ty Patterns ---
        case ["uv", "run", "ty", "check", *extra]:
            return subprocess.run(
                ["uv", "run", "ty", "check", *extra], env=env, shell=False
            ).returncode
        case ["ty", *extra]:
            return subprocess.run(["ty", *extra], env=env, shell=False).returncode

        # --- Pyright Patterns ---
        case ["uv", "run", "pyright", *extra]:
            return subprocess.run(["uv", "run", "pyright", *extra], env=env, shell=False).returncode
        case ["pyright", *extra]:
            return subprocess.run(["pyright", *extra], env=env, shell=False).returncode

        # --- Interrogate Patterns ---
        case ["uv", "run", "interrogate", *extra]:
            return subprocess.run(
                ["uv", "run", "interrogate", *extra], env=env, shell=False
            ).returncode
        case ["interrogate", *extra]:
            return subprocess.run(["interrogate", *extra], env=env, shell=False).returncode

        # --- Pytest Patterns ---
        case ["uv", "run", "pytest", *extra]:
            return subprocess.run(["uv", "run", "pytest", *extra], env=env, shell=False).returncode
        case ["pytest", *extra]:
            return subprocess.run(["pytest", *extra], env=env, shell=False).returncode

        # --- Prettier Patterns ---
        case ["npx", "prettier", *extra]:
            return subprocess.run(["npx", "prettier", *extra], env=env, shell=False).returncode
        case ["prettier", *extra]:
            return subprocess.run(["prettier", *extra], env=env, shell=False).returncode

        case _:
            return None


def run_pipeline() -> None:
    """Execute the full validation pipeline."""
    if os.name == "nt" and not IS_INSIDE_CONTAINER:
        _run_via_docker([])
        return

    print("Starting Unified Validation Pipeline")
    print("-" * 40)

    for cmd in PIPELINE_ORDER:
        print(f"\n>>> STEP: {' '.join(cmd)}")
        exit_code = execute(cmd)
        if exit_code != 0:
            print(f"\nFAILED: {' '.join(cmd)} (Exit code: {exit_code})")
            sys.exit(exit_code or 1)

    print("\n" + "=" * 40)
    print("ALL VALIDATION STEPS PASSED SUCCESSFULLY!")
    print("=" * 40)


def _handle_disallowed_command(cmd: list[str]) -> None:
    """Handle disallowed command attempts."""
    print(f"Error: Command '{' '.join(cmd)}' is not allowed.")
    print("This proxy only allows specific validation tools (Ruff, Ty, Pyright, etc.).")
    sys.exit(1)


def _run_via_docker(args: list[str]) -> None:
    """Run a command inside the Docker validator container."""
    msg = (
        f"Proxying to Docker: {' '.join(args)}"
        if args
        else "Routing full pipeline through Docker..."
    )
    print(f"Windows host detected. {msg}")

    try:
        docker_cmd = ["docker", "compose", "run", "--rm"]
        if not sys.stdin.isatty():
            docker_cmd.append("-T")
        docker_cmd.append("validate")

        final_args = ["uv", "run", "tools/validate.py", *args]
        subprocess.run(docker_cmd + final_args, check=True)
    except FileNotFoundError:
        print("\nERROR: Docker is required.")
        sys.exit(1)


def main() -> None:
    """Main entry point."""
    args = sys.argv[1:]

    try:
        if not args:
            run_pipeline()
        else:
            if os.name == "nt" and not IS_INSIDE_CONTAINER:
                _run_via_docker(args)
                return

            exit_code = execute(args)
            if exit_code is None:
                _handle_disallowed_command(args)

            sys.exit(exit_code)
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)
    except KeyboardInterrupt:
        print("\nValidation interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
