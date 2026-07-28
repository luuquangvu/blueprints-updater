"""Unified POSIX-only validation script.

This script manages the validation pipeline (Ruff, Ty, Pyright, Interrogate, Prettier, Pytest).
It is optimized for Linux, WSL, and macOS environments.

SECURITY NOTE:
Commands are intentionally hardcoded as explicit list literals in subprocess.run
calls where possible to satisfy static analysis security audits. This prevents
false positives related to command injection. The repeated step implementations
are deliberately not consolidated into a DRY shared command runner: security
scanners must be able to verify each literal argument list at its subprocess
call site instead of tracing a command value through another function.
"""

import contextlib
import importlib.metadata as md
import importlib.util
import os
import re
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import orjson
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

_DEPENDENCY_SYNC_TIMEOUT_SECONDS = 300
_DEPENDENCY_UPDATE_TIMEOUT_SECONDS = 120
_FORMAT_STEP_TIMEOUT_SECONDS = 120
_STATIC_ANALYSIS_STEP_TIMEOUT_SECONDS = 180
_PRETTIER_STEP_TIMEOUT_SECONDS = 120
_PYTEST_STEP_TIMEOUT_SECONDS = 600
_VALIDATION_STEP_TIMEOUT_SECONDS = 300

_PACKAGE_NORM_PATTERN = re.compile(r"[._-]+")


def normalize_package_name(package_name: str) -> str:
    """Normalize a package name to its canonical base form."""
    base_name = package_name.split("[", 1)[0].split(";", 1)[0].split("=", 1)[0].strip()
    return _PACKAGE_NORM_PATTERN.sub("-", base_name).lower()


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
        check=False,
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
        check=False,
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

    Each pipeline step is implemented as an explicit step function with hardcoded
    command list literals in subprocess.run calls to satisfy static security audits,
    prevent command injection false positives, and ensure deterministic step markers.
    Keep this intentionally non-DRY structure: moving command execution into a shared
    runner would hide the literal argument lists from scanners that only inspect the
    immediate subprocess call site and could reintroduce security false positives.

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


def _valid_package_name(raw_name: str) -> str:
    """Return a canonical package name when it starts alphanumerically."""
    normalized = normalize_package_name(raw_name)
    return normalized if normalized and normalized[0].isalnum() else ""


def _egg_requirement_name(raw_req: str) -> str:
    """Return a package name from a legacy egg URL fragment."""
    if "#egg=" not in raw_req:
        return ""
    egg_name = raw_req.split("#egg=", 1)[1].split("&", 1)[0].split(";", 1)[0].strip()
    return _valid_package_name(egg_name) if egg_name else ""


def _direct_reference_name(requirement: str) -> str:
    """Return a package name from a PEP 508 direct reference."""
    if (
        "@" not in requirement
        or requirement.startswith(("git+", "http://", "https://", "svn+", "hg+", "bzr+"))
        or "://" in requirement.split("@", 1)[0]
    ):
        return ""
    prefix = requirement.split("@", 1)[0].strip()
    for separator in ("[", "==", "!=", "~=", ">=", "<=", ">", "<"):
        prefix = prefix.split(separator, 1)[0].strip()
    return _valid_package_name(prefix)


def _url_requirement_name(requirement: str) -> str:
    """Return a package name inferred from a requirement URL."""
    if "://" not in requirement and not requirement.startswith(("git+", "hg+", "svn+", "bzr+")):
        return ""
    url_clean = requirement.split("#", 1)[0].split("?", 1)[0].strip()
    if "://" in url_clean:
        rest = url_clean.split("://", 1)[1]
        path_part = rest.split("/", 1)[1] if "/" in rest else ""
    elif ":" in url_clean:
        path_part = url_clean.split(":", 1)[-1]
    else:
        path_part = url_clean

    repo_path = path_part.split("@", 1)[0].rstrip("/")
    stem = repo_path.rsplit("/", 1)[-1]
    for extension in (".git", ".whl", ".tar.gz", ".zip"):
        if stem.endswith(extension):
            stem = stem[: -len(extension)]
            break
    return _valid_package_name(stem)


def _plain_requirement_name(requirement: str) -> str:
    """Return a package name from a non-URL requirement."""
    for separator in (
        "[",
        "==",
        "!=",
        "~=",
        ">=",
        "<=",
        ">",
        "<",
        "@",
        "~",
        "!",
    ):
        requirement = requirement.split(separator, 1)[0].strip()
    return _valid_package_name(requirement)


def _packaging_requirement_name(requirement: str) -> str | None:
    """Return the package name parsed by Packaging when valid."""
    try:
        return normalize_package_name(Requirement(requirement).name)
    except InvalidRequirement:
        return None


