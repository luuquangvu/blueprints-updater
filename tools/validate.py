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
import textwrap
from collections.abc import Callable
from pathlib import Path

import orjson

_DEPENDENCY_SYNC_TIMEOUT_SECONDS = 300
_DEPENDENCY_UPDATE_TIMEOUT_SECONDS = 120


def _format_cmd(cmd_val: object) -> str:
    """Format a subprocess command value into a space-separated string."""
    return (
        " ".join(str(arg) for arg in cmd_val)
        if isinstance(cmd_val, (list, tuple))
        else str(cmd_val)
    )


def _report_dependency_check_timeout(command_label: str, timeout_seconds: int) -> None:
    """Report a non-fatal dependency update check timeout."""
    print(
        f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} timed out after "
        f"{timeout_seconds} seconds; informational only",
        flush=True,
    )
    print(
        f"STEP_WARNING: {command_label} timed out after {timeout_seconds} seconds",
        flush=True,
    )


def _report_dependency_check_failure(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> None:
    """Report a non-fatal dependency check process failure."""
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


def _report_invalid_json_failure(command_label: str) -> None:
    """Report that a dependency check command produced invalid JSON output."""
    print(
        f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} produced invalid JSON output",
        flush=True,
    )


def _parse_dependency_json(command_label: str, stdout: str) -> dict | None:
    """Parse and validate JSON dictionary from command stdout.

    Returns None if parsing or validation fails.
    """
    try:
        data = orjson.loads(stdout)
    except (orjson.JSONDecodeError, TypeError):
        idx = stdout.find("{")
        if idx == -1:
            idx = stdout.find("[")
        if idx != -1:
            try:
                data = orjson.loads(stdout[idx:])
            except (orjson.JSONDecodeError, TypeError):
                _report_invalid_json_failure(command_label)
                return None
        else:
            _report_invalid_json_failure(command_label)
            return None

    if not isinstance(data, dict):
        _report_invalid_json_failure(command_label)
        return None

    return data


def _print_uv_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> bool:
    """Print informational details from uv sync dry-run output in JSON format.

    uv change entries expose per-package install and uninstall actions.

    Returns:
        bool: True if the process completed successfully (exit code 0), False otherwise.
    """
    if completed_process.returncode != 0:
        _report_dependency_check_failure(command_label, completed_process)
        return False

    data = _parse_dependency_json(command_label, completed_process.stdout)
    if data is None:
        return False

    changes = data.get("sync", {}).get("changes", [])
    if not changes:
        print(
            f"DEPENDENCY_UPDATE_CHECK_OK: {command_label!r} reported no updates",
            flush=True,
        )
        return True

    installed_action = "installed"
    uninstalled_action = "uninstalled"
    allowed_actions = {installed_action, uninstalled_action}
    actions_by_name: dict[str, set[str]] = {}
    for change in changes:
        if not isinstance(change, dict):
            _report_invalid_json_failure(command_label)
            return False
        name = change.get("name")
        action = change.get("action")
        if not isinstance(name, str) or not isinstance(action, str):
            _report_invalid_json_failure(command_label)
            return False
        if action not in allowed_actions:
            _report_invalid_json_failure(command_label)
            return False
        actions_by_name.setdefault(name, set()).add(action)

    added = 0
    changed = 0
    removed = 0
    for actions in actions_by_name.values():
        installed = installed_action in actions
        uninstalled = uninstalled_action in actions
        if installed and uninstalled:
            changed += 1
        elif installed:
            added += 1
        elif uninstalled:
            removed += 1

    print(
        f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} found possible dependency updates "
        f"(Added: {added}, Changed: {changed}, Removed: {removed}); informational only",
        flush=True,
    )
    return True


