"""Unified POSIX-only validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Prettier, Pytest).
It is optimized for Linux, WSL, and macOS environments.

SECURITY NOTE:
Commands are intentionally hardcoded as explicit list literals in each subprocess.run call
to satisfy static analysis security audits. This prevents false positives related
to command injection that occur when iterating over dynamic command sequences.
"""

import os
import subprocess
import sys
from pathlib import Path


def run_pipeline() -> None:
    """Execute the full validation pipeline.

    Each step is explicitly defined to ensure security scanners can verify
    the static nature of the commands being executed, avoiding dynamic
    variable execution in subprocess calls.
    """
    os.environ["NO_COLOR"] = "1"

    print("VALIDATION_START", flush=True)

    if os.name != "posix":
        print("VALIDATION_ERROR: Non-POSIX environment detected", flush=True)
        print("VALIDATION_FAILED", flush=True)
        sys.exit(1)

    try:
        repo_root = str(Path(__file__).resolve().parent.parent)
        venv_path = os.path.join(repo_root, ".venv")
        if not os.path.exists(venv_path):
            print("STEP_START: uv sync --all-groups", flush=True)
            subprocess.run(["uv", "sync", "--all-groups"], check=True)
            print("STEP_OK: uv sync --all-groups", flush=True)

        print("STEP_START: uv run ruff format", flush=True)
        subprocess.run(["uv", "run", "ruff", "format"], check=True)
        print("STEP_OK: uv run ruff format", flush=True)

        print("STEP_START: uv run ruff check --fix", flush=True)
        subprocess.run(["uv", "run", "ruff", "check", "--fix"], check=True)
        print("STEP_OK: uv run ruff check --fix", flush=True)

        print("STEP_START: uv run ty check", flush=True)
        subprocess.run(["uv", "run", "ty", "check"], check=True)
        print("STEP_OK: uv run ty check", flush=True)

        print("STEP_START: uv run pyright", flush=True)
        subprocess.run(["uv", "run", "pyright"], check=True)
        print("STEP_OK: uv run pyright", flush=True)

        print("STEP_START: uv run interrogate", flush=True)
        subprocess.run(["uv", "run", "interrogate"], check=True)
        print("STEP_OK: uv run interrogate", flush=True)

        print("STEP_START: npx prettier --log-level warn --write .", flush=True)
        subprocess.run(["npx", "prettier", "--log-level", "warn", "--write", "."], check=True)
        print("STEP_OK: npx prettier --log-level warn --write .", flush=True)

        print("STEP_START: uv run pytest --quiet", flush=True)
        subprocess.run(["uv", "run", "pytest", "--quiet"], check=True)
        print("STEP_OK: uv run pytest --quiet", flush=True)

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
        else:
            cmd_str = getattr(e, "filename", "Unknown command")
            print(f"VALIDATION_ERROR: '{cmd_str}' not found.", flush=True)

        print("VALIDATION_FAILED", flush=True)
        sys.exit(ret_code)

    print("VALIDATION_SUCCESS", flush=True)


def main() -> None:
    """Main entry point."""
    try:
        run_pipeline()
    except KeyboardInterrupt:
        print("VALIDATION_INTERRUPTED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
