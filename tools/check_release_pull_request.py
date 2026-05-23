"""Validate whether a merged pull request should publish a release."""

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION_PATTERN = r"(([0-9]+\.[0-9]+\.[0-9]+)(?:-rc\.[0-9]+)?)"


@dataclass(frozen=True)
class ReleaseGateResult:
    """Release gate decision and outputs for GitHub Actions."""

    should_publish: bool
    message: str
    version: str = ""
    tag: str = ""
    prerelease: bool = False


def _normalized_version(value: Any) -> str | None:
    """Return a valid release version string or None."""
    if not isinstance(value, str):
        return None
    match = re.fullmatch(VERSION_PATTERN, value)
    return match.group(1) if match else None


def _read_manifest_version(manifest_path: Path) -> tuple[str, str | None]:
    """Read and validate the integration manifest version."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw_version = manifest.get("version", "")
    return str(raw_version), _normalized_version(raw_version)


def _read_pyproject_version(pyproject_path: Path) -> tuple[str, str | None]:
    """Read and validate the pyproject project version."""
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    raw_version = project.get("version", "") if isinstance(project, dict) else ""
    return str(raw_version), _normalized_version(raw_version)


def _evaluate_release_gate(
    event_path: Path,
    manifest_path: Path,
    pyproject_path: Path,
) -> ReleaseGateResult:
    """Evaluate release PR labels, branch name, and metadata versions."""
    event = json.loads(event_path.read_text(encoding="utf-8"))
    pull_request = event["pull_request"]
    branch = pull_request["head"]["ref"]
    labels = {label["name"] for label in pull_request.get("labels", [])}

    branch_match = re.fullmatch(rf"release/{VERSION_PATTERN}", branch)
    branch_version = branch_match.group(1) if branch_match else None

    manifest_version, manifest_version_full = _read_manifest_version(manifest_path)
    pyproject_version, pyproject_version_full = _read_pyproject_version(pyproject_path)

    version_matches = (
        branch_version is not None
        and manifest_version_full is not None
        and pyproject_version_full is not None
        and branch_version == manifest_version_full == pyproject_version_full
    )
    should_publish = "release" in labels and version_matches

    if should_publish:
        message = f"Release publishing enabled for branch {branch} with version {manifest_version}"
        return ReleaseGateResult(
            should_publish=True,
            message=message,
            version=manifest_version,
            tag=manifest_version,
            prerelease="rc" in manifest_version,
        )

    message = (
        "Skipping publish because PR must have label 'release', "
        "branch release/X.Y.Z or release/X.Y.Z-rc.N, and matching "
        "manifest.json and pyproject.toml versions; got branch "
        f"{branch!r}, branch version {branch_version!r}, manifest "
        f"version {manifest_version!r}, and pyproject version "
        f"{pyproject_version!r}"
    )
    return ReleaseGateResult(should_publish=False, message=message)


def _write_github_outputs(output_path: Path, result: ReleaseGateResult) -> None:
    """Append release gate outputs for downstream GitHub Actions steps."""
    lines = [f"should_publish={str(result.should_publish).lower()}"]
    if result.should_publish:
        lines.extend(
            [
                f"version={result.version}",
                f"tag={result.tag}",
                f"prerelease={str(result.prerelease).lower()}",
            ]
        )
    with output_path.open("a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")


def _is_within(resolved_path: str, allowed_root: str) -> bool:
    """Return True when ``resolved_path`` is contained within ``allowed_root``.

    Both arguments must already be absolute, real (symlink-free) paths produced
    by :func:`os.path.realpath`. Containment is decided by
    :func:`os.path.commonpath`, which is the canonical barrier recognized by
    CodeQL's ``py/path-injection`` query.
    """
    try:
        return os.path.commonpath([resolved_path, allowed_root]) == allowed_root
    except ValueError:
        return False


def _safe_resolve_env_path(env_var: str) -> Path:
    """Resolve and validate a path from an environment variable to prevent path injection.

    Mitigates CWE-22 (Path Traversal) and CWE-73 (Uncontrolled Data Used in Path
    Expression) by canonicalizing the user-provided value with
    :func:`os.path.realpath` and then enforcing strict containment under a fixed
    allowlist of system roots via :func:`os.path.commonpath`. This combination is
    the barrier that CodeQL's ``py/path-injection`` analysis recognizes as a
    sanitizer for tainted path data sourced from ``os.environ``.
    """
    raw_path = os.environ.get(env_var)
    if raw_path is None:
        raise ValueError(f"Environment variable {env_var!r} is not set")

    stripped = raw_path.strip()
    if not stripped:
        raise ValueError(f"Environment variable {env_var!r} is empty")
    if "\x00" in stripped:
        raise ValueError(f"Path from {env_var!r} contains invalid null bytes")
    if not os.path.isabs(stripped):
        raise ValueError(f"Path from {env_var!r} must be absolute")

    if env_var == "GITHUB_EVENT_PATH":
        expected_event_path = "/github/workflow/event.json"
        if stripped != expected_event_path:
            raise PermissionError(
                f"Path from {env_var!r} must equal {expected_event_path!r}: {stripped!r}"
            )
        try:
            resolved = os.path.realpath(expected_event_path, strict=True)
        except OSError as exc:
            raise ValueError("Expected GitHub event file is missing or not resolvable") from exc
        expected_resolved = os.path.realpath(expected_event_path)
        if resolved != expected_resolved:
            raise PermissionError(
                f"Path from {env_var!r} resolved outside expected event file: {resolved!r}"
            )
        return Path(resolved)

    try:
        resolved = os.path.realpath(stripped, strict=True)
    except OSError as exc:
        raise ValueError(
            f"Path from {env_var!r} must exist and be resolvable: {stripped!r}"
        ) from exc

    allowed_roots: tuple[str, ...] = (
        os.path.realpath("/home/runner"),
        os.path.realpath("/github"),
        os.path.realpath("/Users/runner"),
        os.path.realpath(os.getcwd()),
        os.path.realpath(tempfile.gettempdir()),
    )

    if not any(_is_within(resolved, root) for root in allowed_roots):
        raise PermissionError(
            f"Security Exception: Path {resolved!r} from environment variable "
            f"{env_var!r} is outside permitted secure directories"
        )

    if env_var == "GITHUB_OUTPUT":
        runner_temp_root = os.path.realpath(tempfile.gettempdir())
        if not _is_within(resolved, runner_temp_root):
            raise PermissionError(
                f"Path from {env_var!r} must be within runner temp directory "
                f"{runner_temp_root!r}: {resolved!r}"
            )

    if not os.path.isfile(resolved):
        raise ValueError(f"Path from {env_var!r} must point to a regular file: {resolved!r}")

    return Path(resolved)


def main() -> None:
    """Evaluate release gate inputs from the GitHub Actions environment."""
    event_path = _safe_resolve_env_path("GITHUB_EVENT_PATH")
    output_path = _safe_resolve_env_path("GITHUB_OUTPUT")
    result = _evaluate_release_gate(
        event_path=event_path,
        manifest_path=Path("custom_components/blueprints_updater/manifest.json"),
        pyproject_path=Path("pyproject.toml"),
    )
    print(result.message)
    _write_github_outputs(output_path, result)


if __name__ == "__main__":
    main()
