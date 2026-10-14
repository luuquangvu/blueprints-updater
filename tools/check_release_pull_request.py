"""Validate whether a merged pull request should publish a release."""

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import orjson

VERSION_CORE_PATTERN = r"\d+\.\d+\.\d+"
VERSION_PRERELEASE_PATTERN = r"-rc\.\d+"
VERSION_PATTERN = rf"({VERSION_CORE_PATTERN}(?:{VERSION_PRERELEASE_PATTERN})?)"
INTEGRATION_NAME_PATTERN = r"[A-Za-z0-9 ]+"

_VERSION_RE = re.compile(VERSION_PATTERN, flags=re.ASCII)
_INTEGRATION_NAME_RE = re.compile(INTEGRATION_NAME_PATTERN, flags=re.ASCII)
_RELEASE_BRANCH_RE = re.compile(rf"release/{VERSION_PATTERN}", flags=re.ASCII)
_PRERELEASE_RE = re.compile(VERSION_PRERELEASE_PATTERN, flags=re.ASCII)


@dataclass(frozen=True)
class ReleaseGateResult:
    """Release gate decision and outputs for GitHub Actions."""

    should_publish: bool
    message: str
    version: str = ""
    tag: str = ""
    prerelease: bool = False
    integration_name: str = ""


def _normalized_version(value: object) -> str | None:
    """Return a valid release version string or None."""
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.fullmatch(value)
    return match[1] if match else None


def _normalized_integration_name(value: object) -> str | None:
    """Return a valid integration name with ASCII alphanumeric and spaces or None."""
    if not isinstance(value, str):
        return None
    name = value.strip()
    if len(name) > 100 or not _INTEGRATION_NAME_RE.fullmatch(name):
        return None
    return name


def _read_manifest_metadata(manifest_path: Path) -> tuple[str | None, str, str | None]:
    """Read and validate the integration manifest name and version."""
    manifest = orjson.loads(manifest_path.read_bytes())
    raw_name = manifest.get("name")
    raw_version = manifest.get("version", "")
    integration_name = _normalized_integration_name(raw_name)
    return integration_name, str(raw_version), _normalized_version(raw_version)


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
    branch_match = _RELEASE_BRANCH_RE.fullmatch(branch)
    branch_version = branch_match[1] if branch_match else None

    manifest_name, manifest_version, manifest_version_full = _read_manifest_metadata(manifest_path)
    pyproject_version, pyproject_version_full = _read_pyproject_version(pyproject_path)

    version_matches = (
        branch_version is not None
        and manifest_version_full is not None
        and pyproject_version_full is not None
        and manifest_name is not None
        and branch_version == manifest_version_full == pyproject_version_full
    )
    should_publish = "release" in labels and version_matches

    if should_publish and manifest_name is not None:
        message = f"Release publishing enabled for branch {branch} with version {manifest_version}"
        return ReleaseGateResult(
            should_publish=True,
            message=message,
            version=manifest_version,
            tag=manifest_version,
            prerelease=bool(_PRERELEASE_RE.search(manifest_version)),
            integration_name=manifest_name,
        )

    message = (
        "Skipping publish because PR must have label 'release', "
        "branch release/X.Y.Z or release/X.Y.Z-rc.N, valid single-line "
        "manifest 'name', and matching manifest.json and pyproject.toml "
        f"versions; got branch {branch!r}, branch version {branch_version!r}, "
        f"manifest version {manifest_version!r}, pyproject version "
        f"{pyproject_version!r}, and manifest name {manifest_name!r}"
    )
    return ReleaseGateResult(should_publish=False, message=message)


def _format_github_outputs(result: ReleaseGateResult) -> str:
    """Format release gate outputs for downstream GitHub Actions steps."""
    lines = [f"should_publish={str(result.should_publish).lower()}"]
    if result.should_publish:
        if not (integration_name := _normalized_integration_name(result.integration_name)):
            raise ValueError(
                "integration_name must be a non-empty string of ASCII letters, digits, and spaces"
            )
        lines.extend(
            [
                f"version={result.version}",
                f"tag={result.tag}",
                f"prerelease={str(result.prerelease).lower()}",
                f"integration_name={integration_name}",
            ]
        )
    return "\n".join(lines) + "\n"


def _read_required_env(name: str) -> str:
    """Read a required non-empty environment variable as an ordinary string."""
    value = os.environ.get(name)
    if value is None:
        raise ValueError(f"Environment variable {name!r} is not set")

    if stripped := value.strip():
        return stripped
    raise ValueError(f"Environment variable {name!r} is empty")


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
