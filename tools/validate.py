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


def run_pipeline() -> None:
    """Execute the full validation pipeline.

    Each step is explicitly defined to ensure security scanners can verify
    the static nature of the commands being executed, avoiding dynamic
    variable execution in subprocess calls.
    """
    if os.name != "posix":
        print(
            "Error: This script is only supported on POSIX systems (Linux, WSL, macOS)", flush=True
        )
        sys.exit(1)

    print("=" * 40, flush=True)
    print("STARTING UNIFIED VALIDATION PIPELINE", flush=True)
    print("=" * 40, flush=True)

    try:
        print("\n>>> STEP: uv run ruff format", flush=True)
        subprocess.run(["uv", "run", "ruff", "format"], check=True)

        print("\n>>> STEP: uv run ruff check --fix", flush=True)
        subprocess.run(["uv", "run", "ruff", "check", "--fix"], check=True)

        print("\n>>> STEP: uv run ty check", flush=True)
        subprocess.run(["uv", "run", "ty", "check"], check=True)

        print("\n>>> STEP: uv run pyright", flush=True)
        subprocess.run(["uv", "run", "pyright"], check=True)

        print("\n>>> STEP: uv run interrogate", flush=True)
        subprocess.run(["uv", "run", "interrogate"], check=True)

        print("\n>>> STEP: npx prettier --log-level warn --write .", flush=True)
        subprocess.run(["npx", "prettier", "--log-level", "warn", "--write", "."], check=True)

        print("\n>>> STEP: uv run pytest", flush=True)
        subprocess.run(["uv", "run", "pytest"], check=True)

    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        ret_code = getattr(e, "returncode", 1)
        if isinstance(e, subprocess.CalledProcessError):
            cmd_val = e.cmd
            cmd_str = (
                " ".join(str(arg) for arg in cmd_val)
                if isinstance(cmd_val, (list, tuple))
                else str(cmd_val)
            )
        else:
            cmd_str = getattr(e, "filename", "Unknown command")

        print(f"\nFAILED: {cmd_str} (Exit code: {ret_code})", flush=True)
        if isinstance(e, FileNotFoundError):
            print(
                f"Error: '{cmd_str}' not found. Please ensure all dependencies are installed.",
                flush=True,
            )
        sys.exit(ret_code)

    print("\n" + "=" * 40, flush=True)
    print("ALL VALIDATION STEPS PASSED SUCCESSFULLY", flush=True)
    print("=" * 40 + "\n", flush=True)


def main() -> None:
    """Main entry point."""
    try:
        run_pipeline()
    except KeyboardInterrupt:
        print("\nValidation interrupted by user.", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