def _parse_package_name_from_req(raw_req: str) -> str:
    """Extract canonicalized package name from a requirement specifier string."""
    initial_requirement = raw_req.split("#", 1)[0].strip()
    if not initial_requirement:
        return ""
    if package_name := _packaging_requirement_name(initial_requirement):
        return package_name
    if egg_name := _egg_requirement_name(raw_req):
        return egg_name
    requirement = raw_req.split("#", 1)[0].split(";", 1)[0].strip()
    if not requirement or requirement.startswith("-"):
        return ""
    return (
        _direct_reference_name(requirement)
        or _url_requirement_name(requirement)
        or _plain_requirement_name(requirement)
    )


def _load_ha_package_constraints(constraints_path: Path) -> dict[str, str]:
    """Parse package versions from Home Assistant's package_constraints.txt."""
    constraints: dict[str, str] = {}
    for raw_line in constraints_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        package_name, separator, version = line.partition("==")
        if separator == "==":
            norm_name = normalize_package_name(package_name)
            constraints[norm_name] = version.strip()
    return constraints


def _load_ha_manifest_constraints() -> dict[str, str]:
    """Parse package versions from Home Assistant component manifests."""
    constraints: dict[str, str] = {}
    with contextlib.suppress(Exception):
        spec = importlib.util.find_spec("homeassistant.components")
        if not spec or not spec.submodule_search_locations:
            return constraints

        for location in spec.submodule_search_locations:
            component_root = Path(location)
            if not component_root.is_dir():
                continue
            for manifest_path in sorted(component_root.glob("*/manifest.json")):
                try:
                    manifest = orjson.loads(manifest_path.read_bytes())
                except (OSError, orjson.JSONDecodeError) as err:
                    print(
                        f"STEP_INFO: Warning: skipped manifest {manifest_path}: {err!r}",
                        flush=True,
                    )
                    continue

                raw_requirements = manifest.get("requirements")
                if isinstance(raw_requirements, list):
                    for raw_spec in raw_requirements:
                        if isinstance(raw_spec, str) and "==" in raw_spec:
                            spec_name, _, spec_ver = raw_spec.partition("==")
                            norm_name = normalize_package_name(spec_name)
                            spec_ver_clean = spec_ver.strip()
                            if (
                                norm_name in constraints
                                and constraints[norm_name] != spec_ver_clean
                            ):
                                print(
                                    f"STEP_INFO: Conflicting constraint for {norm_name}: "
                                    f"{constraints[norm_name]} vs {spec_ver_clean} "
                                    f"in {manifest_path}",
                                    flush=True,
                                )
                            else:
                                constraints[norm_name] = spec_ver_clean
    return constraints


def _add_requirement_list(packages: set[str], requirements: Any) -> None:
    """Add package names from a TOML requirement list."""
    if not isinstance(requirements, list):
        return
    for raw_requirement in requirements:
        if isinstance(raw_requirement, str) and (
            package_name := _parse_package_name_from_req(raw_requirement)
        ):
            packages.add(package_name)


def _project_table_packages(project_table: Any) -> set[str]:
    """Return packages declared by the PEP 621 project table."""
    packages: set[str] = set()
    if not isinstance(project_table, dict):
        return packages
    _add_requirement_list(packages, project_table.get("dependencies"))
    optional_dependencies = project_table.get("optional-dependencies")
    if isinstance(optional_dependencies, dict):
        for requirements in optional_dependencies.values():
            _add_requirement_list(packages, requirements)
    return packages


def _add_dependency_group(
    packages: set[str],
    dependency_groups: Mapping[str, Any],
    group_name: str,
    active_path: frozenset[str] = frozenset(),
) -> None:
    """Resolve one PEP 735 dependency group and its includes."""
    if group_name in active_path:
        return
    group_items = dependency_groups.get(group_name)
    if not isinstance(group_items, list):
        return
    next_path = active_path | {group_name}
    for raw_requirement in group_items:
        if isinstance(raw_requirement, str):
            if package_name := _parse_package_name_from_req(raw_requirement):
                packages.add(package_name)
            continue
        if isinstance(raw_requirement, dict):
            included_group = raw_requirement.get("include-group")
            if isinstance(included_group, str):
                _add_dependency_group(
                    packages,
                    dependency_groups,
                    included_group,
                    next_path,
                )


def _dependency_group_packages(dependency_groups: Any) -> set[str]:
    """Return packages declared by PEP 735 dependency groups."""
    packages: set[str] = set()
    if not isinstance(dependency_groups, dict):
        return packages
    for group_name in dependency_groups:
        if isinstance(group_name, str):
            _add_dependency_group(packages, dependency_groups, group_name)
    return packages


