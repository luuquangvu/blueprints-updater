"""Validate whether a merged pull request should publish a release."""

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

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
    manifest = orjson.loads(manifest_path.read_text(encoding="utf-8"))
    raw_version = manifest.get("version", "")
    return str(raw_version), _normalized_version(raw_version)


def _read_pyproject_version(pyproject_path: Path) -> tuple[str, str | None]:
    """Read and validate the pyproject project version."""
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    project = pyproject.get("project", {})
    raw_version = project.get("version", "") if isinstance(project, dict) else ""
    return str(raw_version), _normalized_version(raw_version)


def _evaluate_release_gate(
    branch: str,
    labels: set[str],
    manifest_path: Path,
    pyproject_path: Path,
) -> ReleaseGateResult:
    """Evaluate release PR labels, branch name, and metadata versions."""
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


def _format_github_outputs(result: ReleaseGateResult) -> str:
    """Format release gate outputs for downstream GitHub Actions steps."""
    lines = [f"should_publish={str(result.should_publish).lower()}"]
    if result.should_publish:
        lines.extend(
            [
                f"version={result.version}",
                f"tag={result.tag}",
                f"prerelease={str(result.prerelease).lower()}",
            ]
        )
    return "\n".join(lines) + "\n"


def _read_required_env(name: str) -> str:
    """Read a required non-empty environment variable as an ordinary string."""
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"Environment variable {name!r} is not set")

    stripped = value.strip()
    if not stripped:
        raise ValueError(f"Environment variable {name!r} is empty")
    return stripped


def _read_pull_request_inputs() -> tuple[str, set[str]]:
    """Read pull request release gate inputs without accepting path values."""
    branch = _read_required_env("RELEASE_PR_HEAD_REF")
    labels_json = _read_required_env("RELEASE_PR_LABELS_JSON")
    try:
        raw_labels = orjson.loads(labels_json)
    except orjson.JSONDecodeError as exc:
        raise ValueError("RELEASE_PR_LABELS_JSON must contain a JSON array") from exc

    if not isinstance(raw_labels, list):
        raise ValueError("RELEASE_PR_LABELS_JSON must contain a JSON array")

    labels: set[str] = set()
    for label in raw_labels:
        if not isinstance(label, str):
            raise ValueError("RELEASE_PR_LABELS_JSON must contain only strings")
        labels.add(label)
    return branch, labels


def main() -> None:
    """Evaluate release gate inputs from the GitHub Actions environment."""
    branch, labels = _read_pull_request_inputs()
    result = _evaluate_release_gate(
        branch=branch,
        labels=labels,
        manifest_path=Path("custom_components/blueprints_updater/manifest.json"),
        pyproject_path=Path("pyproject.toml"),
    )
    print(result.message, file=sys.stderr)
    print(_format_github_outputs(result), end="")


if __name__ == "__main__":
    main()
