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
import textwrap
from pathlib import Path


def _print_uv_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> None:
    """Print informational details from uv sync dry-run output in JSON format."""
    if completed_process.returncode != 0:
        error_msg = completed_process.stderr.strip() or completed_process.stdout.strip()
        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} failed "
            f"with exit code {completed_process.returncode}; informational only",
            flush=True,
        )
        if error_msg:
            print(
                f"  Error details: {textwrap.shorten(error_msg, width=150, placeholder='...')}",
                flush=True,
            )
        return

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
    except (json.JSONDecodeError, TypeError):
        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
            flush=True,
        )


def _print_npm_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> None:
    """Print informational details from npm update dry-run output in JSON format."""
    if completed_process.returncode != 0:
        error_msg = completed_process.stderr.strip() or completed_process.stdout.strip()
        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} failed "
            f"with exit code {completed_process.returncode}; informational only",
            flush=True,
        )
        if error_msg:
            print(
                f"  Error details: {textwrap.shorten(error_msg, width=150, placeholder='...')}",
                flush=True,
            )
        return

    try:
        data = json.loads(completed_process.stdout)
    except json.JSONDecodeError:
        stdout = completed_process.stdout
        first_index = next((i for i, c in enumerate(stdout) if c in "{["), -1)
        if first_index == -1:
            print(
                f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
                flush=True,
            )
            return
        try:
            payload = stdout[first_index:]
            data = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            print(
                f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
                flush=True,
            )
            return
    except TypeError:
        print(
            f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
            flush=True,
        )
        return

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
            uv_sync_label = "uv sync --all-groups"
            print(f"STEP_START: {uv_sync_label}", flush=True)
            subprocess.run(["uv", "sync", "--all-groups"], check=True, cwd=repo_root)
            print(f"STEP_OK: {uv_sync_label}", flush=True)

        uv_upgrade_label = "uv sync --all-groups --upgrade --dry-run --output-format json"
        print(f"STEP_START: {uv_upgrade_label}", flush=True)
        uv_upgrade_check = subprocess.run(
            ["uv", "sync", "--all-groups", "--upgrade", "--dry-run", "--output-format", "json"],
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        _print_uv_dependency_update_notice(uv_upgrade_label, uv_upgrade_check)
        print(f"STEP_OK: {uv_upgrade_label}", flush=True)

        npm_update_label = "npm update --dry-run --no-audit --no-fund --json"
        print(f"STEP_START: {npm_update_label}", flush=True)
        npm_update_check = subprocess.run(
            ["npm", "update", "--dry-run", "--no-audit", "--no-fund", "--json"],
            check=False,
            capture_output=True,
            text=True,
            cwd=repo_root,
        )
        _print_npm_dependency_update_notice(npm_update_label, npm_update_check)
        print(f"STEP_OK: {npm_update_label}", flush=True)

        ruff_format_label = "uv run ruff format"
        print(f"STEP_START: {ruff_format_label}", flush=True)
        subprocess.run(["uv", "run", "ruff", "format"], check=True, cwd=repo_root)
        print(f"STEP_OK: {ruff_format_label}", flush=True)

        ruff_check_label = "uv run ruff check --fix"
        print(f"STEP_START: {ruff_check_label}", flush=True)
        subprocess.run(["uv", "run", "ruff", "check", "--fix"], check=True, cwd=repo_root)
        print(f"STEP_OK: {ruff_check_label}", flush=True)

        ty_check_label = "uv run ty check"
        print(f"STEP_START: {ty_check_label}", flush=True)
        subprocess.run(["uv", "run", "ty", "check"], check=True, cwd=repo_root)
        print(f"STEP_OK: {ty_check_label}", flush=True)

        pyright_label = "uv run pyright"
        print(f"STEP_START: {pyright_label}", flush=True)
        subprocess.run(["uv", "run", "pyright"], check=True, cwd=repo_root)
        print(f"STEP_OK: {pyright_label}", flush=True)

        interrogate_label = "uv run interrogate"
        print(f"STEP_START: {interrogate_label}", flush=True)
        subprocess.run(["uv", "run", "interrogate"], check=True, cwd=repo_root)
        print(f"STEP_OK: {interrogate_label}", flush=True)

        prettier_label = "npx prettier --log-level warn --write ."
        print(f"STEP_START: {prettier_label}", flush=True)
        subprocess.run(
            ["npx", "prettier", "--log-level", "warn", "--write", "."], check=True, cwd=repo_root
        )
        print(f"STEP_OK: {prettier_label}", flush=True)

        pytest_label = "uv run pytest --quiet"
        print(f"STEP_START: {pytest_label}", flush=True)
        subprocess.run(["uv", "run", "pytest", "--quiet"], check=True, cwd=repo_root)
        print(f"STEP_OK: {pytest_label}", flush=True)

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
            cmd_val = getattr(e, "filename", "Unknown command")
            print(f"VALIDATION_ERROR: {cmd_val!r} not found.", flush=True)

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