def _load_project_dependency_packages(repo_root: str) -> set[str]:
    """Parse project dependency package names dynamically from pyproject.toml."""
    pyproject_path = Path(repo_root) / "pyproject.toml"
    if not pyproject_path.is_file():
        return set()
    try:
        pyproject_data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as err:
        print(
            f"STEP_INFO: Warning: could not parse {pyproject_path}: {err!r}",
            flush=True,
        )
        return set()
    return _project_table_packages(pyproject_data.get("project")) | _dependency_group_packages(
        pyproject_data.get("dependency-groups")
    )


def _find_ha_constraints_path() -> Path | None:
    """Return path to installed Home Assistant package_constraints.txt if present."""
    ha_spec = importlib.util.find_spec("homeassistant")
    if not ha_spec or not ha_spec.origin:
        return None
    constraints_path = Path(ha_spec.origin).parent / "package_constraints.txt"
    return constraints_path if constraints_path.is_file() else None


def _resolve_target_ha_constraints(constraints_path: Path) -> dict[str, str]:
    """Combine package_constraints.txt and component manifest constraints.

    Reads component manifest constraints first and updates them with package_constraints.txt
    so that package_constraints.txt takes precedence as the authoritative constraints source.
    """
    required_constraints = _load_ha_manifest_constraints()
    required_constraints.update(_load_ha_package_constraints(constraints_path))
    return required_constraints


def _versions_differ(installed: str, required: str) -> bool:
    """Return whether two dependency versions differ."""
    try:
        return Version(installed) != Version(required)
    except InvalidVersion:
        return installed != required


def _constraint_drift(
    project_packages: set[str],
    required_constraints: Mapping[str, str],
) -> list[tuple[str, str, str]]:
    """Return installed project packages that differ from HA constraints."""
    drifted: list[tuple[str, str, str]] = []
    for package_name in sorted(project_packages):
        required_version = required_constraints.get(package_name)
        if not required_version:
            continue
        try:
            installed_version = md.version(package_name)
        except md.PackageNotFoundError:
            drifted.append((package_name, "not installed", required_version))
        else:
            if _versions_differ(installed_version, required_version):
                drifted.append((package_name, installed_version, required_version))
    return drifted


def _sync_constraint_drift(
    repo_root: str,
    drifted: list[tuple[str, str, str]],
) -> None:
    """Lock and synchronize dependencies that differ from HA constraints."""
    drift_details = ", ".join(
        f"{package} ({installed} -> {required})" for package, installed, required in sorted(drifted)
    )
    print(
        "STEP_INFO: Home Assistant constraints drift detected: "
        f"{drift_details}; locking and syncing",
        flush=True,
    )
    subprocess.run(
        [
            "uv",
            "lock",
            *[
                f"--upgrade-package={package}=={required}"
                for package, _, required in sorted(drifted)
            ],
        ],
        check=True,
        cwd=repo_root,
        timeout=_DEPENDENCY_SYNC_TIMEOUT_SECONDS,
    )
    subprocess.run(
        ["uv", "sync", "--all-groups"],
        check=True,
        cwd=repo_root,
        timeout=_DEPENDENCY_SYNC_TIMEOUT_SECONDS,
    )
    print(
        "STEP_INFO: Home Assistant constraints locked and environment synchronized successfully",
        flush=True,
    )


def _constraint_error_kind(error: Exception) -> str:
    """Classify a non-fatal constraint check error."""
    if isinstance(error, (orjson.JSONDecodeError, KeyError, ValueError)):
        return "structural configuration error"
    return "transient environment or file I/O issue"


def _check_and_sync_ha_constraints(repo_root: str) -> None:
    """Verify dependencies match Home Assistant package constraints and component manifests."""
    command_label = "check homeassistant constraints alignment"
    print(f"STEP_START: {command_label}", flush=True)

    constraints_path = _find_ha_constraints_path()
    if not constraints_path:
        print(
            "STEP_INFO: homeassistant package_constraints.txt not found; "
            "skipping constraints check",
            flush=True,
        )
        print(f"STEP_OK: {command_label}", flush=True)
        return

    try:
        required_constraints = _resolve_target_ha_constraints(constraints_path)
        check_packages = _load_project_dependency_packages(repo_root)
        if drifted := _constraint_drift(check_packages, required_constraints):
            _sync_constraint_drift(repo_root, drifted)
        else:
            print("STEP_INFO: Home Assistant constraints are fully synchronized", flush=True)

    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise
    except (
        OSError,
        orjson.JSONDecodeError,
        md.PackageNotFoundError,
        KeyError,
        ValueError,
    ) as err:
        print(
            "STEP_WARNING: Home Assistant constraints check encountered "
            f"{_constraint_error_kind(err)}: {err}",
            flush=True,
        )

    print(f"STEP_OK: {command_label}", flush=True)


