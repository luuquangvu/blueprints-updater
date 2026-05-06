"""Unified POSIX-only validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Prettier, Pytest).
It is optimized for Linux, WSL, and macOS environments.
"""

import os
import subprocess
import sys


def run_pipeline() -> None:
    """Execute the full validation pipeline."""
    if os.name != "posix":
        print("Error: This script is only supported on POSIX systems (Linux, WSL, macOS).")
        sys.exit(1)

    print("\n" + "=" * 40)
    print("STARTING UNIFIED VALIDATION PIPELINE")
    print("=" * 40 + "\n")

    try:
        print("\n>>> STEP: uv run ruff format")
        subprocess.run(["uv", "run", "ruff", "format"], check=True)

        print("\n>>> STEP: uv run ruff check --fix")
        subprocess.run(["uv", "run", "ruff", "check", "--fix"], check=True)

        print("\n>>> STEP: uv run ty check")
        subprocess.run(["uv", "run", "ty", "check"], check=True)

        print("\n>>> STEP: uv run pyright")
        subprocess.run(["uv", "run", "pyright"], check=True)

        print("\n>>> STEP: uv run interrogate")
        subprocess.run(["uv", "run", "interrogate"], check=True)

        print("\n>>> STEP: npx prettier --write .")
        subprocess.run(["npx", "prettier", "--write", "."], check=True)

        print("\n>>> STEP: uv run pytest")
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

        print(f"\nFAILED: {cmd_str} (Exit code: {ret_code})")
        if isinstance(e, FileNotFoundError):
            print(f"Error: '{cmd_str}' not found. Please ensure all dependencies are installed.")
        sys.exit(ret_code)

    print("\n" + "=" * 40)
    print("ALL VALIDATION STEPS PASSED SUCCESSFULLY")
    print("=" * 40 + "\n")


def main() -> None:
    """Main entry point."""
    try:
        run_pipeline()
    except KeyboardInterrupt:
        print("\nValidation interrupted by user.")
        sys.exit(1)


if __name__ == "__main__":
    main()
