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

    pipeline: list[list[str]] = [
        ["uv", "run", "ruff", "format"],
        ["uv", "run", "ruff", "check", "--fix"],
        ["uv", "run", "ty", "check"],
        ["uv", "run", "pyright"],
        ["uv", "run", "interrogate"],
        ["npx", "prettier", "--write", "."],
        ["uv", "run", "pytest"],
    ]

    for cmd in pipeline:
        cmd_str = " ".join(cmd)
        print(f"\n>>> STEP: {cmd_str}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"\nFAILED: {cmd_str} (Exit code: {e.returncode})")
            sys.exit(e.returncode)
        except FileNotFoundError:
            print(f"\nFAILED: {cmd_str} (executable not found: {cmd[0]})")
            print(f"Error: '{cmd[0]}' not found. Please ensure all dependencies are installed.")
            sys.exit(1)

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
