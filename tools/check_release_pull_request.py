"""Validate whether a merged pull request should publish a release."""

import json
import os
import re
import tempfile
import tomllib
from contextlib import suppress
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


def _safe_resolve_env_path(env_var: str) -> Path:
    """Resolve and validate a path from an environment variable to prevent path injection.

    This function mitigates CWE-22 (Path Traversal) and CWE-73 (Uncontrolled Data
    Used in Path Expression) by enforcing that the resolved path is absolute and
    fully contained within a designated set of allowed system directories.
    """
    raw_path = os.environ.get(env_var)
    if not raw_path:
        raise ValueError(f"Environment variable {env_var!r} is not set")

    try:
        candidate = Path(raw_path).expanduser().resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(
            f"Path from {env_var!r} must exist and be resolvable: {raw_path!r}"
        ) from exc
    if not candidate.is_absolute():
        raise ValueError(f"Path from {env_var!r} must be absolute: {candidate}")

    allowed_roots: list[Path] = [
        Path("/home/runner").resolve(strict=False),
        Path("/github").resolve(strict=False),
        Path("/Users/runner").resolve(strict=False),
        Path(os.getcwd()).resolve(strict=False),
        Path(tempfile.gettempdir()).resolve(strict=False),
    ]

    for var in ("GITHUB_WORKSPACE", "RUNNER_TEMP", "RUNNER_WORKSPACE", "GITHUB_HOME"):
        if val := os.environ.get(var):
            with suppress(Exception):
                allowed_roots.append(Path(val).expanduser().resolve(strict=False))

    is_safe = False
    for root in allowed_roots:
        try:
            candidate.relative_to(root)
            is_safe = True
            break
        except ValueError:
            continue

    if not is_safe:
        raise PermissionError(
            f"Security Exception: Path {str(candidate)!r} from environment variable "
            f"{env_var!r} is outside permitted secure directories"
        )

    if not candidate.is_file():
        raise ValueError(
            f"Path from {env_var!r} must point to a regular file: {candidate!r}"
        )

    return candidate


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
