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


def _print_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
    no_update_marker: str,
) -> None:
    """Print dry-run details when dependency updates are available."""
    output_parts = [completed_process.stdout.strip(), completed_process.stderr.strip()]
    output = "\n".join(part for part in output_parts if part)

    if no_update_marker in output:
        print(f"DEPENDENCY_UPDATE_CHECK_OK: {command_label} reported no updates", flush=True)
        return

    if output:
        print(f"DEPENDENCY_UPDATE_AVAILABLE: {command_label}", flush=True)
        print(output, flush=True)
        return

    print(f"DEPENDENCY_UPDATE_CHECK_OK: {command_label} produced no update output", flush=True)


def _run_pipeline() -> None:
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
            subprocess.run(["uv", "sync", "--all-groups"], check=True, cwd=repo_root)
            print("STEP_OK: uv sync --all-groups", flush=True)

        print("STEP_START: uv sync --all-groups --upgrade --dry-run", flush=True)
        uv_upgrade_check = subprocess.run(
            ["uv", "sync", "--all-groups", "--upgrade", "--dry-run"],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        _print_dependency_update_notice(
            "uv sync --all-groups --upgrade --dry-run",
            uv_upgrade_check,
            "Would make no changes",
        )
        print("STEP_OK: uv sync --all-groups --upgrade --dry-run", flush=True)

        print("STEP_START: npm update --dry-run --no-audit --no-fund", flush=True)
        npm_update_check = subprocess.run(
            ["npm", "update", "--dry-run", "--no-audit", "--no-fund"],
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        _print_dependency_update_notice(
            "npm update --dry-run --no-audit --no-fund",
            npm_update_check,
            "up to date",
        )
        print("STEP_OK: npm update --dry-run --no-audit --no-fund", flush=True)

        print("STEP_START: uv run ruff format", flush=True)
        subprocess.run(["uv", "run", "ruff", "format"], check=True, cwd=repo_root)
        print("STEP_OK: uv run ruff format", flush=True)

        print("STEP_START: uv run ruff check --fix", flush=True)
        subprocess.run(["uv", "run", "ruff", "check", "--fix"], check=True, cwd=repo_root)
        print("STEP_OK: uv run ruff check --fix", flush=True)

        print("STEP_START: uv run ty check", flush=True)
        subprocess.run(["uv", "run", "ty", "check"], check=True, cwd=repo_root)
        print("STEP_OK: uv run ty check", flush=True)

        print("STEP_START: uv run pyright", flush=True)
        subprocess.run(["uv", "run", "pyright"], check=True, cwd=repo_root)
        print("STEP_OK: uv run pyright", flush=True)

        print("STEP_START: uv run interrogate", flush=True)
        subprocess.run(["uv", "run", "interrogate"], check=True, cwd=repo_root)
        print("STEP_OK: uv run interrogate", flush=True)

        print("STEP_START: npx prettier --log-level warn --write .", flush=True)
        subprocess.run(
            ["npx", "prettier", "--log-level", "warn", "--write", "."], check=True, cwd=repo_root
        )
        print("STEP_OK: npx prettier --log-level warn --write .", flush=True)

        print("STEP_START: uv run pytest --quiet", flush=True)
        subprocess.run(["uv", "run", "pytest", "--quiet"], check=True, cwd=repo_root)
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
        _run_pipeline()
    except KeyboardInterrupt:
        print("VALIDATION_INTERRUPTED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
