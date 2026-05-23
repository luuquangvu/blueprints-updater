"""Validate whether a merged pull request should publish a release."""

import json
import os
import re
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


def evaluate_release_gate(
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


def write_github_outputs(output_path: Path, result: ReleaseGateResult) -> None:
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


def main() -> None:
    """Evaluate release gate inputs from the GitHub Actions environment."""
    event_path = Path(os.environ["GITHUB_EVENT_PATH"])
    output_path = Path(os.environ["GITHUB_OUTPUT"])
    result = evaluate_release_gate(
        event_path=event_path,
        manifest_path=Path("custom_components/blueprints_updater/manifest.json"),
        pyproject_path=Path("pyproject.toml"),
    )
    print(result.message)
    write_github_outputs(output_path, result)


if __name__ == "__main__":
    main()
