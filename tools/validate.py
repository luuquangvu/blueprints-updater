"""Unified Linux-only validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Prettier, Pytest).
It is optimized for Linux/WSL environments.
"""

import os
import shlex
import subprocess
import sys

VALIDATION_PIPELINE = [
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

    for cmd in VALIDATION_PIPELINE:
        full_cmd = shlex.join(cmd)
        print(f"\n>>> STEP: {full_cmd}")
        try:
            subprocess.run(full_cmd, shell=True, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            ret_code = e.returncode if isinstance(e, subprocess.CalledProcessError) else 1
            print(f"\nFAILED: {' '.join(cmd)} (Exit code: {ret_code})")
            if isinstance(e, FileNotFoundError):
                print(f"Error: Command not found. Please ensure '{cmd[0]}' is installed.")
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