def _print_npm_dependency_update_notice(
    command_label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> bool:
    """Print informational details from npm update dry-run output in JSON format.

    Returns:
        bool: True if the process completed successfully (exit code 0), False otherwise.
    """
    if completed_process.returncode != 0:
        _report_dependency_check_failure(command_label, completed_process)
        return False

    data = _parse_dependency_json(command_label, completed_process.stdout)
    if data is None:
        return False

    try:
        added = int(data.get("added", 0))
        changed = int(data.get("changed", 0))
        removed = int(data.get("removed", 0))
    except (ValueError, TypeError):
        _report_invalid_json_failure(command_label)
        return False

    if added == 0 and changed == 0 and removed == 0:
        print(
            f"DEPENDENCY_UPDATE_CHECK_OK: {command_label!r} reported no updates",
            flush=True,
        )
        return True

    print(
        f"DEPENDENCY_UPDATE_NOTICE: {command_label!r} found possible dependency updates "
        f"(Added: {added}, Changed: {changed}, Removed: {removed}); informational only",
        flush=True,
    )
    return True


def _print_process_output_summary(
    label: str,
    completed_process: subprocess.CompletedProcess[str],
) -> None:
    """Print shortened stdout and stderr of a completed process for synchronization checks."""
    if completed_process.stdout:
        print(f"{label} stdout:", flush=True)
        print(
            textwrap.shorten(completed_process.stdout, width=150, placeholder="..."),
            flush=True,
        )
    if completed_process.stderr:
        print(f"{label} stderr:", flush=True)
        print(
            textwrap.shorten(completed_process.stderr, width=150, placeholder="..."),
            flush=True,
        )


def _run_sync_repair_step(
    repo_root: str,
    *,
    command_label: str,
    check_output_label: str,
    repair_message: str,
    synchronized_message: str,
    run_check: Callable[[str], subprocess.CompletedProcess[str]],
    run_repair: Callable[[str], None],
) -> None:
    """Run a dependency sync check and repair the environment when needed."""
    print(f"STEP_START: {command_label}", flush=True)
    sync_check = run_check(repo_root)
    if sync_check.returncode != 0:
        print(repair_message, flush=True)
        _print_process_output_summary(check_output_label, sync_check)
        run_repair(repo_root)
    else:
        print(synchronized_message, flush=True)
    print(f"STEP_OK: {command_label}", flush=True)


def _run_dependency_update_notice_step(
    repo_root: str,
    *,
    command_label: str,
    run_check: Callable[[str], subprocess.CompletedProcess[str]],
    print_notice: Callable[[str, subprocess.CompletedProcess[str]], bool],
) -> None:
    """Run an informational dependency-update dry run and emit validation markers."""
    print(f"STEP_START: {command_label}", flush=True)
    try:
        update_check = run_check(repo_root)
    except subprocess.TimeoutExpired:
        _report_dependency_check_timeout(command_label, _DEPENDENCY_UPDATE_TIMEOUT_SECONDS)
    else:
        if print_notice(command_label, update_check):
            print(f"STEP_OK: {command_label}", flush=True)
        else:
            print(
                f"STEP_WARNING: {command_label} exited with code {update_check.returncode}",
                flush=True,
            )


def _run_uv_sync_check(repo_root: str) -> subprocess.CompletedProcess[str]:
    """Run uv dependency synchronization check."""
    return subprocess.run(
        ["uv", "sync", "--check", "--all-groups"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_DEPENDENCY_SYNC_TIMEOUT_SECONDS,
    )


def _repair_uv_sync(repo_root: str) -> None:
    """Synchronize uv dependencies."""
    subprocess.run(
        ["uv", "sync", "--all-groups"],
        check=True,
        cwd=repo_root,
        timeout=_DEPENDENCY_SYNC_TIMEOUT_SECONDS,
    )


def _run_npm_sync_check(repo_root: str) -> subprocess.CompletedProcess[str]:
    """Run npm dependency synchronization check."""
    return subprocess.run(
        ["npm", "ls"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=_DEPENDENCY_SYNC_TIMEOUT_SECONDS,
    )


def _repair_npm_sync(repo_root: str) -> None:
    """Synchronize npm dependencies."""
    subprocess.run(
        ["npm", "ci"],
        check=True,
        cwd=repo_root,
        timeout=_DEPENDENCY_SYNC_TIMEOUT_SECONDS,
    )


def _run_uv_dependency_update_check(repo_root: str) -> subprocess.CompletedProcess[str]:
    """Run the uv dependency-update dry run."""
    return subprocess.run(
        ["uv", "sync", "--all-groups", "--upgrade", "--dry-run", "--output-format", "json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=_DEPENDENCY_UPDATE_TIMEOUT_SECONDS,
    )


def _run_npm_dependency_update_check(repo_root: str) -> subprocess.CompletedProcess[str]:
    """Run the npm dependency-update dry run."""
    return subprocess.run(
        ["npm", "update", "--dry-run", "--no-audit", "--no-fund", "--json"],
        check=False,
        capture_output=True,
        text=True,
        cwd=repo_root,
        timeout=_DEPENDENCY_UPDATE_TIMEOUT_SECONDS,
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
        sys.exit(1)

    try:
        _validate_pipeline()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        ret_code = getattr(e, "returncode", 1)
        if isinstance(e, subprocess.TimeoutExpired):
            cmd_str = _format_cmd(e.cmd)
            print(f"STEP_FAILED: {cmd_str} TIMEOUT={e.timeout}", flush=True)
        elif isinstance(e, subprocess.CalledProcessError):
            cmd_str = _format_cmd(e.cmd)
            print(f"STEP_FAILED: {cmd_str} EXIT_CODE={ret_code}", flush=True)
        else:
            cmd_val = getattr(e, "filename", "Unknown command")
            print(f"VALIDATION_ERROR: {cmd_val!r} not found.", flush=True)

        print(flush=True)
        print("VALIDATION_FAILED", flush=True)
        sys.exit(ret_code)

    print(flush=True)
    print("VALIDATION_SUCCESS", flush=True)


def _validate_pipeline() -> None:
    """Run the validation pipeline steps in order."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    _run_sync_repair_step(
        repo_root,
        command_label="uv sync --check --all-groups",
        check_output_label="uv sync --check",
        repair_message="Environment is out of sync. Running 'uv sync --all-groups'",
        synchronized_message="Environment is already synchronized.",
        run_check=_run_uv_sync_check,
        run_repair=_repair_uv_sync,
    )
    _run_sync_repair_step(
        repo_root,
        command_label="npm ls",
        check_output_label="npm ls",
        repair_message="NPM packages are out of sync. Running 'npm ci'",
        synchronized_message="NPM packages are already synchronized.",
        run_check=_run_npm_sync_check,
        run_repair=_repair_npm_sync,
    )
    _run_dependency_update_notice_step(
        repo_root,
        command_label="uv sync --all-groups --upgrade --dry-run --output-format json",
        run_check=_run_uv_dependency_update_check,
        print_notice=_print_uv_dependency_update_notice,
    )
    _run_dependency_update_notice_step(
        repo_root,
        command_label="npm update --dry-run --no-audit --no-fund --json",
        run_check=_run_npm_dependency_update_check,
        print_notice=_print_npm_dependency_update_notice,
    )

    ruff_format_label = "uv run --no-project ruff format"
    print(f"STEP_START: {ruff_format_label}", flush=True)
    subprocess.run(["uv", "run", "--no-project", "ruff", "format"], check=True, cwd=repo_root)
    print(f"STEP_OK: {ruff_format_label}", flush=True)

    ruff_check_label = "uv run --no-project ruff check --fix"
    print(f"STEP_START: {ruff_check_label}", flush=True)
    subprocess.run(
        ["uv", "run", "--no-project", "ruff", "check", "--fix"], check=True, cwd=repo_root
    )
    print(f"STEP_OK: {ruff_check_label}", flush=True)

    ty_check_label = "uv run --no-project ty check"
    print(f"STEP_START: {ty_check_label}", flush=True)
    subprocess.run(["uv", "run", "--no-project", "ty", "check"], check=True, cwd=repo_root)
    print(f"STEP_OK: {ty_check_label}", flush=True)

    pyright_label = "uv run --no-project pyright"
    print(f"STEP_START: {pyright_label}", flush=True)
    subprocess.run(["uv", "run", "--no-project", "pyright"], check=True, cwd=repo_root)
    print(f"STEP_OK: {pyright_label}", flush=True)

    interrogate_label = "uv run --no-project interrogate"
    print(f"STEP_START: {interrogate_label}", flush=True)
    subprocess.run(["uv", "run", "--no-project", "interrogate"], check=True, cwd=repo_root)
    print(f"STEP_OK: {interrogate_label}", flush=True)

    prettier_label = "npx prettier --log-level warn --write ."
    print(f"STEP_START: {prettier_label}", flush=True)
    subprocess.run(
        ["npx", "prettier", "--log-level", "warn", "--write", "."], check=True, cwd=repo_root
    )
    print(f"STEP_OK: {prettier_label}", flush=True)

    pytest_label = "uv run --no-project pytest"
    print(f"STEP_START: {pytest_label}", flush=True)
    subprocess.run(["uv", "run", "--no-project", "pytest"], check=True, cwd=repo_root)
    print(f"STEP_OK: {pytest_label}", flush=True)


def main() -> None:
    """Main entry point."""
    try:
        _run_pipeline()
    except KeyboardInterrupt:
        print("VALIDATION_INTERRUPTED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
