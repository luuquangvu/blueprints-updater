"""Unified POSIX-only validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Prettier, Pytest).
It is optimized for Linux, WSL, and macOS environments.

SECURITY NOTE:
Commands are intentionally hardcoded as explicit list literals in each subprocess.run call
to satisfy static analysis security audits. This prevents false positives related
to command injection that occur when iterating over dynamic command sequences.
"""

import json
import os
import subprocess
import sys
from pathlib import Path


def _print_uv_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> None:
    """Print informational details from uv sync dry-run output in JSON format."""
    try:
        data = json.loads(completed_process.stdout)
        changes = data.get("sync", {}).get("changes", [])
        if not changes:
            print(
                f"DEPENDENCY_UPDATE_CHECK_OK: {command_label!r} reported no updates",
                flush=True,
            )
            return

        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} found possible dependency updates; "
            "informational only",
            flush=True,
        )
        for change in changes:
            name = change.get("name", "unknown")
            prev = change.get("previous_version", "unknown")
            curr = change.get("version", "unknown")
            print(f"  - {name}: {prev} → {curr}", flush=True)
    except (json.JSONDecodeError, TypeError, KeyError):
        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
            flush=True,
        )


def _print_npm_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> None:
    """Print informational details from npm update dry-run output in JSON format."""
    try:
        data = json.loads(completed_process.stdout)
        added = data.get("added", 0)
        changed = data.get("changed", 0)
        removed = data.get("removed", 0)

        if added == 0 and changed == 0 and removed == 0:
            print(
                f"DEPENDENCY_UPDATE_CHECK_OK: {command_label!r} reported no updates",
                flush=True,
            )
            return

        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} found possible dependency updates "
            f"(Added: {added}, Changed: {changed}, Removed: {removed}); informational only",
            flush=True,
        )
    except (json.JSONDecodeError, TypeError):
        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
            flush=True,
        )


def _run_pipeline() -> None:
    """Execute the full validation pipeline.

    Each step is explicitly defined to ensure security scanners can verify
    the static nature of the commands being executed, avoiding dynamic
    variable execution in subprocess calls.

    Dependency update checks use dry-run commands and are informational only;
    available updates are reported without failing validation.
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
            uv_sync_command = ["uv", "sync", "--all-groups"]
            print(f"STEP_START: {' '.join(uv_sync_command)}", flush=True)
            subprocess.run(uv_sync_command, check=True, cwd=repo_root)
            print(f"STEP_OK: {' '.join(uv_sync_command)}", flush=True)

        uv_upgrade_command = [
            "uv",
            "sync",
            "--all-groups",
            "--upgrade",
            "--dry-run",
            "--output-format",
            "json",
        ]
        print(f"STEP_START: {' '.join(uv_upgrade_command)}", flush=True)
        uv_upgrade_check = subprocess.run(
            uv_upgrade_command,
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        _print_uv_dependency_update_notice(" ".join(uv_upgrade_command), uv_upgrade_check)
        print(f"STEP_OK: {' '.join(uv_upgrade_command)}", flush=True)

        npm_update_command = ["npm", "update", "--dry-run", "--no-audit", "--no-fund", "--json"]
        print(f"STEP_START: {' '.join(npm_update_command)}", flush=True)
        npm_update_check = subprocess.run(
            npm_update_command,
            check=True,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        _print_npm_dependency_update_notice(" ".join(npm_update_command), npm_update_check)
        print(f"STEP_OK: {' '.join(npm_update_command)}", flush=True)

        ruff_format_command = ["uv", "run", "ruff", "format"]
        print(f"STEP_START: {' '.join(ruff_format_command)}", flush=True)
        subprocess.run(ruff_format_command, check=True, cwd=repo_root)
        print(f"STEP_OK: {' '.join(ruff_format_command)}", flush=True)

        ruff_check_command = ["uv", "run", "ruff", "check", "--fix"]
        print(f"STEP_START: {' '.join(ruff_check_command)}", flush=True)
        subprocess.run(ruff_check_command, check=True, cwd=repo_root)
        print(f"STEP_OK: {' '.join(ruff_check_command)}", flush=True)

        ty_check_command = ["uv", "run", "ty", "check"]
        print(f"STEP_START: {' '.join(ty_check_command)}", flush=True)
        subprocess.run(ty_check_command, check=True, cwd=repo_root)
        print(f"STEP_OK: {' '.join(ty_check_command)}", flush=True)

        pyright_command = ["uv", "run", "pyright"]
        print(f"STEP_START: {' '.join(pyright_command)}", flush=True)
        subprocess.run(pyright_command, check=True, cwd=repo_root)
        print(f"STEP_OK: {' '.join(pyright_command)}", flush=True)

        interrogate_command = ["uv", "run", "interrogate"]
        print(f"STEP_START: {' '.join(interrogate_command)}", flush=True)
        subprocess.run(interrogate_command, check=True, cwd=repo_root)
        print(f"STEP_OK: {' '.join(interrogate_command)}", flush=True)

        prettier_command = ["npx", "prettier", "--log-level", "warn", "--write", "."]
        print(f"STEP_START: {' '.join(prettier_command)}", flush=True)
        subprocess.run(
            prettier_command,
            check=True,
            cwd=repo_root,
        )
        print(f"STEP_OK: {' '.join(prettier_command)}", flush=True)

        pytest_command = ["uv", "run", "pytest", "--quiet"]
        print(f"STEP_START: {' '.join(pytest_command)}", flush=True)
        subprocess.run(pytest_command, check=True, cwd=repo_root)
        print(f"STEP_OK: {' '.join(pytest_command)}", flush=True)

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