def _run_dependency_steps(repo_root: str) -> None:
    """Synchronize dependencies and report available updates."""
    _run_sync_repair_step(
        repo_root,
        command_label="uv sync --check --all-groups",
        check_output_label="uv sync --check",
        repair_message="Environment is out of sync. Running 'uv sync --all-groups'",
        synchronized_message="Environment is already synchronized.",
        run_check=_run_uv_sync_check,
        run_repair=_repair_uv_sync,
    )
    _check_and_sync_ha_constraints(repo_root)
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


def _run_ruff_format_step(repo_root: str) -> None:
    """Format Python code with explicit list literal for security audits."""
    format_cmd = "uv run ruff format"
    print(f"STEP_START: {format_cmd}", flush=True)
    subprocess.run(
        ["uv", "run", "ruff", "format"],
        check=True,
        cwd=repo_root,
        timeout=_FORMAT_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {format_cmd}", flush=True)


def _run_ruff_check_step(repo_root: str) -> None:
    """Lint Python code with explicit list literal for security audits."""
    check_cmd = "uv run ruff check --fix"
    print(f"STEP_START: {check_cmd}", flush=True)
    subprocess.run(
        ["uv", "run", "ruff", "check", "--fix"],
        check=True,
        cwd=repo_root,
        timeout=_FORMAT_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {check_cmd}", flush=True)


def _run_ruff_steps(repo_root: str) -> None:
    """Lint and format Python with explicit Ruff commands."""
    _run_ruff_check_step(repo_root)
    _run_ruff_format_step(repo_root)


def _run_ty_step(repo_root: str) -> None:
    """Run Ty type check with explicit list literal for security audits."""
    ty_cmd = "uv run ty check"
    print(f"STEP_START: {ty_cmd}", flush=True)
    subprocess.run(
        ["uv", "run", "ty", "check"],
        check=True,
        cwd=repo_root,
        timeout=_STATIC_ANALYSIS_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {ty_cmd}", flush=True)


def _run_pyright_step(repo_root: str) -> None:
    """Run Pyright type check with explicit list literal for security audits."""
    pyright_cmd = "uv run pyright"
    print(f"STEP_START: {pyright_cmd}", flush=True)
    subprocess.run(
        ["uv", "run", "pyright"],
        check=True,
        cwd=repo_root,
        timeout=_STATIC_ANALYSIS_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {pyright_cmd}", flush=True)


def _run_interrogate_step(repo_root: str) -> None:
    """Run Interrogate docstring audit with explicit list literal for security audits."""
    interrogate_cmd = "uv run interrogate"
    print(f"STEP_START: {interrogate_cmd}", flush=True)
    subprocess.run(
        ["uv", "run", "interrogate"],
        check=True,
        cwd=repo_root,
        timeout=_STATIC_ANALYSIS_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {interrogate_cmd}", flush=True)


def _run_static_analysis_steps(repo_root: str) -> None:
    """Run explicit type and docstring analysis commands."""
    _run_ty_step(repo_root)
    _run_pyright_step(repo_root)
    _run_interrogate_step(repo_root)


def _run_prettier_step(repo_root: str) -> None:
    """Format text files using explicit Prettier list literal for security audits."""
    prettier_cmd = "npx prettier --log-level warn --write ."
    print(f"STEP_START: {prettier_cmd}", flush=True)
    subprocess.run(
        ["npx", "prettier", "--log-level", "warn", "--write", "."],
        check=True,
        cwd=repo_root,
        timeout=_PRETTIER_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {prettier_cmd}", flush=True)


def _run_pytest_step(repo_root: str) -> None:
    """Run test suite using explicit Pytest list literal for security audits."""
    pytest_cmd = "uv run pytest"
    print(f"STEP_START: {pytest_cmd}", flush=True)
    subprocess.run(
        ["uv", "run", "pytest"],
        check=True,
        cwd=repo_root,
        timeout=_PYTEST_STEP_TIMEOUT_SECONDS,
    )
    print(f"STEP_OK: {pytest_cmd}", flush=True)


def _validate_pipeline() -> None:
    """Run the validation pipeline steps in order."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    _run_dependency_steps(repo_root)
    _run_ruff_steps(repo_root)
    _run_static_analysis_steps(repo_root)
    _run_prettier_step(repo_root)
    _run_pytest_step(repo_root)


def main() -> None:
    """Main entry point."""
    try:
        _run_pipeline()
    except KeyboardInterrupt:
        print("VALIDATION_INTERRUPTED", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
