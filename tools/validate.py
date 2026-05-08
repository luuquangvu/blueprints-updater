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
    # Set environment variables to disable color
    os.environ["NO_COLOR"] = "1"

    if os.name != "posix":
        print("VALIDATION_ERROR: Non-POSIX environment detected", flush=True)
        sys.exit(1)

    print("VALIDATION_START", flush=True)

    try:
        # 1. Ruff Format
        ruff_format = ["uv", "run", "ruff", "format"]
        print(f"STEP_START: {' '.join(ruff_format)}", flush=True)
        subprocess.run(ruff_format, check=True)
        print(f"STEP_OK: {' '.join(ruff_format)}", flush=True)

        # 2. Ruff Check
        ruff_check = ["uv", "run", "ruff", "check", "--fix"]
        print(f"STEP_START: {' '.join(ruff_check)}", flush=True)
        subprocess.run(ruff_check, check=True)
        print(f"STEP_OK: {' '.join(ruff_check)}", flush=True)

        # 3. Ty Check
        ty_check = ["uv", "run", "ty", "check"]
        print(f"STEP_START: {' '.join(ty_check)}", flush=True)
        subprocess.run(ty_check, check=True)
        print(f"STEP_OK: {' '.join(ty_check)}", flush=True)

        # 4. Pyright
        pyright = ["uv", "run", "pyright"]
        print(f"STEP_START: {' '.join(pyright)}", flush=True)
        subprocess.run(pyright, check=True)
        print(f"STEP_OK: {' '.join(pyright)}", flush=True)

        # 5. Interrogate
        interrogate = ["uv", "run", "interrogate"]
        print(f"STEP_START: {' '.join(interrogate)}", flush=True)
        subprocess.run(interrogate, check=True)
        print(f"STEP_OK: {' '.join(interrogate)}", flush=True)

        # 6. Prettier
        prettier = ["npx", "prettier", "--log-level", "warn", "--write", "."]
        print(f"STEP_START: {' '.join(prettier)}", flush=True)
        subprocess.run(prettier, check=True)
        print(f"STEP_OK: {' '.join(prettier)}", flush=True)

        # 7. Pytest
        pytest = ["uv", "run", "pytest"]
        print(f"STEP_START: {' '.join(pytest)}", flush=True)
        subprocess.run(pytest, check=True)
        print(f"STEP_OK: {' '.join(pytest)}", flush=True)

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
            print("VALIDATION_FAILED", flush=True)
        else:
            cmd_str = getattr(e, "filename", "Unknown command")
            print(f"VALIDATION_ERROR: '{cmd_str}' not found.", flush=True)

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
