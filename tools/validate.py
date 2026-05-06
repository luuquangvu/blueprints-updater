"""Unified Linux-only validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Prettier, Pytest).
It is optimized for Linux/WSL environments.
"""

import os
import subprocess
import sys

PIPELINE_ORDER = [
    ["uv", "run", "ruff", "format"],
    ["uv", "run", "ruff", "check", "--fix"],
    ["uv", "run", "ty", "check"],
    ["uv", "run", "pyright"],
    ["uv", "run", "interrogate"],
    ["npx", "prettier", "--write", "."],
    ["uv", "run", "pytest"],
]


def run_pipeline() -> None:
    """Execute the full validation pipeline."""
    if os.name != "posix":
        print("Error: This script is only supported on Linux/WSL (POSIX systems).")
        sys.exit(1)

    print("\n" + "=" * 40)
    print("STARTING UNIFIED VALIDATION PIPELINE")
    print("=" * 40 + "\n")

    for cmd in PIPELINE_ORDER:
        print(f"\n>>> STEP: {' '.join(cmd)}")
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"\nFAILED: {' '.join(cmd)} (Exit code: {e.returncode})")
            sys.exit(e.returncode)

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
